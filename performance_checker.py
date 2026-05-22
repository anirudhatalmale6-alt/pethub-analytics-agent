import logging
import time
from datetime import datetime, timezone
import httpx
from config import settings

logger = logging.getLogger("analytics-agent.performance")

# Thresholds for performance grading
RESPONSE_TIME_GOOD = 1.0       # seconds
RESPONSE_TIME_WARN = 2.5       # seconds
PAGE_SIZE_GOOD = 500_000       # 500 KB
PAGE_SIZE_WARN = 1_500_000     # 1.5 MB
TTFB_GOOD = 0.5               # seconds
TTFB_WARN = 1.0               # seconds


def grade_metric(value: float, good_threshold: float, warn_threshold: float) -> str:
    """Return 'good', 'warning', or 'critical' based on thresholds."""
    if value <= good_threshold:
        return "good"
    elif value <= warn_threshold:
        return "warning"
    return "critical"


async def check_page_performance(url: str) -> dict:
    """
    Measure performance metrics for a single page:
    - Total response time
    - Time to first byte (TTFB)
    - Page size (bytes)
    - Content type
    - HTTP status
    - Redirect count
    """
    result = {
        "url": url,
        "status_code": 0,
        "response_time_ms": 0,
        "ttfb_ms": 0,
        "page_size_bytes": 0,
        "page_size_display": "0 B",
        "content_type": "",
        "redirect_count": 0,
        "is_gzipped": False,
        "has_cache_headers": False,
        "cache_control": "",
        "grade": "unknown",
        "issues": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Measure TTFB using a HEAD request first
            ttfb_start = time.monotonic()
            head_resp = await client.head(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; PetHubAnalyticsBot/1.0)"},
                follow_redirects=True
            )
            ttfb_end = time.monotonic()
            result["ttfb_ms"] = round((ttfb_end - ttfb_start) * 1000)

            # Full GET request for page size and response time
            start = time.monotonic()
            resp = await client.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; PetHubAnalyticsBot/1.0)",
                    "Accept-Encoding": "gzip, deflate, br",
                },
                follow_redirects=True
            )
            end = time.monotonic()

            result["status_code"] = resp.status_code
            result["response_time_ms"] = round((end - start) * 1000)
            result["page_size_bytes"] = len(resp.content)
            result["page_size_display"] = _format_bytes(len(resp.content))
            result["content_type"] = resp.headers.get("content-type", "")
            result["redirect_count"] = len(resp.history)

            # Check compression
            content_encoding = resp.headers.get("content-encoding", "")
            result["is_gzipped"] = content_encoding in ("gzip", "br", "deflate")

            # Check cache headers
            cache_control = resp.headers.get("cache-control", "")
            result["cache_control"] = cache_control
            result["has_cache_headers"] = bool(cache_control)

            # Grade the response time
            response_time_sec = result["response_time_ms"] / 1000
            ttfb_sec = result["ttfb_ms"] / 1000

            time_grade = grade_metric(response_time_sec, RESPONSE_TIME_GOOD, RESPONSE_TIME_WARN)
            size_grade = grade_metric(result["page_size_bytes"], PAGE_SIZE_GOOD, PAGE_SIZE_WARN)
            ttfb_grade = grade_metric(ttfb_sec, TTFB_GOOD, TTFB_WARN)

            # Overall grade is the worst of individual grades
            grades = [time_grade, size_grade, ttfb_grade]
            if "critical" in grades:
                result["grade"] = "critical"
            elif "warning" in grades:
                result["grade"] = "warning"
            else:
                result["grade"] = "good"

            # Build issues list
            if time_grade == "critical":
                result["issues"].append(f"Very slow response: {result['response_time_ms']}ms (should be under {int(RESPONSE_TIME_WARN * 1000)}ms)")
            elif time_grade == "warning":
                result["issues"].append(f"Slow response: {result['response_time_ms']}ms (aim for under {int(RESPONSE_TIME_GOOD * 1000)}ms)")

            if size_grade == "critical":
                result["issues"].append(f"Very large page: {result['page_size_display']} (should be under {_format_bytes(PAGE_SIZE_WARN)})")
            elif size_grade == "warning":
                result["issues"].append(f"Large page: {result['page_size_display']} (aim for under {_format_bytes(PAGE_SIZE_GOOD)})")

            if ttfb_grade == "critical":
                result["issues"].append(f"High TTFB: {result['ttfb_ms']}ms (should be under {int(TTFB_WARN * 1000)}ms)")
            elif ttfb_grade == "warning":
                result["issues"].append(f"Elevated TTFB: {result['ttfb_ms']}ms (aim for under {int(TTFB_GOOD * 1000)}ms)")

            if not result["is_gzipped"]:
                result["issues"].append("Response not compressed (missing gzip/brotli)")

            if not result["has_cache_headers"]:
                result["issues"].append("No cache-control headers set")

            if result["redirect_count"] > 2:
                result["issues"].append(f"Too many redirects ({result['redirect_count']})")

            if resp.status_code != 200:
                result["issues"].append(f"Non-200 status code: {resp.status_code}")

    except httpx.TimeoutException:
        result["issues"].append("Request timed out (30s)")
        result["grade"] = "critical"
    except Exception as e:
        result["issues"].append(f"Error: {str(e)[:100]}")
        result["grade"] = "critical"

    return result


async def check_all_performance(pages: list) -> dict:
    """
    Run performance checks on all pages in the inventory.
    """
    logger.info(f"Checking performance for {len(pages)} pages...")

    results = []
    total_response_time = 0
    total_size = 0
    slow_pages = []
    large_pages = []
    total_issues = 0

    for page in pages:
        url = page.get("url", "")
        if not url:
            continue

        perf = await check_page_performance(url)

        total_response_time += perf["response_time_ms"]
        total_size += perf["page_size_bytes"]

        if perf["response_time_ms"] > RESPONSE_TIME_WARN * 1000:
            slow_pages.append({
                "title": page.get("title", ""),
                "url": url,
                "response_time_ms": perf["response_time_ms"],
            })

        if perf["page_size_bytes"] > PAGE_SIZE_WARN:
            large_pages.append({
                "title": page.get("title", ""),
                "url": url,
                "size": perf["page_size_display"],
                "size_bytes": perf["page_size_bytes"],
            })

        total_issues += len(perf["issues"])

        results.append({
            "page_id": page.get("id", 0),
            "title": page.get("title", ""),
            "slug": page.get("slug", ""),
            "type": page.get("type", "page"),
            **perf,
        })

    page_count = max(len(results), 1)
    avg_response_time = round(total_response_time / page_count)
    avg_page_size = round(total_size / page_count)

    # Sort by response time descending (slowest first)
    results.sort(key=lambda x: x["response_time_ms"], reverse=True)

    summary = {
        "total_pages": len(results),
        "avg_response_time_ms": avg_response_time,
        "avg_page_size_bytes": avg_page_size,
        "avg_page_size_display": _format_bytes(avg_page_size),
        "slow_pages_count": len(slow_pages),
        "large_pages_count": len(large_pages),
        "total_issues": total_issues,
        "slow_pages": slow_pages,
        "large_pages": large_pages,
        "good_count": sum(1 for r in results if r["grade"] == "good"),
        "warning_count": sum(1 for r in results if r["grade"] == "warning"),
        "critical_count": sum(1 for r in results if r["grade"] == "critical"),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
    }

    logger.info(
        f"Performance check complete: avg {avg_response_time}ms, "
        f"{len(slow_pages)} slow, {len(large_pages)} large"
    )
    return summary


def _format_bytes(size: int) -> str:
    """Format bytes to human-readable string."""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    else:
        return f"{size / (1024 * 1024):.1f} MB"
