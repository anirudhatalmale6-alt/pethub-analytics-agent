import json
import os
import logging
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import settings
from stats_collector import collect_all_stats, fetch_wp_stats, build_page_inventory
from indexing_checker import check_all_indexing
from performance_checker import check_all_performance
from manager_client import heartbeat, create_task, update_task, update_kpi, log_message

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("analytics-agent")

scheduler = AsyncIOScheduler()

agent_state = {
    "last_collection": None,
    "last_indexing": None,
    "last_performance": None,
    "running": False,
    "running_task": None,
    "history": [],
    "traffic": {
        "views_today": 0,
        "views_yesterday": 0,
        "views_week": 0,
        "views_month": 0,
        "visitors_today": 0,
        "visitors_week": 0,
        "top_pages": [],
        "referrers": [],
        "search_terms": [],
        "source": "none",
        "fetched_at": None,
    },
    "inventory": [],
    "indexing": None,
    "performance": None,
    "traffic_history": [],
}


def load_state():
    os.makedirs(os.path.dirname(settings.DB_PATH), exist_ok=True)
    if os.path.exists(settings.DB_PATH):
        try:
            with open(settings.DB_PATH, "r") as f:
                data = json.load(f)
                agent_state["last_collection"] = data.get("last_collection")
                agent_state["last_indexing"] = data.get("last_indexing")
                agent_state["last_performance"] = data.get("last_performance")
                agent_state["history"] = data.get("history", [])
                agent_state["traffic"] = data.get("traffic", agent_state["traffic"])
                agent_state["inventory"] = data.get("inventory", [])
                agent_state["indexing"] = data.get("indexing")
                agent_state["performance"] = data.get("performance")
                agent_state["traffic_history"] = data.get("traffic_history", [])
        except Exception:
            pass


def save_state():
    os.makedirs(os.path.dirname(settings.DB_PATH), exist_ok=True)
    with open(settings.DB_PATH, "w") as f:
        json.dump({
            "last_collection": agent_state["last_collection"],
            "last_indexing": agent_state["last_indexing"],
            "last_performance": agent_state["last_performance"],
            "history": agent_state["history"][-50:],
            "traffic": agent_state["traffic"],
            "inventory": agent_state["inventory"],
            "indexing": agent_state["indexing"],
            "performance": agent_state["performance"],
            "traffic_history": agent_state["traffic_history"][-90:],
        }, f, default=str)


async def send_heartbeat():
    metrics = {
        "tasks_completed": len(agent_state["history"]),
        "tasks_failed": 0,
        "avg_latency_ms": 0,
    }
    if agent_state["traffic"]:
        metrics["traffic_weekly"] = agent_state["traffic"].get("views_week", 0)
    if agent_state["indexing"]:
        metrics["pages_indexed"] = agent_state["indexing"].get("indexable_count", 0)
    if agent_state["performance"]:
        metrics["avg_response_time"] = agent_state["performance"].get("avg_response_time_ms", 0)
    await heartbeat("active", metrics)


async def run_scheduled_collection():
    """Full data collection: traffic stats + page inventory + performance."""
    if agent_state["running"]:
        logger.info("Collection already running, skipping")
        return

    agent_state["running"] = True
    agent_state["running_task"] = "collection"
    task = await create_task("Scheduled Analytics Collection", "data_collection", priority="normal")
    task_id = task["id"] if task else None

    if task_id:
        await update_task(task_id, "in_progress")

    try:
        await log_message("info", "Starting scheduled analytics collection")

        # Collect stats and inventory
        collection = await collect_all_stats()

        agent_state["traffic"] = collection["traffic"]
        agent_state["inventory"] = collection["inventory"]
        agent_state["last_collection"] = datetime.now(timezone.utc).isoformat()

        # Record traffic snapshot in history
        agent_state["traffic_history"].append({
            "date": datetime.now(timezone.utc).isoformat(),
            "views_today": collection["traffic"].get("views_today", 0),
            "visitors_today": collection["traffic"].get("visitors_today", 0),
            "views_week": collection["traffic"].get("views_week", 0),
            "total_pages": collection["total_pages"],
        })

        # Run performance checks
        perf = await check_all_performance(collection["inventory"])
        agent_state["performance"] = perf
        agent_state["last_performance"] = datetime.now(timezone.utc).isoformat()

        # Record in history
        agent_state["history"].append({
            "date": datetime.now(timezone.utc).isoformat(),
            "type": "collection",
            "total_pages": collection["total_pages"],
            "views_week": collection["traffic"].get("views_week", 0),
            "avg_response_time_ms": perf.get("avg_response_time_ms", 0),
        })

        save_state()

        # Report KPIs
        await update_kpi("traffic_weekly", collection["traffic"].get("views_week", 0))
        if agent_state["indexing"]:
            await update_kpi("pages_indexed", agent_state["indexing"].get("indexable_count", 0))
        await update_kpi("avg_response_time", perf.get("avg_response_time_ms", 0))

        summary = {
            "total_pages": collection["total_pages"],
            "views_week": collection["traffic"].get("views_week", 0),
            "avg_response_time_ms": perf.get("avg_response_time_ms", 0),
            "slow_pages": perf.get("slow_pages_count", 0),
        }

        if task_id:
            await update_task(task_id, "completed", output_data=summary)

        await log_message(
            "info",
            f"Analytics collection complete: {collection['total_pages']} pages, "
            f"{collection['traffic'].get('views_week', 0)} weekly views, "
            f"avg {perf.get('avg_response_time_ms', 0)}ms response"
        )

    except Exception as e:
        logger.error(f"Collection failed: {e}")
        if task_id:
            await update_task(task_id, "failed", error_message=str(e))
        await log_message("error", f"Analytics collection failed: {e}")
    finally:
        agent_state["running"] = False
        agent_state["running_task"] = None


async def run_scheduled_indexing():
    """Weekly indexing check for all pages."""
    if agent_state["running"]:
        logger.info("Another task running, skipping indexing check")
        return

    agent_state["running"] = True
    agent_state["running_task"] = "indexing"
    task = await create_task("Weekly Indexing Check", "indexing_check", priority="normal")
    task_id = task["id"] if task else None

    if task_id:
        await update_task(task_id, "in_progress")

    try:
        await log_message("info", "Starting weekly indexing check")

        # Use existing inventory or fetch fresh
        inventory = agent_state["inventory"]
        if not inventory:
            inventory = await build_page_inventory()
            agent_state["inventory"] = inventory

        indexing = await check_all_indexing(inventory)
        agent_state["indexing"] = indexing
        agent_state["last_indexing"] = datetime.now(timezone.utc).isoformat()

        agent_state["history"].append({
            "date": datetime.now(timezone.utc).isoformat(),
            "type": "indexing",
            "total_pages": indexing.get("total_pages", 0),
            "indexable_count": indexing.get("indexable_count", 0),
            "issues": indexing.get("total_issues", 0),
        })

        save_state()

        await update_kpi("pages_indexed", indexing.get("indexable_count", 0))

        summary = {
            "total_pages": indexing.get("total_pages", 0),
            "indexable": indexing.get("indexable_count", 0),
            "not_indexable": indexing.get("not_indexable_count", 0),
            "issues": indexing.get("total_issues", 0),
        }

        if task_id:
            await update_task(task_id, "completed", output_data=summary)

        await log_message(
            "info",
            f"Indexing check complete: {indexing.get('indexable_count', 0)}/{indexing.get('total_pages', 0)} indexable"
        )

    except Exception as e:
        logger.error(f"Indexing check failed: {e}")
        if task_id:
            await update_task(task_id, "failed", error_message=str(e))
        await log_message("error", f"Indexing check failed: {e}")
    finally:
        agent_state["running"] = False
        agent_state["running_task"] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_state()
    scheduler.add_job(send_heartbeat, "interval", seconds=settings.HEARTBEAT_INTERVAL, id="heartbeat")
    scheduler.add_job(run_scheduled_collection, "cron", hour=str(settings.COLLECTION_HOUR), id="daily_collection")
    scheduler.add_job(run_scheduled_indexing, "cron", day_of_week="mon", hour="5", id="weekly_indexing")
    scheduler.start()
    await send_heartbeat()
    await log_message("info", "Analytics Agent started")
    logger.info("Analytics Agent started on port %d", settings.API_PORT)
    yield
    scheduler.shutdown()


app = FastAPI(
    title="PetHub Analytics Agent",
    description="Site analytics, performance monitoring, and indexing tracking for pethubonline.com",
    version="1.0.0",
    lifespan=lifespan,
    root_path="/agents/analytics"
)


# ─── API Endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    return {
        "agent": "analytics",
        "status": "running" if agent_state["running"] else "idle",
        "running_task": agent_state["running_task"],
        "last_collection": agent_state["last_collection"],
        "last_indexing": agent_state["last_indexing"],
        "last_performance": agent_state["last_performance"],
        "total_pages": len(agent_state["inventory"]),
        "views_week": agent_state["traffic"].get("views_week", 0),
        "indexable_count": agent_state["indexing"].get("indexable_count", 0) if agent_state["indexing"] else 0,
        "avg_response_time_ms": agent_state["performance"].get("avg_response_time_ms", 0) if agent_state["performance"] else 0,
        "history_count": len(agent_state["history"]),
    }


@app.post("/api/collect/run")
async def trigger_collection():
    if agent_state["running"]:
        raise HTTPException(409, "A task is already running")
    import asyncio
    asyncio.create_task(run_scheduled_collection())
    return {"message": "Data collection started", "status": "running"}


@app.get("/api/traffic")
async def get_traffic():
    traffic = agent_state["traffic"]
    return {
        "views_today": traffic.get("views_today", 0),
        "views_yesterday": traffic.get("views_yesterday", 0),
        "views_week": traffic.get("views_week", 0),
        "views_month": traffic.get("views_month", 0),
        "visitors_today": traffic.get("visitors_today", 0),
        "visitors_week": traffic.get("visitors_week", 0),
        "top_pages": traffic.get("top_pages", []),
        "referrers": traffic.get("referrers", []),
        "search_terms": traffic.get("search_terms", []),
        "source": traffic.get("source", "none"),
        "fetched_at": traffic.get("fetched_at"),
        "history": agent_state["traffic_history"][-30:],
    }


@app.get("/api/indexing")
async def get_indexing():
    if not agent_state["indexing"]:
        return {
            "message": "No indexing data yet. Trigger a collection or wait for the weekly check.",
            "total_pages": 0,
            "indexable_count": 0,
            "results": [],
        }
    idx = agent_state["indexing"]
    return {
        "total_pages": idx.get("total_pages", 0),
        "indexable_count": idx.get("indexable_count", 0),
        "not_indexable_count": idx.get("not_indexable_count", 0),
        "sitemap_accessible": idx.get("sitemap_accessible", False),
        "sitemap_url_count": idx.get("sitemap_url_count", 0),
        "total_issues": idx.get("total_issues", 0),
        "checked_at": idx.get("checked_at"),
        "results": [{
            "page_id": r.get("page_id", 0),
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "type": r.get("type", "page"),
            "status_code": r.get("status_code", 0),
            "is_live": r.get("is_live", False),
            "is_indexable": r.get("is_indexable", False),
            "has_noindex": r.get("has_noindex", False),
            "has_canonical": r.get("has_canonical", False),
            "in_sitemap": r.get("in_sitemap", False),
            "is_placeholder": r.get("is_placeholder", False),
            "issues": r.get("issues", []),
        } for r in idx.get("results", [])],
    }


@app.post("/api/indexing/run")
async def trigger_indexing():
    if agent_state["running"]:
        raise HTTPException(409, "A task is already running")
    import asyncio
    asyncio.create_task(run_scheduled_indexing())
    return {"message": "Indexing check started", "status": "running"}


@app.get("/api/performance")
async def get_performance():
    if not agent_state["performance"]:
        return {
            "message": "No performance data yet. Trigger a collection first.",
            "total_pages": 0,
            "results": [],
        }
    perf = agent_state["performance"]
    return {
        "total_pages": perf.get("total_pages", 0),
        "avg_response_time_ms": perf.get("avg_response_time_ms", 0),
        "avg_page_size_display": perf.get("avg_page_size_display", "0 B"),
        "slow_pages_count": perf.get("slow_pages_count", 0),
        "large_pages_count": perf.get("large_pages_count", 0),
        "good_count": perf.get("good_count", 0),
        "warning_count": perf.get("warning_count", 0),
        "critical_count": perf.get("critical_count", 0),
        "total_issues": perf.get("total_issues", 0),
        "checked_at": perf.get("checked_at"),
        "results": [{
            "page_id": r.get("page_id", 0),
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "type": r.get("type", "page"),
            "status_code": r.get("status_code", 0),
            "response_time_ms": r.get("response_time_ms", 0),
            "ttfb_ms": r.get("ttfb_ms", 0),
            "page_size_display": r.get("page_size_display", "0 B"),
            "page_size_bytes": r.get("page_size_bytes", 0),
            "is_gzipped": r.get("is_gzipped", False),
            "has_cache_headers": r.get("has_cache_headers", False),
            "grade": r.get("grade", "unknown"),
            "issues": r.get("issues", []),
        } for r in perf.get("results", [])],
    }


@app.get("/api/freshness")
async def get_freshness():
    """Return content freshness for all pages, flagging stale content."""
    now = datetime.now(timezone.utc)
    stale_threshold = now - timedelta(days=settings.STALE_DAYS)
    inventory = agent_state["inventory"]

    if not inventory:
        return {
            "message": "No page data yet. Trigger a collection first.",
            "total_pages": 0,
            "results": [],
        }

    results = []
    stale_count = 0
    fresh_count = 0

    for page in inventory:
        modified_str = page.get("date_modified", "")
        published_str = page.get("date_published", "")

        try:
            modified_dt = datetime.fromisoformat(modified_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            modified_dt = None

        try:
            published_dt = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            published_dt = None

        if modified_dt:
            days_since_modified = (now - modified_dt).days
        else:
            days_since_modified = -1

        is_stale = days_since_modified > settings.STALE_DAYS if days_since_modified >= 0 else False

        if is_stale:
            stale_count += 1
        else:
            fresh_count += 1

        results.append({
            "page_id": page.get("id", 0),
            "title": page.get("title", ""),
            "url": page.get("url", ""),
            "type": page.get("type", "page"),
            "slug": page.get("slug", ""),
            "date_published": published_str,
            "date_modified": modified_str,
            "days_since_modified": days_since_modified,
            "is_stale": is_stale,
            "word_count": page.get("word_count", 0),
        })

    # Sort: stale first, then by days since modified descending
    results.sort(key=lambda x: (-1 if x["is_stale"] else 0, -x["days_since_modified"]))

    return {
        "total_pages": len(results),
        "fresh_count": fresh_count,
        "stale_count": stale_count,
        "stale_threshold_days": settings.STALE_DAYS,
        "results": results,
    }


@app.get("/api/history")
async def get_history():
    return {
        "history": agent_state["history"][-50:],
        "traffic_history": agent_state["traffic_history"][-30:],
    }


@app.get("/", response_class=HTMLResponse)
async def analytics_dashboard():
    template_path = os.path.join(os.path.dirname(__file__), "templates", "analytics_dashboard.html")
    with open(template_path, "r") as f:
        return HTMLResponse(f.read())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=settings.API_PORT, reload=False)
