"""The HTTP API and the chat page (PLAN.md §8).

    GET  /         the chat page (web/index.html)
    POST /chat     one question to the agent
    GET  /hubs     the hubs we cover
    GET  /scores   hubs ranked by score, for one category or overall
    GET  /health   is the server up, and has the cache been filled?
    POST /scores/refresh   fetch the NWS alerts, recompute, alert on changes (bonus)

Run: uvicorn app.main:app   ->   http://localhost:8000

Nothing here computes a number. /chat hands the question to the agent; /hubs
and /scores call the same tool functions the agent uses, so the API and the
chat always agree. The chat path reads only the cache.

Only the NWS-only refresh reaches an external data API, through app/refresh.py.
It runs from POST /scores/refresh and from the scheduled check: a background
task that runs right after startup and then every ALERT_CHECK_INTERVAL_MINUTES
(app/config.py; 0 turns it off). A lock makes sure two refreshes never overlap.
"""

import asyncio
import json
import logging
import threading
from contextlib import asynccontextmanager

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import agent, config, db, refresh, tools
from app.schemas import CategoryOrOverall, ListHubsArgs, RankHubsArgs, Region

# Show the app's own log lines, from INFO up (uvicorn only sets up its own loggers).
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("app").setLevel(logging.INFO)
log = logging.getLogger("app.main")

# Held while a refresh runs, so the scheduled check and POST /scores/refresh
# never run at the same time: the second one waits for the first to finish.
_refresh_lock = threading.Lock()


def _refresh_alerts() -> dict:
    """The NWS-only refresh, one at a time."""
    with _refresh_lock:
        return refresh.refresh(full=False)


async def _scheduled_checks() -> None:
    """Run the NWS-only refresh now, then again every ALERT_CHECK_INTERVAL_MINUTES."""
    while True:
        try:
            # In a thread, so chat requests are answered while it runs.
            summary = await asyncio.to_thread(_refresh_alerts)
            log.info("Scheduled alert check done: %d hubs, %d alert(s), computed at %s",
                     len(summary["hubs"]), len(summary["changes"]), summary["computed_at"])
        except Exception as error:  # log it and keep going: the next check still runs
            log.error("Scheduled alert check failed: %s: %s", type(error).__name__, error)
        await asyncio.sleep(config.ALERT_CHECK_INTERVAL_MINUTES * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Database: %s", db.DB_PATH)
    task = None
    if config.ALERT_CHECK_INTERVAL_MINUTES > 0:
        log.info("Scheduled alert check: every %g minutes", config.ALERT_CHECK_INTERVAL_MINUTES)
        task = asyncio.create_task(_scheduled_checks())
    else:
        log.info("Scheduled alert check: off")
    yield
    if task:
        task.cancel()


app = FastAPI(title="Weather Risk Intelligence Agent", lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: str | None = None  # omit to start a new conversation


def _short_result(tool: str, result: dict) -> str:
    """One line describing a tool result, for the "How I got this" panel.

    The agent keeps each tool's full result; the API shortens it for display.
    Every number in the line is copied from the result, none is computed here.
    """
    if "error" in result:
        return f"error: {result['error']}"
    if tool == "list_hubs":
        return f"{result['hub_count']} hubs (region: {result['region']})"
    if tool == "rank_hubs":
        ranking = ", ".join(
            f"{row['rank']}. {row['city']} {row['score']}" for row in result["ranking"]
        )
        return f"{result['category']} scores, region {result['region']}: {ranking}"
    if tool == "compare_hubs":
        hubs = "; ".join(
            f"{hub['city']}: "
            + ", ".join(
                f"{name} {category['score']}"
                for name, category in hub["categories"].items()
            )
            for hub in result["hubs"]
        )
        return f"category scores: {hubs}"
    if tool == "explain_hub_score":
        return (
            f"{result['city']}: overall score {result['overall']}, "
            f"rank {result['overall_rank']} of {result['ranked_among_hubs']}"
        )
    if tool == "current_alerts":
        hubs = "; ".join(
            f"{hub['city']}: " + ", ".join(
                f"{alert['event']} ({alert['severity']})" for alert in hub["active_alerts"]
            )
            for hub in result["hubs"] if hub["active_alerts"]
        )
        return (f"as of {result['as_of']}, {result['hubs_checked']} hubs checked: "
                + (hubs or "no active alerts"))
    if tool == "weather_stat":
        return (
            f"{result['hub_id']} {result['year']}, {result['definition']}: "
            f"{result['days']} of {result['days_with_data']} days ({result['percent_of_days']}%)"
        )
    return ""


def _run_tool(function, args) -> dict:
    """Run one tool function for a GET endpoint. An empty cache becomes a 503."""
    conn = db.connect()
    try:
        return function(conn, args)
    except LookupError as error:  # refresh hasn't been run
        raise HTTPException(status_code=503, detail=str(error))
    finally:
        conn.close()


@app.get("/")
def chat_page() -> FileResponse:
    return FileResponse(config.WEB_PAGE_PATH)


@app.post("/chat")
def chat(request: ChatRequest) -> dict:
    """Answer one question. Send the returned session_id back to ask a follow-up.

    `answer` is a validated AgentAnswer, or null when `error` is set.
    """
    turn = agent.run_turn(request.message, request.session_id)
    turn["trace"] = [
        {
            "tool": step["tool"],
            "arguments": json.loads(step["arguments"]),
            "result": _short_result(step["tool"], step["result"]),
        }
        for step in turn["trace"]
    ]
    return turn


@app.get("/hubs")
def hubs() -> dict:
    return _run_tool(tools.list_hubs, ListHubsArgs(region=None))


@app.get("/scores")
def scores(
    category: CategoryOrOverall = config.OVERALL,  # pyright: ignore[reportInvalidTypeForm]
    region: Region | None = None,  # pyright: ignore[reportInvalidTypeForm]
) -> dict:
    """Hubs ranked by score, highest first. An unknown category or region is a 422."""
    return _run_tool(
        tools.rank_hubs, RankHubsArgs(category=category, region=region, top_n=None)
    )


@app.get("/health")
def health() -> dict:
    """`scores_computed_at` is the time of the last refresh, or null if it never ran."""
    conn = db.connect()
    try:
        last_refresh = conn.execute(
            "SELECT MAX(computed_at) FROM score_snapshots"
        ).fetchone()[0]
    finally:
        conn.close()
    return {"status": "ok", "scores_computed_at": last_refresh}


@app.post("/scores/refresh")
def scores_refresh() -> dict:
    """Fetch the active NWS alerts, recompute both scores, and alert on changes.

    Meant for cron or any scheduler. Weather history and NRI don't change, so
    they are not fetched again here; `python -m app.refresh` fetches everything.
    Returns each hub's scores and the changes that crossed a threshold.
    """
    try:
        return _refresh_alerts()
    except LookupError as error:  # the full refresh hasn't been run
        status, detail = 503, str(error)
    except requests.RequestException as error:  # NWS is unreachable or answered with an error
        status, detail = 502, f"NWS request failed: {type(error).__name__}: {error}"
    except Exception as error:  # anything else: report it instead of a bare 500
        status, detail = 500, f"{type(error).__name__}: {error}"
    log.error("POST /scores/refresh failed with %d: %s", status, detail)
    raise HTTPException(status_code=status, detail=detail)
