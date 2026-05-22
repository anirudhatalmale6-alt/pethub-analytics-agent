import re
import logging
from datetime import datetime, timezone
import httpx
from config import settings

logger = logging.getLogger("analytics-agent.indexing")


async def check_page_indexability(url: str) -> dict:
    """
    Check if a page is live and indexable by verifying:
    1. HTTP response (200 OK)
    2. No noindex robots meta tag
    3. Proper canonical URL
    4. Content is not a placeholder/coming-soon page
    """
    result = {
        "url": url,
        "status_code": 0,
        "is_live": False,
        "is_indexable": False,
        "has_noindex": False,
        "has_canonical": False,
        "canonical_url": "",
        "in_sitemap": False,
        "is_placeholder": False,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "issues": [],
    }

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; PetHubAnalyticsBot/1.0)"}
            )
            result["status_code"] = resp.status_code

            if resp.status_code == 200:
                result["is_live"] = True
                html = resp.text

                # Check for noindex
                robots_match = re.search(
                    r'<meta\s+name=["\']robots["\']\s+content=["\']([^"\']*)["\']',
                    html, re.IGNORECASE
                )
                if robots_match:
                    robots_content = robots_match.group(1).lower()
                    if "noindex" in robots_content:
                        result["has_noindex"] = True
                        result["issues"].append("Page has noindex meta tag")

                # Check for canonical
                canonical_match = re.search(
                    r'<link\s+rel=["\']canonical["\']\s+href=["\']([^"\']*)["\']',
                    html, re.IGNORECASE
                )
                if not canonical_match:
                    canonical_match = re.search(
                        r'<link\s+href=["\']([^"\']*?)["\']\s+rel=["\']canonical["\']',
                        html, re.IGNORECASE
                    )
                if canonical_match:
                    result["has_canonical"] = True
                    result["canonical_url"] = canonical_match.group(1)
                else:
                    result["issues"].append("Missing canonical URL")

                # Check for placeholder/coming-soon content
                placeholder_patterns = [
                    r"coming\s+soon",
                    r"under\s+construction",
                    r"site\s+is\s+being\s+built",
                    r"launching\s+soon",
                    r"maintenance\s+mode",
                ]
                lower_html = html.lower()
                for pattern in placeholder_patterns:
                    if re.search(pattern, lower_html):
                        result["is_placeholder"] = True
                        result["issues"].append(f"Page appears to be placeholder (matched: {pattern})")
                        break

                # Determine indexability
                result["is_indexable"] = (
                    result["is_live"]
                    and not result["has_noindex"]
                    and not result["is_placeholder"]
                )
            else:
                result["issues"].append(f"HTTP {resp.status_code}")

    except httpx.TimeoutException:
        result["issues"].append("Request timed out")
    except Exception as e:
        result["issues"].append(f"Error: {str(e)[:100]}")

    return result


async def check_sitemap(sitemap_url: str = None) -> dict:
    """
    Parse the XML sitemap and extract all URLs listed.
    Returns dict with sitemap URLs for cross-referencing.
    """
    if not sitemap_url:
        sitemap_url = f"{settings.WP_URL}/sitemap.xml"

    result = {
        "sitemap_url": sitemap_url,
        "accessible": False,
        "urls": [],
        "url_count": 0,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(
                sitemap_url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; PetHubAnalyticsBot/1.0)"}
            )
            if resp.status_code == 200:
                result["accessible"] = True
                xml_text = resp.text

                # Try sitemap index first (multiple sitemaps)
                sitemap_locs = re.findall(r"<sitemap>\s*<loc>([^<]+)</loc>", xml_text)
                if sitemap_locs:
                    # It's a sitemap index, fetch each child sitemap
                    for child_url in sitemap_locs:
                        try:
                            child_resp = await client.get(
                                child_url.strip(),
                                headers={"User-Agent": "Mozilla/5.0 (compatible; PetHubAnalyticsBot/1.0)"}
                            )
                            if child_resp.status_code == 200:
                                child_urls = re.findall(r"<url>\s*<loc>([^<]+)</loc>", child_resp.text)
                                result["urls"].extend([u.strip() for u in child_urls])
                        except Exception:
                            pass
                else:
                    # Direct sitemap with <url> entries
                    urls = re.findall(r"<url>\s*<loc>([^<]+)</loc>", xml_text)
                    result["urls"] = [u.strip() for u in urls]

                result["url_count"] = len(result["urls"])
                logger.info(f"Sitemap parsed: {result['url_count']} URLs found")
            else:
                logger.warning(f"Sitemap returned {resp.status_code}")

    except Exception as e:
        logger.error(f"Sitemap fetch failed: {e}")

    return result


async def check_all_indexing(pages: list) -> dict:
    """
    Run indexing checks on all pages in the inventory.
    Cross-references with the sitemap.
    """
    logger.info(f"Checking indexing status for {len(pages)} pages...")

    # First fetch the sitemap
    sitemap = await check_sitemap()
    sitemap_urls = set(u.rstrip("/") for u in sitemap.get("urls", []))

    results = []
    indexed_count = 0
    issues_count = 0

    for page in pages:
        url = page.get("url", "")
        if not url:
            continue

        check = await check_page_indexability(url)

        # Cross-reference with sitemap
        normalized_url = url.rstrip("/")
        check["in_sitemap"] = normalized_url in sitemap_urls or url in sitemap_urls

        if not check["in_sitemap"]:
            check["issues"].append("Page not found in sitemap.xml")

        if check["is_indexable"] and check["in_sitemap"]:
            indexed_count += 1

        if check["issues"]:
            issues_count += len(check["issues"])

        results.append({
            "page_id": page.get("id", 0),
            "title": page.get("title", ""),
            "slug": page.get("slug", ""),
            "url": url,
            "type": page.get("type", "page"),
            **check,
        })

    summary = {
        "total_pages": len(results),
        "indexable_count": indexed_count,
        "not_indexable_count": len(results) - indexed_count,
        "sitemap_accessible": sitemap.get("accessible", False),
        "sitemap_url_count": sitemap.get("url_count", 0),
        "total_issues": issues_count,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
    }

    logger.info(
        f"Indexing check complete: {indexed_count}/{len(results)} indexable, "
        f"{issues_count} issues"
    )
    return summary
