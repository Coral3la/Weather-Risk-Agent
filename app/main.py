"""The HTTP API and the chat page (PLAN.md §8).

    GET  /         the chat page (web/index.html)
    POST /chat     one question to the agent
    GET  /hubs     the hubs we cover
    GET  /scores   hubs ranked by score, for one category or overall
    GET  /health   is the server up, and has the cache been filled?

Run: uvicorn app.main:app   ->   http://localhost:8000

Nothing here computes a number or calls an external API. /chat hands the
question to the agent; /hubs and /scores call the same tool functions the
agent uses, so the API and the chat always agree.
"""

import json

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import agent, config, db, tools
from app.schemas import CategoryOrOverall, ListHubsArgs, RankHubsArgs, Region

app = FastAPI(title="Weather Risk Intelligence Agent")


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
        ranking = ", ".join(f"{row['rank']}. {row['city']} {row['score']}" for row in result["ranking"])
        return f"{result['category']} scores, region {result['region']}: {ranking}"
    if tool == "compare_hubs":
        hubs = "; ".join(
            f"{hub['city']}: " + ", ".join(f"{name} {category['score']}"
                                           for name, category in hub["categories"].items())
            for hub in result["hubs"]
        )
        return f"category scores: {hubs}"
    if tool == "explain_hub_score":
        return (f"{result['city']}: overall score {result['overall']}, "
                f"rank {result['overall_rank']} of {result['ranked_among_hubs']}")
    if tool == "weather_stat":
        return (f"{result['hub_id']} {result['year']}, {result['definition']}: "
                f"{result['days']} of {result['days_with_data']} days ({result['percent_of_days']}%)")
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
        {"tool": step["tool"],
         "arguments": json.loads(step["arguments"]),
         "result": _short_result(step["tool"], step["result"])}
        for step in turn["trace"]
    ]
    return turn


@app.get("/hubs")
def hubs() -> dict:
    return _run_tool(tools.list_hubs, ListHubsArgs(region=None))


@app.get("/scores")
def scores(category: CategoryOrOverall = config.OVERALL, region: Region | None = None) -> dict:
    """Hubs ranked by score, highest first. An unknown category or region is a 422."""
    return _run_tool(tools.rank_hubs, RankHubsArgs(category=category, region=region, top_n=None))


@app.get("/health")
def health() -> dict:
    """`scores_computed_at` is the time of the last refresh, or null if it never ran."""
    conn = db.connect()
    try:
        last_refresh = conn.execute("SELECT MAX(computed_at) FROM score_snapshots").fetchone()[0]
    finally:
        conn.close()
    return {"status": "ok", "scores_computed_at": last_refresh}
