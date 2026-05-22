"""
Agent performance scoring system.
Scores each agent on uptime, activity, task completion, and errors.
"""

import logging
from datetime import datetime, timezone, timedelta
import httpx

logger = logging.getLogger("analytics-agent.scorer")

AGENT_CONFIGS = {
    "seo": {"url": "http://127.0.0.1:8101/agents/seo/api/status", "port": 8101},
    "analytics": {"url": None, "port": 8102},  # Self-assessment
    "social": {"url": "http://127.0.0.1:8103/agents/social/api/status", "port": 8103},
    "maintenance": {"url": "http://127.0.0.1:8104/agents/maintenance/api/status", "port": 8104},
}


def _grade_from_score(score: int) -> str:
    """Return letter grade from numeric score."""
    if score >= 90:
        return "A"
    elif score >= 75:
        return "B"
    elif score >= 60:
        return "C"
    else:
        return "D"


async def score_agent(agent_name: str, agent_url: str) -> dict:
    """
    Score a single agent's effectiveness (0-100):
    - Uptime (25 pts): is it responding?
    - Last activity (25 pts): how recently did it do something?
    - Task completion (25 pts): check task history
    - Error rate (25 pts): check error counts
    """
    result = {
        "agent": agent_name,
        "score": 0,
        "grade": "D",
        "uptime_score": 0,
        "activity_score": 0,
        "task_score": 0,
        "error_score": 0,
        "status": "unknown",
        "last_activity": None,
        "details": {},
        "scored_at": datetime.now(timezone.utc).isoformat(),
    }

    # ── Uptime check (25 pts) ──
    agent_status = None
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(agent_url)
            if resp.status_code == 200:
                agent_status = resp.json()
                result["uptime_score"] = 25
                result["status"] = agent_status.get("status", "active")
                result["details"]["reachable"] = True
            else:
                result["uptime_score"] = 0
                result["status"] = "error"
                result["details"]["reachable"] = False
                result["details"]["status_code"] = resp.status_code
    except Exception as e:
        result["uptime_score"] = 0
        result["status"] = "unreachable"
        result["details"]["reachable"] = False
        result["details"]["error"] = str(e)[:100]

    if not agent_status:
        # Can't score further without status data
        result["score"] = result["uptime_score"]
        result["grade"] = _grade_from_score(result["score"])
        return result

    # ── Last Activity (25 pts) ──
    now = datetime.now(timezone.utc)
    last_activity = None

    # Check common timestamp fields
    for field in ("last_collection", "last_check", "last_run", "last_scan", "last_post"):
        ts = agent_status.get(field)
        if ts:
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                if last_activity is None or dt > last_activity:
                    last_activity = dt
            except (ValueError, AttributeError):
                pass

    if last_activity:
        result["last_activity"] = last_activity.isoformat()
        hours_since = (now - last_activity).total_seconds() / 3600
        if hours_since <= 6:
            result["activity_score"] = 25
        elif hours_since <= 24:
            result["activity_score"] = 20
        elif hours_since <= 48:
            result["activity_score"] = 15
        elif hours_since <= 168:  # 1 week
            result["activity_score"] = 10
        else:
            result["activity_score"] = 5
    else:
        result["activity_score"] = 10  # Unknown, give partial credit

    # ── Task Completion (25 pts) ──
    history_count = agent_status.get("history_count", 0)
    tasks_completed = agent_status.get("tasks_completed", history_count)

    if tasks_completed >= 10:
        result["task_score"] = 25
    elif tasks_completed >= 5:
        result["task_score"] = 20
    elif tasks_completed >= 1:
        result["task_score"] = 15
    else:
        result["task_score"] = 5

    result["details"]["tasks_completed"] = tasks_completed

    # ── Error Rate (25 pts) ──
    tasks_failed = agent_status.get("tasks_failed", 0)
    total_tasks = max(tasks_completed + tasks_failed, 1)
    error_rate = tasks_failed / total_tasks

    if error_rate == 0:
        result["error_score"] = 25
    elif error_rate < 0.05:
        result["error_score"] = 20
    elif error_rate < 0.1:
        result["error_score"] = 15
    elif error_rate < 0.25:
        result["error_score"] = 10
    else:
        result["error_score"] = 5

    result["details"]["tasks_failed"] = tasks_failed
    result["details"]["error_rate_pct"] = round(error_rate * 100, 1)

    # ── Total Score ──
    result["score"] = (
        result["uptime_score"]
        + result["activity_score"]
        + result["task_score"]
        + result["error_score"]
    )
    result["grade"] = _grade_from_score(result["score"])

    return result


async def score_analytics_self(state: dict) -> dict:
    """Self-assessment for the Analytics agent based on internal state."""
    now = datetime.now(timezone.utc)
    result = {
        "agent": "analytics",
        "score": 0,
        "grade": "D",
        "uptime_score": 25,  # We're running if this code executes
        "activity_score": 0,
        "task_score": 0,
        "error_score": 0,
        "status": "active",
        "last_activity": None,
        "details": {"reachable": True},
        "scored_at": now.isoformat(),
    }

    # Activity score
    last_collection = state.get("last_collection")
    if last_collection:
        result["last_activity"] = last_collection
        try:
            dt = datetime.fromisoformat(str(last_collection).replace("Z", "+00:00"))
            hours_since = (now - dt).total_seconds() / 3600
            if hours_since <= 6:
                result["activity_score"] = 25
            elif hours_since <= 24:
                result["activity_score"] = 20
            elif hours_since <= 48:
                result["activity_score"] = 15
            elif hours_since <= 168:
                result["activity_score"] = 10
            else:
                result["activity_score"] = 5
        except (ValueError, AttributeError):
            result["activity_score"] = 10

    # Task score
    history = state.get("history", [])
    tasks_completed = len(history)
    if tasks_completed >= 10:
        result["task_score"] = 25
    elif tasks_completed >= 5:
        result["task_score"] = 20
    elif tasks_completed >= 1:
        result["task_score"] = 15
    else:
        result["task_score"] = 5
    result["details"]["tasks_completed"] = tasks_completed

    # Error score (analytics agent tracks errors in history)
    failed = sum(1 for h in history if h.get("type") == "error")
    total = max(len(history), 1)
    error_rate = failed / total
    if error_rate == 0:
        result["error_score"] = 25
    elif error_rate < 0.05:
        result["error_score"] = 20
    elif error_rate < 0.1:
        result["error_score"] = 15
    else:
        result["error_score"] = 10
    result["details"]["tasks_failed"] = failed
    result["details"]["error_rate_pct"] = round(error_rate * 100, 1)

    result["score"] = (
        result["uptime_score"]
        + result["activity_score"]
        + result["task_score"]
        + result["error_score"]
    )
    result["grade"] = _grade_from_score(result["score"])

    return result


async def score_all_agents(state: dict) -> dict:
    """Score all 4 agents and compute overall ecosystem health."""
    scores = {}

    for agent_name, config in AGENT_CONFIGS.items():
        if agent_name == "analytics":
            scores[agent_name] = await score_analytics_self(state)
        else:
            scores[agent_name] = await score_agent(agent_name, config["url"])

    # Overall ecosystem health
    all_scores = [s["score"] for s in scores.values()]
    overall_score = round(sum(all_scores) / max(len(all_scores), 1))
    overall_grade = _grade_from_score(overall_score)

    reachable_count = sum(1 for s in scores.values() if s.get("details", {}).get("reachable", False))

    return {
        "agents": scores,
        "overall_score": overall_score,
        "overall_grade": overall_grade,
        "agents_online": reachable_count,
        "agents_total": len(AGENT_CONFIGS),
        "scored_at": datetime.now(timezone.utc).isoformat(),
    }
