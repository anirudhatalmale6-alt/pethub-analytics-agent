"""OpenAI GPT integration for the Analytics Agent.

Uses httpx to call OpenAI API directly (no openai SDK dependency).
Provides AI-powered weekly report generation and anomaly explanations.
"""

import logging
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger("analytics.ai")

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
TIMEOUT = 15.0

SYSTEM_PROMPT = (
    "You are a data analyst for Pet Hub Online (pethubonline.com), "
    "a UK-based pet supplies affiliate website. You provide clear, concise, "
    "actionable insights from website metrics. You focus on what matters for "
    "affiliate revenue growth."
)


async def _call_openai(
    messages: list[dict],
    model: str = "gpt-4o-mini",
    temperature: float = 0.4,
    max_tokens: int = 500,
) -> Optional[str]:
    """Low-level helper to call the OpenAI chat completions endpoint."""
    headers = {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(OPENAI_API_URL, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except httpx.TimeoutException:
        logger.error("OpenAI API timeout after %.0fs", TIMEOUT)
        return None
    except httpx.HTTPStatusError as exc:
        logger.error("OpenAI API HTTP %d: %s", exc.response.status_code, exc.response.text[:300])
        return None
    except Exception as exc:
        logger.error("OpenAI API unexpected error: %s", exc)
        return None


async def ai_generate_weekly_report(metrics: dict) -> Optional[str]:
    """Generate a natural language weekly performance summary.

    Args:
        metrics: Dict containing keys like:
            - traffic: total page views this week
            - top_pages: list of top performing page titles
            - social_engagement: dict with likes, shares, comments counts
            - seo_scores: dict with average score and pages audited
            - issues_count: number of issues detected this week

    Returns:
        A 3-5 sentence summary paragraph, or None on failure.
    """
    user_prompt = (
        "Here are this week's metrics for Pet Hub Online:\n\n"
        f"Traffic (page views): {metrics.get('traffic', 'N/A')}\n"
        f"Top pages: {', '.join(metrics.get('top_pages', ['N/A']))}\n"
        f"Social engagement: {metrics.get('social_engagement', 'N/A')}\n"
        f"SEO scores: {metrics.get('seo_scores', 'N/A')}\n"
        f"Issues detected: {metrics.get('issues_count', 'N/A')}\n\n"
        "Write a concise weekly summary (3-5 sentences). Highlight the biggest win, "
        "any concerns, and one specific recommendation for next week. "
        "Use plain English, no bullet points.\n\n"
        "Return ONLY the summary paragraph, nothing else."
    )

    return await _call_openai(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )


async def ai_explain_anomaly(
    metric_name: str,
    current_value: float,
    average_value: float,
    direction: str,
) -> Optional[str]:
    """Explain a detected anomaly in a metric in plain English.

    Args:
        metric_name: Name of the metric (e.g. "page_views", "bounce_rate").
        current_value: The current/anomalous value.
        average_value: The typical/average value.
        direction: "up" or "down" indicating the direction of the anomaly.

    Returns:
        A 1-2 sentence explanation, or None on failure.
    """
    pct_change = ((current_value - average_value) / average_value * 100) if average_value else 0

    user_prompt = (
        f"Metric: {metric_name}\n"
        f"Current value: {current_value}\n"
        f"Normal average: {average_value}\n"
        f"Direction: {direction} ({pct_change:+.1f}% change)\n\n"
        "Explain this anomaly in 1-2 sentences for a website owner. "
        "Suggest the most likely cause and whether immediate action is needed. "
        "Keep it practical and jargon-free.\n\n"
        "Return ONLY the explanation, nothing else."
    )

    return await _call_openai(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=150,
    )
