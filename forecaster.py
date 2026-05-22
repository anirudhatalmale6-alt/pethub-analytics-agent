"""
KPI forecasting and trend analysis.
Uses simple linear regression to project traffic and detect trends.
"""

import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("analytics-agent.forecaster")


def _linear_regression(x: list, y: list) -> dict:
    """
    Simple linear regression returning slope, intercept, and R-squared.
    x and y must be equal-length numeric lists.
    """
    n = len(x)
    if n < 2:
        return {"slope": 0, "intercept": 0, "r_squared": 0}

    sum_x = sum(x)
    sum_y = sum(y)
    sum_xy = sum(xi * yi for xi, yi in zip(x, y))
    sum_x2 = sum(xi ** 2 for xi in x)

    denominator = n * sum_x2 - sum_x ** 2
    if denominator == 0:
        return {"slope": 0, "intercept": sum_y / n if n else 0, "r_squared": 0}

    slope = (n * sum_xy - sum_x * sum_y) / denominator
    intercept = (sum_y - slope * sum_x) / n

    # R-squared
    y_mean = sum_y / n
    ss_tot = sum((yi - y_mean) ** 2 for yi in y)
    ss_res = sum((yi - (slope * xi + intercept)) ** 2 for xi, yi in zip(x, y))

    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
    r_squared = max(0, min(1, r_squared))  # Clamp to [0,1]

    return {"slope": slope, "intercept": intercept, "r_squared": r_squared}


def forecast_traffic(traffic_history: list, days_ahead: int = 7) -> dict:
    """
    Simple linear regression on traffic_history to predict next week's traffic.
    Uses the last 30 days of data.
    """
    if not traffic_history:
        return {
            "predictions": [],
            "trend_slope": 0,
            "confidence": 0,
            "message": "No traffic history available for forecasting",
        }

    # Use last 30 days
    recent = traffic_history[-30:]

    # Build x (day index) and y (views) arrays
    views_data = []
    for snap in recent:
        views = snap.get("views_today", 0) or snap.get("views_week", 0) or 0
        views_data.append(views)

    if len(views_data) < 3:
        return {
            "predictions": [],
            "trend_slope": 0,
            "confidence": 0,
            "message": "Not enough data points for forecasting (need at least 3 days)",
        }

    x = list(range(len(views_data)))
    y = views_data

    reg = _linear_regression(x, y)
    slope = reg["slope"]
    intercept = reg["intercept"]
    r_squared = reg["r_squared"]

    # Project forward
    predictions = []
    now = datetime.now(timezone.utc)
    for i in range(1, days_ahead + 1):
        day_index = len(views_data) - 1 + i
        predicted_value = max(0, round(slope * day_index + intercept))
        pred_date = now + timedelta(days=i)
        predictions.append({
            "date": pred_date.strftime("%Y-%m-%d"),
            "predicted_views": predicted_value,
            "day_index": day_index,
        })

    # Confidence label based on R-squared
    if r_squared >= 0.7:
        confidence_label = "high"
    elif r_squared >= 0.4:
        confidence_label = "medium"
    else:
        confidence_label = "low"

    # Current average and projected total
    current_avg = sum(views_data[-7:]) / min(7, len(views_data))
    projected_week_total = sum(p["predicted_views"] for p in predictions[:7])

    return {
        "predictions": predictions,
        "trend_slope": round(slope, 2),
        "r_squared": round(r_squared, 3),
        "confidence": confidence_label,
        "data_points": len(views_data),
        "current_avg_daily": round(current_avg),
        "projected_week_total": projected_week_total,
        "forecasted_at": now.isoformat(),
    }


def analyze_trends(traffic_history: list) -> dict:
    """
    Analyze traffic trends:
    - Overall direction: growing, stable, declining
    - Growth rate (% per week)
    - Best day of week for traffic
    - Patterns
    """
    if not traffic_history or len(traffic_history) < 2:
        return {
            "direction": "unknown",
            "growth_rate_pct": 0,
            "best_day_of_week": None,
            "patterns": [],
            "message": "Not enough data for trend analysis",
        }

    # Extract views
    entries = []
    for snap in traffic_history:
        date_str = snap.get("date", "")
        views = snap.get("views_today", 0) or snap.get("views_week", 0) or 0
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            dt = None
        entries.append({"date": dt, "views": views})

    views_list = [e["views"] for e in entries]

    # Overall direction via linear regression
    x = list(range(len(views_list)))
    reg = _linear_regression(x, views_list)
    slope = reg["slope"]

    avg_views = sum(views_list) / len(views_list) if views_list else 1
    if avg_views == 0:
        avg_views = 1

    # Weekly growth rate
    weekly_slope = slope * 7
    growth_rate_pct = (weekly_slope / avg_views) * 100

    if growth_rate_pct > 5:
        direction = "growing"
    elif growth_rate_pct < -5:
        direction = "declining"
    else:
        direction = "stable"

    # Best day of week
    day_views = {}  # day_of_week -> [views]
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    for entry in entries:
        if entry["date"]:
            dow = entry["date"].weekday()
            if dow not in day_views:
                day_views[dow] = []
            day_views[dow].append(entry["views"])

    best_day = None
    best_avg = 0
    day_averages = {}
    for dow, views in day_views.items():
        avg = sum(views) / len(views)
        day_averages[day_names[dow]] = round(avg)
        if avg > best_avg:
            best_avg = avg
            best_day = day_names[dow]

    # Patterns detection
    patterns = []
    if len(views_list) >= 14:
        # Check for weekend patterns
        weekend_views = []
        weekday_views = []
        for entry in entries:
            if entry["date"]:
                if entry["date"].weekday() >= 5:
                    weekend_views.append(entry["views"])
                else:
                    weekday_views.append(entry["views"])

        if weekend_views and weekday_views:
            weekend_avg = sum(weekend_views) / len(weekend_views)
            weekday_avg = sum(weekday_views) / len(weekday_views)
            if weekday_avg > 0:
                ratio = weekend_avg / weekday_avg
                if ratio > 1.3:
                    patterns.append("Higher traffic on weekends")
                elif ratio < 0.7:
                    patterns.append("Higher traffic on weekdays")

    # Week-over-week comparison
    if len(views_list) >= 14:
        recent_week = views_list[-7:]
        prev_week = views_list[-14:-7]
        recent_sum = sum(recent_week)
        prev_sum = sum(prev_week)
        if prev_sum > 0:
            wow_change = ((recent_sum - prev_sum) / prev_sum) * 100
            if wow_change > 20:
                patterns.append(f"Strong week-over-week growth ({wow_change:.0f}%)")
            elif wow_change < -20:
                patterns.append(f"Significant week-over-week decline ({wow_change:.0f}%)")

    return {
        "direction": direction,
        "growth_rate_pct": round(growth_rate_pct, 1),
        "slope_per_day": round(slope, 2),
        "r_squared": round(reg["r_squared"], 3),
        "avg_daily_views": round(avg_views),
        "best_day_of_week": best_day,
        "day_averages": day_averages,
        "patterns": patterns,
        "data_points": len(views_list),
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
    }


def get_kpi_dashboard(state: dict, forecast: dict = None, trends: dict = None, agent_scores: dict = None) -> dict:
    """
    Compile all KPIs into a dashboard view:
    - Current traffic vs target
    - Week-over-week growth
    - Page performance trends
    - Indexing coverage
    - Agent ecosystem health
    """
    traffic = state.get("traffic", {})
    perf = state.get("performance")
    indexing = state.get("indexing")
    traffic_history = state.get("traffic_history", [])

    # Traffic KPIs
    views_week = traffic.get("views_week", 0)
    visitors_week = traffic.get("visitors_week", 0)

    # Week-over-week change
    wow_change = 0
    if len(traffic_history) >= 2:
        # Compare last entry's weekly views with an earlier entry
        if len(traffic_history) >= 8:
            prev_views = traffic_history[-8].get("views_week", 0)
        else:
            prev_views = traffic_history[0].get("views_week", 0)
        if prev_views > 0:
            wow_change = round(((views_week - prev_views) / prev_views) * 100, 1)

    # Performance KPIs
    avg_response_time = perf.get("avg_response_time_ms", 0) if perf else 0
    perf_good_pct = 0
    if perf and perf.get("total_pages", 0) > 0:
        perf_good_pct = round((perf.get("good_count", 0) / perf["total_pages"]) * 100)

    # Indexing coverage
    indexing_coverage = 0
    if indexing and indexing.get("total_pages", 0) > 0:
        indexing_coverage = round(
            (indexing.get("indexable_count", 0) / indexing["total_pages"]) * 100
        )

    # Agent ecosystem health
    ecosystem_score = 0
    ecosystem_grade = "N/A"
    if agent_scores:
        ecosystem_score = agent_scores.get("overall_score", 0)
        ecosystem_grade = agent_scores.get("overall_grade", "N/A")

    kpis = {
        "traffic": {
            "views_week": views_week,
            "visitors_week": visitors_week,
            "views_today": traffic.get("views_today", 0),
            "views_month": traffic.get("views_month", 0),
            "wow_change_pct": wow_change,
        },
        "performance": {
            "avg_response_time_ms": avg_response_time,
            "good_pages_pct": perf_good_pct,
            "total_issues": perf.get("total_issues", 0) if perf else 0,
        },
        "indexing": {
            "coverage_pct": indexing_coverage,
            "total_pages": indexing.get("total_pages", 0) if indexing else 0,
            "indexable": indexing.get("indexable_count", 0) if indexing else 0,
        },
        "ecosystem": {
            "score": ecosystem_score,
            "grade": ecosystem_grade,
        },
        "forecast": {
            "trend": forecast.get("confidence", "unknown") if forecast else "unknown",
            "projected_week_total": forecast.get("projected_week_total", 0) if forecast else 0,
            "slope": forecast.get("trend_slope", 0) if forecast else 0,
        },
        "trends": {
            "direction": trends.get("direction", "unknown") if trends else "unknown",
            "growth_rate_pct": trends.get("growth_rate_pct", 0) if trends else 0,
            "best_day": trends.get("best_day_of_week") if trends else None,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    return kpis
