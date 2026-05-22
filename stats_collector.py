import re
import logging
import base64
from datetime import datetime, timezone, timedelta
import httpx
from config import settings

logger = logging.getLogger("analytics-agent.stats")

WP_AUTH = "Basic " + base64.b64encode(f"{settings.WP_USER}:{settings.WP_APP_PASSWORD}".encode()).decode()
WP_HEADERS = {"Authorization": WP_AUTH}


async def fetch_all_pages() -> list:
    """Fetch all published pages from WordPress REST API."""
    pages = []
    page_num = 1
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            resp = await client.get(
                f"{settings.WP_URL}/wp-json/wp/v2/pages",
                headers=WP_HEADERS,
                params={"per_page": 50, "page": page_num, "status": "publish"}
            )
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not batch:
                break
            pages.extend(batch)
            if len(batch) < 50:
                break
            page_num += 1
    return pages


async def fetch_all_posts() -> list:
    """Fetch all published posts from WordPress REST API."""
    posts = []
    page_num = 1
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            resp = await client.get(
                f"{settings.WP_URL}/wp-json/wp/v2/posts",
                headers=WP_HEADERS,
                params={"per_page": 50, "page": page_num, "status": "publish"}
            )
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not batch:
                break
            posts.extend(batch)
            if len(batch) < 50:
                break
            page_num += 1
    return posts


async def fetch_wp_stats() -> dict:
    """
    Fetch site traffic stats via WordPress.com / Jetpack stats endpoints.
    Tries multiple endpoint patterns since WordPress.com hosted sites expose
    stats differently than self-hosted with Jetpack.
    """
    stats = {
        "views_today": 0,
        "views_yesterday": 0,
        "views_week": 0,
        "views_month": 0,
        "visitors_today": 0,
        "visitors_week": 0,
        "top_pages": [],
        "referrers": [],
        "search_terms": [],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "none",
    }

    endpoints_to_try = [
        # WordPress.com REST API v1.1 (for WP.com hosted sites)
        {
            "url": f"https://public-api.wordpress.com/rest/v1.1/sites/pethubonline.com/stats",
            "headers": WP_HEADERS,
            "parser": "_parse_wpcom_stats",
        },
        # Jetpack stats via WP REST API
        {
            "url": f"{settings.WP_URL}/wp-json/wpcom/v2/stats",
            "headers": WP_HEADERS,
            "parser": "_parse_jetpack_stats",
        },
        # WordPress.com stats summary
        {
            "url": f"https://public-api.wordpress.com/rest/v1.1/sites/pethubonline.com/stats/summary",
            "headers": WP_HEADERS,
            "parser": "_parse_wpcom_summary",
        },
    ]

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for ep in endpoints_to_try:
            try:
                resp = await client.get(ep["url"], headers=ep["headers"])
                if resp.status_code == 200:
                    data = resp.json()
                    parser_func = globals().get(ep["parser"])
                    if parser_func:
                        parsed = parser_func(data)
                        if parsed:
                            stats.update(parsed)
                            stats["source"] = ep["parser"]
                            logger.info(f"Stats fetched via {ep['parser']}")
                            break
                else:
                    logger.debug(f"Stats endpoint {ep['url']} returned {resp.status_code}")
            except Exception as e:
                logger.debug(f"Stats endpoint {ep['url']} failed: {e}")

        # Try fetching top posts separately
        try:
            resp = await client.get(
                f"https://public-api.wordpress.com/rest/v1.1/sites/pethubonline.com/stats/top-posts",
                headers=WP_HEADERS,
                params={"period": "week", "num": 10}
            )
            if resp.status_code == 200:
                data = resp.json()
                if "top-posts" in data:
                    stats["top_pages"] = [
                        {"title": p.get("title", ""), "url": p.get("href", ""), "views": p.get("views", 0)}
                        for p in data["top-posts"][:10]
                    ]
        except Exception:
            pass

        # Try fetching referrers separately
        try:
            resp = await client.get(
                f"https://public-api.wordpress.com/rest/v1.1/sites/pethubonline.com/stats/referrers",
                headers=WP_HEADERS,
                params={"period": "week", "num": 10}
            )
            if resp.status_code == 200:
                data = resp.json()
                if "referrers" in data:
                    for group in data.get("referrers", []):
                        for ref in group.get("results", []):
                            if isinstance(ref, dict):
                                stats["referrers"].append({
                                    "name": ref.get("name", ""),
                                    "views": ref.get("views", 0),
                                })
        except Exception:
            pass

        # Try fetching search terms
        try:
            resp = await client.get(
                f"https://public-api.wordpress.com/rest/v1.1/sites/pethubonline.com/stats/search-terms",
                headers=WP_HEADERS,
                params={"period": "week", "num": 10}
            )
            if resp.status_code == 200:
                data = resp.json()
                if "search-terms" in data:
                    for term_group in data.get("search-terms", []):
                        for term in term_group.get("terms", []):
                            if isinstance(term, dict):
                                stats["search_terms"].append({
                                    "term": term.get("term", ""),
                                    "views": term.get("views", 0),
                                })
        except Exception:
            pass

    return stats


def _parse_wpcom_stats(data: dict) -> dict:
    """Parse WordPress.com REST v1.1 stats response."""
    if not data or "stats" not in data:
        return None
    s = data["stats"]
    result = {
        "views_today": s.get("views_today", 0),
        "views_yesterday": s.get("views_yesterday", 0),
        "views_week": s.get("views", 0),
        "visitors_today": s.get("visitors_today", 0),
        "visitors_week": s.get("visitors", 0),
    }
    # Parse visits data for monthly
    visits = data.get("visits", {}).get("data", [])
    if visits:
        monthly_views = sum(v[1] for v in visits[-30:] if len(v) > 1)
        result["views_month"] = monthly_views
    return result


def _parse_jetpack_stats(data: dict) -> dict:
    """Parse Jetpack stats response via WP REST API."""
    if not data:
        return None
    result = {}
    if "stats" in data:
        s = data["stats"]
        result["views_today"] = s.get("views_today", 0)
        result["views_yesterday"] = s.get("views_yesterday", 0)
        result["visitors_today"] = s.get("visitors_today", 0)
    if "visits" in data:
        visits = data["visits"]
        if isinstance(visits, list) and visits:
            result["views_week"] = sum(v.get("views", 0) for v in visits[-7:])
            result["views_month"] = sum(v.get("views", 0) for v in visits[-30:])
            result["visitors_week"] = sum(v.get("visitors", 0) for v in visits[-7:])
    return result if result else None


def _parse_wpcom_summary(data: dict) -> dict:
    """Parse WordPress.com stats summary response."""
    if not data:
        return None
    result = {
        "views_today": data.get("views", 0),
        "visitors_today": data.get("visitors", 0),
        "views_week": data.get("views", 0),
        "visitors_week": data.get("visitors", 0),
    }
    return result


async def build_page_inventory() -> list:
    """
    Build a complete inventory of all published pages and posts with their
    metadata for tracking purposes.
    """
    pages = await fetch_all_pages()
    posts = await fetch_all_posts()
    all_content = pages + posts

    inventory = []
    for item in all_content:
        title_raw = item.get("title", {}).get("rendered", "")
        title = re.sub(r"<[^>]+>", "", title_raw).strip()
        content_html = item.get("content", {}).get("rendered", "")
        text = re.sub(r"<[^>]+>", " ", content_html)
        text = re.sub(r"\s+", " ", text).strip()
        word_count = len(text.split())

        inventory.append({
            "id": item.get("id", 0),
            "title": title,
            "slug": item.get("slug", ""),
            "url": item.get("link", ""),
            "type": item.get("type", "page"),
            "status": item.get("status", ""),
            "date_published": item.get("date", ""),
            "date_modified": item.get("modified", ""),
            "word_count": word_count,
            "author": item.get("author", 0),
            "featured_media": item.get("featured_media", 0),
        })

    return inventory


async def collect_all_stats() -> dict:
    """Run full data collection: stats + page inventory."""
    logger.info("Starting full data collection...")

    stats = await fetch_wp_stats()
    inventory = await build_page_inventory()

    result = {
        "traffic": stats,
        "inventory": inventory,
        "total_pages": len(inventory),
        "total_words": sum(p["word_count"] for p in inventory),
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }

    logger.info(f"Collection complete: {len(inventory)} pages, traffic source: {stats['source']}")
    return result
