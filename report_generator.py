"""
Weekly performance report generator.
Compiles data from analytics + sister agents into a comprehensive report.
"""

import logging
from datetime import datetime, timezone, timedelta
import httpx

logger = logging.getLogger("analytics-agent.reports")

AGENT_URLS = {
    "seo": "http://127.0.0.1:8101/agents/seo/api/status",
    "social": "http://127.0.0.1:8103/agents/social/api/status",
    "maintenance": "http://127.0.0.1:8104/agents/maintenance/api/status",
}


async def _fetch_agent_status(name: str, url: str) -> dict:
    """Fetch status from a sister agent, return None on failure."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        logger.debug(f"Could not reach {name} agent: {e}")
    return None


async def generate_weekly_report(state: dict) -> dict:
    """
    Compile a comprehensive weekly report from all available data.
    """
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)

    report = {
        "generated_at": now.isoformat(),
        "date_range": {
            "start": week_ago.isoformat(),
            "end": now.isoformat(),
        },
        "traffic": {},
        "top_pages": [],
        "performance": {},
        "indexing": {},
        "seo": None,
        "social": None,
        "maintenance": None,
        "recommendations": [],
    }

    # ── Traffic Summary ──
    traffic = state.get("traffic", {})
    traffic_history = state.get("traffic_history", [])

    current_week_views = traffic.get("views_week", 0)
    current_week_visitors = traffic.get("visitors_week", 0)

    # Estimate last week's views from history
    last_week_views = 0
    if len(traffic_history) >= 2:
        # Find entries from ~7 days ago
        older_entries = [
            h for h in traffic_history
            if h.get("date") and _parse_date(h["date"]) and _parse_date(h["date"]) < week_ago
        ]
        if older_entries:
            last_week_views = older_entries[-1].get("views_week", 0)

    if last_week_views > 0:
        change_pct = ((current_week_views - last_week_views) / last_week_views) * 100
        if change_pct > 0:
            trend = "growing"
        elif change_pct < -5:
            trend = "declining"
        else:
            trend = "stable"
    else:
        change_pct = 0
        trend = "unknown"

    report["traffic"] = {
        "views_this_week": current_week_views,
        "views_last_week": last_week_views,
        "change_pct": round(change_pct, 1),
        "trend": trend,
        "visitors_this_week": current_week_visitors,
        "views_today": traffic.get("views_today", 0),
        "views_month": traffic.get("views_month", 0),
    }

    # Top 5 pages
    top_pages = traffic.get("top_pages", [])
    report["top_pages"] = top_pages[:5]

    # ── Performance Summary ──
    perf = state.get("performance")
    if perf:
        results = perf.get("results", [])
        slowest = sorted(results, key=lambda r: r.get("response_time_ms", 0), reverse=True)[:5]
        report["performance"] = {
            "avg_response_time_ms": perf.get("avg_response_time_ms", 0),
            "avg_page_size": perf.get("avg_page_size_display", "0 B"),
            "good_count": perf.get("good_count", 0),
            "warning_count": perf.get("warning_count", 0),
            "critical_count": perf.get("critical_count", 0),
            "total_pages": perf.get("total_pages", 0),
            "slowest_pages": [
                {
                    "title": p.get("title", ""),
                    "url": p.get("url", ""),
                    "response_time_ms": p.get("response_time_ms", 0),
                    "grade": p.get("grade", "unknown"),
                }
                for p in slowest
            ],
        }
    else:
        report["performance"] = {"message": "No performance data available"}

    # ── Indexing Summary ──
    indexing = state.get("indexing")
    if indexing:
        total = indexing.get("total_pages", 0)
        indexable = indexing.get("indexable_count", 0)
        coverage = round((indexable / total) * 100, 1) if total > 0 else 0
        report["indexing"] = {
            "total_pages": total,
            "indexable_count": indexable,
            "not_indexable_count": indexing.get("not_indexable_count", 0),
            "coverage_pct": coverage,
            "sitemap_accessible": indexing.get("sitemap_accessible", False),
            "total_issues": indexing.get("total_issues", 0),
        }
    else:
        report["indexing"] = {"message": "No indexing data available"}

    # ── Sister Agent Summaries ──
    for agent_name, agent_url in AGENT_URLS.items():
        status = await _fetch_agent_status(agent_name, agent_url)
        if status:
            report[agent_name] = {
                "status": status.get("status", "unknown"),
                "data": status,
                "reachable": True,
            }
        else:
            report[agent_name] = {
                "status": "unreachable",
                "reachable": False,
            }

    # ── Recommendations ──
    recs = []

    # Traffic recommendations
    if trend == "declining":
        recs.append({
            "area": "traffic",
            "priority": "high",
            "recommendation": "Traffic is declining. Review recent content changes and SEO performance.",
        })
    if current_week_views == 0:
        recs.append({
            "area": "traffic",
            "priority": "critical",
            "recommendation": "No traffic recorded this week. Verify site accessibility and analytics tracking.",
        })

    # Performance recommendations
    if perf:
        avg_rt = perf.get("avg_response_time_ms", 0)
        if avg_rt > 2500:
            recs.append({
                "area": "performance",
                "priority": "high",
                "recommendation": f"Average response time is {avg_rt}ms. Consider enabling caching, optimizing images, or upgrading hosting.",
            })
        elif avg_rt > 1000:
            recs.append({
                "area": "performance",
                "priority": "medium",
                "recommendation": f"Average response time is {avg_rt}ms. Look into performance optimizations.",
            })
        if perf.get("critical_count", 0) > 0:
            recs.append({
                "area": "performance",
                "priority": "high",
                "recommendation": f"{perf['critical_count']} pages have critical performance issues.",
            })

    # Indexing recommendations
    if indexing:
        coverage = report["indexing"].get("coverage_pct", 0)
        if coverage < 80:
            recs.append({
                "area": "indexing",
                "priority": "high",
                "recommendation": f"Only {coverage}% of pages are indexable. Fix noindex tags, sitemap, and canonicals.",
            })
        if indexing.get("total_issues", 0) > 5:
            recs.append({
                "area": "indexing",
                "priority": "medium",
                "recommendation": f"{indexing['total_issues']} indexing issues found. Review and resolve them.",
            })

    report["recommendations"] = recs

    # Generate AI-powered natural language summary
    try:
        from ai_client import ai_generate_weekly_report
        ai_summary = await ai_generate_weekly_report({
            "traffic_views": report.get("traffic", {}).get("views_this_week", 0),
            "traffic_change_pct": report.get("traffic", {}).get("change_pct", 0),
            "traffic_trend": report.get("traffic", {}).get("trend", "unknown"),
            "top_pages": [p.get("title", "") for p in report.get("top_pages", [])[:5]],
            "seo_score": report.get("seo", {}).get("avg_score") if report.get("seo") else None,
            "seo_issues": report.get("seo", {}).get("total_issues") if report.get("seo") else None,
            "social_posts": report.get("social", {}).get("total_posts") if report.get("social") else None,
            "maintenance_issues": report.get("maintenance", {}).get("link_issues") if report.get("maintenance") else None,
            "recommendations_count": len(recs),
        })
        if ai_summary:
            report["ai_summary"] = ai_summary
            logger.info("AI summary generated for weekly report")
    except Exception as e:
        logger.debug(f"AI summary unavailable: {e}")

    return report


def format_report_text(report: dict) -> str:
    """Format the weekly report as readable text for dashboard / messaging."""
    lines = []
    lines.append("=" * 60)
    lines.append("PETHUB ONLINE - WEEKLY ANALYTICS REPORT")
    lines.append("=" * 60)
    date_range = report.get("date_range", {})
    lines.append(f"Period: {date_range.get('start', '?')[:10]} to {date_range.get('end', '?')[:10]}")
    lines.append(f"Generated: {report.get('generated_at', '?')[:19]}")
    lines.append("")

    # Traffic
    t = report.get("traffic", {})
    lines.append("--- TRAFFIC ---")
    lines.append(f"  Views this week:  {t.get('views_this_week', 0):,}")
    lines.append(f"  Views last week:  {t.get('views_last_week', 0):,}")
    change = t.get("change_pct", 0)
    arrow = "+" if change > 0 else ""
    lines.append(f"  Change:           {arrow}{change}%  ({t.get('trend', 'unknown')})")
    lines.append(f"  Visitors:         {t.get('visitors_this_week', 0):,}")
    lines.append(f"  Monthly views:    {t.get('views_month', 0):,}")
    lines.append("")

    # Top pages
    top = report.get("top_pages", [])
    if top:
        lines.append("--- TOP PAGES ---")
        for i, p in enumerate(top, 1):
            lines.append(f"  {i}. {p.get('title', p.get('url', '?'))} ({p.get('views', 0):,} views)")
        lines.append("")

    # Performance
    p = report.get("performance", {})
    if "avg_response_time_ms" in p:
        lines.append("--- PERFORMANCE ---")
        lines.append(f"  Avg response time: {p['avg_response_time_ms']}ms")
        lines.append(f"  Avg page size:     {p.get('avg_page_size', '?')}")
        lines.append(f"  Good / Warn / Crit: {p.get('good_count', 0)} / {p.get('warning_count', 0)} / {p.get('critical_count', 0)}")
        slowest = p.get("slowest_pages", [])
        if slowest:
            lines.append("  Slowest pages:")
            for s in slowest[:3]:
                lines.append(f"    - {s.get('title', '?')} ({s.get('response_time_ms', 0)}ms, {s.get('grade', '?')})")
        lines.append("")

    # Indexing
    idx = report.get("indexing", {})
    if "total_pages" in idx:
        lines.append("--- INDEXING ---")
        lines.append(f"  Total pages:    {idx['total_pages']}")
        lines.append(f"  Indexable:      {idx.get('indexable_count', 0)} ({idx.get('coverage_pct', 0)}%)")
        lines.append(f"  Issues:         {idx.get('total_issues', 0)}")
        lines.append(f"  Sitemap:        {'OK' if idx.get('sitemap_accessible') else 'Not accessible'}")
        lines.append("")

    # Agent statuses
    for agent_name in ("seo", "social", "maintenance"):
        agent_data = report.get(agent_name, {})
        status = agent_data.get("status", "unknown") if agent_data else "unknown"
        lines.append(f"  {agent_name.upper()} Agent: {status}")
    lines.append("")

    # Recommendations
    recs = report.get("recommendations", [])
    if recs:
        lines.append("--- RECOMMENDATIONS ---")
        for r in recs:
            priority = r.get("priority", "info").upper()
            lines.append(f"  [{priority}] {r.get('recommendation', '')}")
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)


def _parse_date(date_str: str):
    """Safely parse an ISO date string."""
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
