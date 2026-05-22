"""
Anomaly detection for traffic and performance data.
Flags spikes, drops, zero-traffic days, and performance degradation.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger("analytics-agent.anomaly")


def detect_traffic_anomalies(traffic_history: list) -> list:
    """
    Analyze traffic_history (list of daily snapshots with views/visitors) for anomalies.
    - Rolling 7-day average comparison (>50% deviation = spike or drop)
    - 3+ consecutive days of decline
    - Zero traffic for a day (site might be down)
    """
    anomalies = []

    if not traffic_history or len(traffic_history) < 2:
        return anomalies

    # Extract views from each snapshot
    views_list = []
    for snap in traffic_history:
        views = snap.get("views_today", 0) or snap.get("views_week", 0) or 0
        views_list.append({
            "date": snap.get("date", ""),
            "views": views,
            "visitors": snap.get("visitors_today", 0),
        })

    # Check for zero-traffic days
    for entry in views_list:
        if entry["views"] == 0 and entry["visitors"] == 0:
            anomalies.append({
                "type": "zero_traffic",
                "severity": "critical",
                "date": entry["date"],
                "message": f"Zero traffic detected on {entry['date'][:10]} - site may be down",
                "value": 0,
                "detected_at": datetime.now(timezone.utc).isoformat(),
            })

    # Rolling 7-day average comparison
    if len(views_list) >= 8:
        for i in range(7, len(views_list)):
            window = views_list[i - 7:i]
            avg_7d = sum(v["views"] for v in window) / 7
            current = views_list[i]["views"]

            if avg_7d > 0:
                deviation = (current - avg_7d) / avg_7d

                if deviation > 0.5:
                    anomalies.append({
                        "type": "traffic_spike",
                        "severity": "warning",
                        "date": views_list[i]["date"],
                        "message": (
                            f"Traffic spike: {current} views vs 7-day avg of {avg_7d:.0f} "
                            f"(+{deviation * 100:.0f}%)"
                        ),
                        "value": current,
                        "average": round(avg_7d),
                        "deviation_pct": round(deviation * 100),
                        "detected_at": datetime.now(timezone.utc).isoformat(),
                    })
                elif deviation < -0.5:
                    anomalies.append({
                        "type": "traffic_drop",
                        "severity": "warning",
                        "date": views_list[i]["date"],
                        "message": (
                            f"Traffic drop: {current} views vs 7-day avg of {avg_7d:.0f} "
                            f"({deviation * 100:.0f}%)"
                        ),
                        "value": current,
                        "average": round(avg_7d),
                        "deviation_pct": round(deviation * 100),
                        "detected_at": datetime.now(timezone.utc).isoformat(),
                    })

    # Check for 3+ consecutive days of decline
    if len(views_list) >= 4:
        consecutive_decline = 0
        for i in range(1, len(views_list)):
            if views_list[i]["views"] < views_list[i - 1]["views"]:
                consecutive_decline += 1
                if consecutive_decline >= 3:
                    anomalies.append({
                        "type": "consecutive_decline",
                        "severity": "warning",
                        "date": views_list[i]["date"],
                        "message": (
                            f"{consecutive_decline} consecutive days of traffic decline "
                            f"(from {views_list[i - consecutive_decline]['views']} to {views_list[i]['views']})"
                        ),
                        "value": views_list[i]["views"],
                        "decline_days": consecutive_decline,
                        "detected_at": datetime.now(timezone.utc).isoformat(),
                    })
                    break  # Only report the first streak
            else:
                consecutive_decline = 0

    return anomalies


def detect_performance_anomalies(performance: dict, history: list) -> list:
    """
    Check performance data for anomalies:
    - Avg response time > 2x historical average
    - Any page with TTFB > 3 seconds
    - Sudden increase in error rate
    """
    anomalies = []

    if not performance:
        return anomalies

    current_avg_rt = performance.get("avg_response_time_ms", 0)
    results = performance.get("results", [])

    # Check for high TTFB pages (> 3000ms)
    for page in results:
        ttfb = page.get("ttfb_ms", 0)
        if ttfb > 3000:
            anomalies.append({
                "type": "high_ttfb",
                "severity": "critical",
                "message": (
                    f"High TTFB ({ttfb}ms) on: {page.get('title', page.get('url', 'Unknown'))}"
                ),
                "url": page.get("url", ""),
                "value": ttfb,
                "threshold": 3000,
                "detected_at": datetime.now(timezone.utc).isoformat(),
            })

    # Compare against historical average response time
    if history and len(history) >= 2:
        historical_rts = []
        for h in history:
            rt = h.get("avg_response_time_ms", 0)
            if rt > 0:
                historical_rts.append(rt)

        if historical_rts:
            hist_avg = sum(historical_rts) / len(historical_rts)
            if hist_avg > 0 and current_avg_rt > hist_avg * 2:
                anomalies.append({
                    "type": "response_time_spike",
                    "severity": "critical",
                    "message": (
                        f"Response time spike: {current_avg_rt}ms vs historical avg of {hist_avg:.0f}ms "
                        f"({current_avg_rt / hist_avg:.1f}x increase)"
                    ),
                    "value": current_avg_rt,
                    "historical_avg": round(hist_avg),
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                })

    # Check for error rate increase (non-200 status codes)
    error_pages = [r for r in results if r.get("status_code", 200) != 200]
    critical_pages = [r for r in results if r.get("grade") == "critical"]

    total_pages = max(len(results), 1)
    error_rate = len(error_pages) / total_pages
    critical_rate = len(critical_pages) / total_pages

    if error_rate > 0.1:  # More than 10% errors
        anomalies.append({
            "type": "high_error_rate",
            "severity": "critical",
            "message": (
                f"High error rate: {len(error_pages)}/{total_pages} pages returning errors "
                f"({error_rate * 100:.0f}%)"
            ),
            "value": round(error_rate * 100),
            "error_count": len(error_pages),
            "detected_at": datetime.now(timezone.utc).isoformat(),
        })

    if critical_rate > 0.2:  # More than 20% critical
        anomalies.append({
            "type": "high_critical_rate",
            "severity": "warning",
            "message": (
                f"Many pages with critical performance: {len(critical_pages)}/{total_pages} "
                f"({critical_rate * 100:.0f}%)"
            ),
            "value": round(critical_rate * 100),
            "critical_count": len(critical_pages),
            "detected_at": datetime.now(timezone.utc).isoformat(),
        })

    return anomalies


def generate_alerts(anomalies: list) -> list:
    """Convert anomalies into alert objects with severity, message, and timestamp."""
    alerts = []
    for anomaly in anomalies:
        alerts.append({
            "severity": anomaly.get("severity", "info"),
            "type": anomaly.get("type", "unknown"),
            "message": anomaly.get("message", "Unknown anomaly"),
            "timestamp": anomaly.get("detected_at", datetime.now(timezone.utc).isoformat()),
            "details": {k: v for k, v in anomaly.items() if k not in ("severity", "message", "detected_at", "type")},
        })

    # Sort by severity: critical first, then warning, then info
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    alerts.sort(key=lambda a: severity_order.get(a["severity"], 3))

    return alerts


def get_anomaly_summary(anomalies: list, alerts: list) -> dict:
    """Return current anomaly status summary."""
    critical_count = sum(1 for a in alerts if a["severity"] == "critical")
    warning_count = sum(1 for a in alerts if a["severity"] == "warning")
    info_count = sum(1 for a in alerts if a["severity"] == "info")

    if critical_count > 0:
        status = "critical"
    elif warning_count > 0:
        status = "warning"
    else:
        status = "healthy"

    return {
        "status": status,
        "total_anomalies": len(anomalies),
        "critical_count": critical_count,
        "warning_count": warning_count,
        "info_count": info_count,
        "anomalies": anomalies,
        "alerts": alerts,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
