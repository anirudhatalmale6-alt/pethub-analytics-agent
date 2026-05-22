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
from anomaly_detector import (
    detect_traffic_anomalies,
    detect_performance_anomalies,
    generate_alerts,
    get_anomaly_summary,
)
from report_generator import generate_weekly_report, format_report_text
from agent_scorer import score_all_agents
from forecaster import forecast_traffic, analyze_trends, get_kpi_dashboard

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
    # Phase 2D additions
    "anomalies": None,
    "weekly_reports": [],
    "agent_scores": None,
    "forecasts": None,
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
                # Phase 2D state
                agent_state["anomalies"] = data.get("anomalies")
                agent_state["weekly_reports"] = data.get("weekly_reports", [])
                agent_state["agent_scores"] = data.get("agent_scores")
                agent_state["forecasts"] = data.get("forecasts")
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
            # Phase 2D state
            "anomalies": agent_state["anomalies"],
            "weekly_reports": agent_state["weekly_reports"][-12:],
            "agent_scores": agent_state["agent_scores"],
            "forecasts": agent_state["forecasts"],
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


async def run_anomaly_detection():
    """Run anomaly detection on current data."""
    try:
        logger.info("Running anomaly detection...")
        traffic_anomalies = detect_traffic_anomalies(agent_state["traffic_history"])
        perf_anomalies = detect_performance_anomalies(
            agent_state["performance"],
            agent_state["history"],
        )
        all_anomalies = traffic_anomalies + perf_anomalies
        alerts = generate_alerts(all_anomalies)
        summary = get_anomaly_summary(all_anomalies, alerts)
        agent_state["anomalies"] = summary
        save_state()
        logger.info(
            f"Anomaly detection complete: {summary['total_anomalies']} anomalies, "
            f"{summary['critical_count']} critical"
        )
    except Exception as e:
        logger.error(f"Anomaly detection failed: {e}")


async def run_forecasting():
    """Run traffic forecasting and trend analysis."""
    try:
        logger.info("Running forecasting...")
        forecast = forecast_traffic(agent_state["traffic_history"])
        trends = analyze_trends(agent_state["traffic_history"])
        kpis = get_kpi_dashboard(
            agent_state,
            forecast=forecast,
            trends=trends,
            agent_scores=agent_state.get("agent_scores"),
        )
        agent_state["forecasts"] = {
            "forecast": forecast,
            "trends": trends,
            "kpis": kpis,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        save_state()
        logger.info("Forecasting complete")
    except Exception as e:
        logger.error(f"Forecasting failed: {e}")


async def run_agent_scoring():
    """Score all agents in the ecosystem."""
    try:
        logger.info("Running agent scoring...")
        scores = await score_all_agents(agent_state)
        agent_state["agent_scores"] = scores
        save_state()
        logger.info(
            f"Agent scoring complete: overall {scores['overall_score']}/100 "
            f"({scores['overall_grade']})"
        )
    except Exception as e:
        logger.error(f"Agent scoring failed: {e}")


async def run_weekly_report():
    """Generate a weekly report."""
    try:
        logger.info("Generating weekly report...")
        report = await generate_weekly_report(agent_state)
        agent_state["weekly_reports"].append(report)
        # Keep last 12 reports
        agent_state["weekly_reports"] = agent_state["weekly_reports"][-12:]
        save_state()
        text = format_report_text(report)
        await log_message("info", f"Weekly report generated: {len(report.get('recommendations', []))} recommendations")
        logger.info("Weekly report generated")
    except Exception as e:
        logger.error(f"Weekly report generation failed: {e}")


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

        # Phase 2D: Run anomaly detection and forecasting after data collection
        await run_anomaly_detection()
        await run_forecasting()

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
    # Phase 2D scheduled jobs
    scheduler.add_job(run_weekly_report, "cron", day_of_week="sun", hour="8", id="weekly_report")
    scheduler.add_job(run_agent_scoring, "interval", hours=6, id="agent_scoring")
    scheduler.start()
    await send_heartbeat()
    await log_message("info", "Analytics Agent started (Phase 2D)")
    logger.info("Analytics Agent started on port %d", settings.API_PORT)
    yield
    scheduler.shutdown()


app = FastAPI(
    title="PetHub Analytics Agent",
    description="Site analytics, performance monitoring, and indexing tracking for pethubonline.com",
    version="2.0.0",
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
        "anomaly_status": agent_state["anomalies"].get("status", "unknown") if agent_state["anomalies"] else "unknown",
        "agent_score": agent_state["agent_scores"].get("overall_score", 0) if agent_state["agent_scores"] else 0,
        "weekly_reports_count": len(agent_state["weekly_reports"]),
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


# ─── Phase 2D Endpoints ───────────────────────────────────────────────────────

@app.get("/api/anomalies")
async def get_anomalies():
    """Current anomalies and alerts."""
    if not agent_state["anomalies"]:
        return {
            "status": "unknown",
            "total_anomalies": 0,
            "critical_count": 0,
            "warning_count": 0,
            "info_count": 0,
            "anomalies": [],
            "alerts": [],
            "message": "No anomaly data yet. Run a data collection first.",
        }
    return agent_state["anomalies"]


@app.get("/api/reports")
async def get_reports():
    """List of weekly reports (metadata only for list view)."""
    reports = agent_state.get("weekly_reports", [])
    return {
        "total": len(reports),
        "reports": [
            {
                "index": i,
                "generated_at": r.get("generated_at", ""),
                "date_range": r.get("date_range", {}),
                "traffic_views": r.get("traffic", {}).get("views_this_week", 0),
                "recommendations_count": len(r.get("recommendations", [])),
            }
            for i, r in enumerate(reports)
        ],
    }


@app.get("/api/reports/latest")
async def get_latest_report():
    """Latest weekly report."""
    reports = agent_state.get("weekly_reports", [])
    if not reports:
        return {"message": "No reports generated yet. Reports are generated weekly on Sundays."}
    report = reports[-1]
    report["formatted_text"] = format_report_text(report)
    return report


@app.post("/api/reports/generate")
async def trigger_report_generation():
    """Trigger immediate report generation."""
    import asyncio
    asyncio.create_task(run_weekly_report())
    return {"message": "Report generation started"}


@app.get("/api/agents/scores")
async def get_agent_scores():
    """Agent performance scores."""
    if not agent_state["agent_scores"]:
        return {
            "message": "No agent scores yet. Scores are computed every 6 hours.",
            "agents": {},
            "overall_score": 0,
            "overall_grade": "N/A",
        }
    return agent_state["agent_scores"]


@app.post("/api/agents/score")
async def trigger_agent_scoring():
    """Trigger immediate agent scoring."""
    import asyncio
    asyncio.create_task(run_agent_scoring())
    return {"message": "Agent scoring started"}


@app.get("/api/forecast")
async def get_forecast():
    """Traffic forecast."""
    forecasts = agent_state.get("forecasts")
    if not forecasts or not forecasts.get("forecast"):
        return {
            "message": "No forecast data yet. Run a data collection first.",
            "predictions": [],
            "trend_slope": 0,
            "confidence": "unknown",
        }
    return forecasts["forecast"]


@app.get("/api/trends")
async def get_trends():
    """Trend analysis."""
    forecasts = agent_state.get("forecasts")
    if not forecasts or not forecasts.get("trends"):
        return {
            "message": "No trend data yet. Run a data collection first.",
            "direction": "unknown",
            "growth_rate_pct": 0,
        }
    return forecasts["trends"]


@app.get("/api/kpis")
async def get_kpis():
    """KPI dashboard."""
    forecasts = agent_state.get("forecasts")
    if not forecasts or not forecasts.get("kpis"):
        # Generate live KPIs
        kpis = get_kpi_dashboard(
            agent_state,
            forecast=forecasts.get("forecast") if forecasts else None,
            trends=forecasts.get("trends") if forecasts else None,
            agent_scores=agent_state.get("agent_scores"),
        )
        return kpis
    return forecasts["kpis"]


# ─── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def analytics_dashboard():
    template_path = os.path.join(os.path.dirname(__file__), "templates", "analytics_dashboard.html")
    with open(template_path, "r") as f:
        return HTMLResponse(f.read())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=settings.API_PORT, reload=False)
