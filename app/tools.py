"""The five tools the LLM can call (PLAN.md §7).

Each tool has:
  - an arguments model (app/schemas.py), which gives the strict JSON schema;
  - a function that calls the scoring engine and returns a plain dict.

The LLM never computes anything: every number it reports comes from these dicts.
"""

import openai
from pydantic import ValidationError

from app import config, db, scoring
from app.schemas import (
    CompareHubsArgs,
    ExplainHubScoreArgs,
    ListHubsArgs,
    RankHubsArgs,
    WeatherStatArgs,
)


def _score(breakdown: dict, category: str) -> float:
    """A hub's reported (rounded) score for one category, or its overall score."""
    if category == config.OVERALL:
        return breakdown["overall"]
    return breakdown["categories"][category]["score"]


def _rank(scored: list[tuple[dict, dict]], category: str) -> list[dict]:
    """Breakdowns sorted by score, highest first.

    Sorts on the exact (unrounded) scores; rounding happens only in what is
    reported. Exact ties keep hubs.json order (Python's sort is stable).
    """
    ordered = sorted(scored, key=lambda pair: pair[1][category], reverse=True)
    return [breakdown for breakdown, _ in ordered]


def _category_for_llm(category: dict) -> dict:
    """One category's breakdown, with the NRI/weather split as percentages (50 and 50)
    instead of shares (0.5 and 0.5), so the LLM never converts a number itself."""
    shown = dict(category)
    shown["nri_share_percent"] = round(shown.pop("nri_share") * 100)
    shown["weather_share_percent"] = round(shown.pop("weather_share") * 100)
    return shown


# --- Tool functions ------------------------------------------------------------------

def list_hubs(conn, args: ListHubsArgs) -> dict:
    hubs = [
        {"hub_id": hub["id"], "city": hub["city"], "state": hub["state"], "region": hub["region"]}
        for hub in scoring.load_hubs()
        if args.region in (None, hub["region"])
    ]
    return {"region": args.region or "all", "hub_count": len(hubs), "hubs": hubs,
            "regions": config.REGIONS}


def rank_hubs(conn, args: RankHubsArgs) -> dict:
    scored = [pair for pair in scoring.score_all_exact(conn) if args.region in (None, pair[0]["region"])]
    rows = [
        {"rank": position, "hub_id": b["hub_id"], "city": b["city"], "state": b["state"],
         "region": b["region"], "score": _score(b, args.category)}
        for position, b in enumerate(_rank(scored, args.category), start=1)
    ]
    return {
        "category": args.category,
        "region": args.region or "all",
        "hubs_ranked": len(rows),
        "ranking": rows[: args.top_n] if args.top_n else rows,
        "score_scale": "0-100, higher = more exposed",
        "observation_years": f"{config.FIRST_YEAR}-{config.LAST_YEAR}",
    }


def compare_hubs(conn, args: CompareHubsArgs) -> dict:
    categories = args.categories or list(config.CATEGORIES)
    hub_ids = list(dict.fromkeys(args.hub_ids))  # drops duplicates, keeps order
    by_id = {pair[0]["hub_id"]: pair for pair in scoring.score_all_exact(conn)}
    scored = [by_id[hub_id] for hub_id in hub_ids]
    hubs = [
        {"hub_id": b["hub_id"], "city": b["city"], "state": b["state"], "region": b["region"],
         "overall": b["overall"],
         "categories": {name: _category_for_llm(b["categories"][name]) for name in categories},
         "nri_county": b["nri_county"]}
        for b, _ in scored
    ]
    return {
        "categories_compared": categories,
        "hubs": hubs,
        # Who scores highest in each category, so the LLM doesn't have to compare numbers.
        "highest_per_category": {name: _rank(scored, name)[0]["hub_id"] for name in categories},
        "score_scale": "0-100, higher = more exposed",
        "observation_years": f"{config.FIRST_YEAR}-{config.LAST_YEAR}",
    }


def explain_hub_score(conn, args: ExplainHubScoreArgs) -> dict:
    scored = scoring.score_all_exact(conn)
    breakdown, exact = next(pair for pair in scored if pair[0]["hub_id"] == args.hub_id)

    def rank_of(category: str) -> int:
        return [b["hub_id"] for b in _rank(scored, category)].index(args.hub_id) + 1

    # Categories ordered by what they add to the overall score (weight x exact score).
    by_contribution = sorted(
        config.CATEGORIES,
        key=lambda name: config.CATEGORY_WEIGHTS[name] * exact[name],
        reverse=True,
    )
    return {
        **breakdown,
        "categories": {name: _category_for_llm(category)
                       for name, category in breakdown["categories"].items()},
        "overall_rank": rank_of(config.OVERALL),
        "ranked_among_hubs": len(scored),
        "categories_by_contribution": [
            {"category": name, "score": breakdown["categories"][name]["score"],
             "rank_among_hubs": rank_of(name)}
            for name in by_contribution
        ],
        "score_scale": "0-100, higher = more exposed",
    }


def weather_stat(conn, args: WeatherStatArgs) -> dict:
    return scoring.weather_stat(conn, args.hub_id, args.metric, args.year)


# --- Registry: name -> (arguments model, function, description for the LLM) ------------

_METRICS = "; ".join(f"{name} = {m['label']}" for name, m in config.WEATHER_METRICS.items())

TOOLS = {
    "list_hubs": (ListHubsArgs, list_hubs,
        "List the hubs and regions we cover. Use it to check whether a city is one of our hubs."),
    "rank_hubs": (RankHubsArgs, rank_hubs,
        ("Rank hubs by exposure score, highest first, for one hazard category or overall, "
        "optionally within one region.")),
    "compare_hubs": (CompareHubsArgs, compare_hubs,
        ("Compare two or more hubs side by side: overall score and category scores, "
        "each with its breakdown (NRI hazards and weather metrics).")),
    "explain_hub_score": (ExplainHubScoreArgs, explain_hub_score,
        ("Full breakdown of one hub's score (inputs, parts, categories, overall), its rank "
        "among all hubs, and its categories ordered by contribution. Use it for 'why' questions.")),
    "weather_stat": (WeatherStatArgs, weather_stat,
        ("Count the days in one year at one hub that pass a weather threshold, as a number of "
        f"days and a percentage of the year. Metrics: {_METRICS}.")),
}

# The tool list sent to the LLM. The SDK turns each Pydantic model into a strict JSON schema.
TOOL_SCHEMAS = [
    openai.pydantic_function_tool(args_model, name=name, description=description)
    for name, (args_model, _, description) in TOOLS.items()
]


def run_tool(name: str, arguments_json: str) -> dict:
    """Validate the LLM's arguments and run one tool. Errors come back as a dict,
    so the LLM can read them and correct itself."""
    if name not in TOOLS:
        return {"error": f"Unknown tool '{name}'. Available: {list(TOOLS)}"}
    args_model, function, _ = TOOLS[name]
    try:
        args = args_model.model_validate_json(arguments_json)
    except ValidationError as error:
        return {"error": f"Invalid arguments for {name}: {error.errors(include_url=False)}"}
    conn = db.connect()
    try:
        return function(conn, args)
    except (LookupError, ValueError) as error:
        return {"error": str(error)}
    finally:
        conn.close()
