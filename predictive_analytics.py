"""
Enhanced predictive analytics for the Analytics Agent.

Provides AI-powered forecasting, multi-metric anomaly detection,
content ROI scoring, and insight generation for PetHub Online.
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

logger = logging.getLogger("analytics.predictive")

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"


# ---------------------------------------------------------------------------
#  Linear regression helper (self-contained, no numpy dependency)
# ---------------------------------------------------------------------------

def _linear_regression(x: list[float], y: list[float]) -> dict:
    """Simple OLS linear regression returning slope, intercept, R-squared."""
    n = len(x)
    if n < 2:
        return {"slope": 0.0, "intercept": 0.0, "r_squared": 0.0}

    sum_x = sum(x)
    sum_y = sum(y)
    sum_xy = sum(xi * yi for xi, yi in zip(x, y))
    sum_x2 = sum(xi ** 2 for xi in x)

    denom = n * sum_x2 - sum_x ** 2
    if denom == 0:
        return {"slope": 0.0, "intercept": sum_y / n if n else 0.0, "r_squared": 0.0}

    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n

    y_mean = sum_y / n
    ss_tot = sum((yi - y_mean) ** 2 for yi in y)
    ss_res = sum((yi - (slope * xi + intercept)) ** 2 for xi, yi in zip(x, y))
    r_squared = max(0.0, min(1.0, 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0))

    return {"slope": slope, "intercept": intercept, "r_squared": r_squared}


# ---------------------------------------------------------------------------
#  Traffic forecasting
# ---------------------------------------------------------------------------

def forecast_traffic(history: list[dict], days_ahead: int = 7) -> dict:
    """Forecast traffic for the next N days using trend analysis.

    Args:
        history: List of dicts with "date" (str), "views" (int),
                 and optionally "visitors" (int).
        days_ahead: Number of days to project forward (default 7).

    Returns:
        {
            "predictions": [{"date": str, "predicted_views": int}],
            "trend": str ("growing"|"stable"|"declining"),
            "growth_rate_pct": float,
            "confidence": str ("low"|"medium"|"high"),
        }
    """
    if not history or len(history) < 3:
        return {
            "predictions": [],
            "trend": "unknown",
            "growth_rate_pct": 0.0,
            "confidence": "low",
            "message": "Need at least 3 data points for forecasting",
        }

    # Use last 30 entries (or fewer if not available)
    recent = history[-30:]
    views = [entry.get("views", 0) for entry in recent]

    x = list(range(len(views)))
    y = [float(v) for v in views]

    reg = _linear_regression(x, y)
    slope = reg["slope"]
    r_squared = reg["r_squared"]

    # Determine trend direction
    avg_views = sum(views) / len(views) if views else 1
    if avg_views == 0:
        avg_views = 1
    weekly_growth_pct = (slope * 7 / avg_views) * 100

    if weekly_growth_pct > 5:
        trend = "growing"
    elif weekly_growth_pct < -5:
        trend = "declining"
    else:
        trend = "stable"

    # Confidence based on R-squared and data points
    if r_squared >= 0.7 and len(views) >= 14:
        confidence = "high"
    elif r_squared >= 0.4 and len(views) >= 7:
        confidence = "medium"
    else:
        confidence = "low"

    # Project forward
    predictions: list[dict] = []
    now = datetime.now(timezone.utc)
    base_index = len(views) - 1
    for i in range(1, days_ahead + 1):
        day_idx = base_index + i
        predicted = slope * day_idx + reg["intercept"]
        predicted = max(0, round(predicted))
        pred_date = now + timedelta(days=i)
        predictions.append({
            "date": pred_date.strftime("%Y-%m-%d"),
            "predicted_views": predicted,
        })

    return {
        "predictions": predictions,
        "trend": trend,
        "growth_rate_pct": round(weekly_growth_pct, 1),
        "confidence": confidence,
        "r_squared": round(r_squared, 3),
        "data_points_used": len(views),
        "forecasted_at": now.isoformat(),
    }


# ---------------------------------------------------------------------------
#  Multi-metric anomaly detection
# ---------------------------------------------------------------------------

def _mean_and_std(values: list[float]) -> tuple[float, float]:
    """Compute mean and population standard deviation."""
    n = len(values)
    if n == 0:
        return 0.0, 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return mean, variance ** 0.5


def detect_multi_metric_anomalies(metrics: dict) -> list[dict]:
    """Detect anomalies across multiple metrics simultaneously.

    Args:
        metrics: Dict mapping metric name to a list of numeric values:
            {"traffic": [values], "engagement": [values],
             "seo_scores": [values], "page_speed": [values]}

    Returns:
        List of anomaly dicts, each containing metric name, type,
        severity, deviation info, and any correlated anomalies.
    """
    anomalies: list[dict] = []
    anomaly_directions: dict[str, str] = {}  # metric -> "spike"|"drop"

    for metric_name, values in metrics.items():
        if not values or len(values) < 4:
            continue

        fvalues = [float(v) for v in values]
        mean, std = _mean_and_std(fvalues)

        if std == 0:
            continue

        # Check the most recent value against the distribution
        current = fvalues[-1]
        z_score = (current - mean) / std

        if abs(z_score) < 2.0:
            continue  # Within normal range

        direction = "spike" if z_score > 0 else "drop"
        anomaly_directions[metric_name] = direction

        deviation_pct = round(((current - mean) / mean) * 100, 1) if mean else 0.0

        # Severity based on z-score magnitude
        abs_z = abs(z_score)
        if abs_z >= 3.5:
            severity = "critical"
        elif abs_z >= 3.0:
            severity = "high"
        elif abs_z >= 2.5:
            severity = "medium"
        else:
            severity = "low"

        anomalies.append({
            "metric": metric_name,
            "type": direction,
            "severity": severity,
            "value": round(current, 2),
            "expected": round(mean, 2),
            "std_dev": round(std, 2),
            "z_score": round(z_score, 2),
            "deviation_pct": deviation_pct,
            "correlated_with": [],  # Populated below
            "detected_at": datetime.now(timezone.utc).isoformat(),
        })

    # Cross-correlate: flag compound anomalies (same direction = correlated)
    anomaly_names = list(anomaly_directions.keys())
    for anomaly in anomalies:
        metric = anomaly["metric"]
        my_direction = anomaly_directions[metric]
        correlated = [
            other for other in anomaly_names
            if other != metric and anomaly_directions[other] == my_direction
        ]
        if correlated:
            anomaly["correlated_with"] = correlated
            # Compound anomalies are more severe
            if anomaly["severity"] == "low":
                anomaly["severity"] = "medium"
            elif anomaly["severity"] == "medium":
                anomaly["severity"] = "high"

    # Sort by severity (critical first)
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    anomalies.sort(key=lambda a: severity_order.get(a["severity"], 4))

    return anomalies


# ---------------------------------------------------------------------------
#  AI-powered insight generation
# ---------------------------------------------------------------------------

def _get_openai_api_key() -> str:
    """Resolve OpenAI API key from env or config."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        try:
            from config import settings  # type: ignore[import-untyped]
            api_key = settings.OPENAI_API_KEY
        except Exception:
            pass
    return api_key


async def ai_generate_insights(
    metrics_summary: dict,
    anomalies: list[dict],
) -> Optional[list[dict]]:
    """Use AI to generate actionable insights from metrics and anomalies.

    Args:
        metrics_summary: High-level metrics overview (traffic, engagement, etc.)
        anomalies: List of detected anomaly dicts.

    Returns:
        List of insight dicts:
        [{"insight": str, "action": str, "priority": str, "affected_area": str}]
        or None on failure.
    """
    api_key = _get_openai_api_key()
    if not api_key:
        logger.warning("No OpenAI API key available -- skipping AI insight generation")
        return None

    system_prompt = (
        "You are a data analyst for Pet Hub Online (pethubonline.com), "
        "a UK-based pet supplies affiliate website. You provide clear, concise, "
        "actionable insights from website metrics. Focus on revenue impact and "
        "user experience."
    )

    user_prompt = (
        "Here are the latest metrics and detected anomalies for Pet Hub Online:\n\n"
        f"Metrics summary:\n{json.dumps(metrics_summary, indent=2, default=str)}\n\n"
        f"Detected anomalies:\n{json.dumps(anomalies, indent=2, default=str)}\n\n"
        "Generate 3-5 actionable insights. For each insight, return a JSON object with:\n"
        '- "insight": what was observed (1 sentence)\n'
        '- "action": what should be done about it (1 sentence)\n'
        '- "priority": "high", "medium", or "low"\n'
        '- "affected_area": which area (e.g. "seo", "traffic", "content", "performance")\n\n'
        "Return ONLY a JSON array of these objects, no markdown formatting."
    )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.4,
        "max_tokens": 600,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(OPENAI_API_URL, headers=headers, json=payload)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
    except httpx.TimeoutException:
        logger.error("OpenAI API timeout during insight generation")
        return None
    except httpx.HTTPStatusError as exc:
        logger.error("OpenAI API HTTP %d: %s", exc.response.status_code, exc.response.text[:300])
        return None
    except Exception as exc:
        logger.error("OpenAI API unexpected error: %s", exc)
        return None

    # Parse response
    try:
        cleaned = content
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            return parsed
        logger.warning("AI returned non-list for insights: %s", type(parsed))
        return None
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse AI insights JSON: %s", exc)
        return None


# ---------------------------------------------------------------------------
#  Content ROI scoring
# ---------------------------------------------------------------------------

def calculate_content_roi(page_metrics: list[dict]) -> list[dict]:
    """Calculate ROI score for each piece of content.

    Args:
        page_metrics: List of dicts with:
            - title (str): page title
            - url (str): page URL
            - views (int): total page views
            - engagement (float): engagement rate (0-1)
            - seo_score (int): SEO health score (0-100)

    Returns:
        Sorted list (highest ROI first) with added roi_score and
        performance_tier fields.
    """
    if not page_metrics:
        return []

    # Extract raw scores
    raw_scores: list[dict] = []
    for page in page_metrics:
        views = page.get("views", 0)
        engagement = page.get("engagement", 0.0)
        seo_score = page.get("seo_score", 0)

        # Weighted composite (views normalized to 0-1 later)
        raw_scores.append({
            **page,
            "_raw_views": views,
            "_raw_engagement": engagement,
            "_raw_seo": seo_score,
        })

    # Normalize views to 0-1 range
    max_views = max((p["_raw_views"] for p in raw_scores), default=1)
    if max_views == 0:
        max_views = 1

    for page in raw_scores:
        norm_views = page["_raw_views"] / max_views
        norm_engagement = min(1.0, page["_raw_engagement"])
        norm_seo = page["_raw_seo"] / 100.0

        roi_score = round(
            (norm_views * 0.4) + (norm_engagement * 0.3) + (norm_seo * 0.3),
            3,
        )
        page["roi_score"] = roi_score

    # Sort descending by ROI score
    raw_scores.sort(key=lambda p: p["roi_score"], reverse=True)

    # Assign performance tiers
    total = len(raw_scores)
    for idx, page in enumerate(raw_scores):
        percentile = idx / total if total > 1 else 0
        if percentile < 0.15:
            tier = "star"
        elif percentile < 0.50:
            tier = "solid"
        elif percentile < 0.80:
            tier = "underperforming"
        else:
            tier = "critical"
        page["performance_tier"] = tier

    # Clean up internal keys
    for page in raw_scores:
        page.pop("_raw_views", None)
        page.pop("_raw_engagement", None)
        page.pop("_raw_seo", None)

    return raw_scores
