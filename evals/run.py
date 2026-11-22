"""The eval runner (PLAN.md §10).

    python -m evals.run

Layer A: deterministic checks on the scoring engine. No LLM.
Layer B: the agent cases in evals/cases.json. The agent answers with the real
         LLM; plain code then checks the answer. No LLM grades another LLM.

Expected values are never written into the cases. Each check asks the scoring
engine (through the same tools the agent uses) for the right value at run time.

Prints a pass/fail table with a summary and saves everything to evals/results.json.
"""

import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from app import agent, config, db, scoring, tools
from app.schemas import AgentAnswer

EVALS_DIR = Path(__file__).resolve().parent
CASES_PATH = EVALS_DIR / "cases.json"
RESULTS_PATH = EVALS_DIR / "results.json"


def engine(tool: str, **arguments) -> dict:
    """The right answer, straight from the scoring engine (no LLM involved)."""
    return tools.run_tool(tool, json.dumps(arguments))


def passed(ok: bool, detail: str) -> dict:
    return {"passed": bool(ok), "detail": detail}


# --- Layer A: deterministic checks ----------------------------------------------------

def check_denver_snowfall() -> dict:
    """Denver's 2025 snowfall %, recounted here from the raw cached days, equals the tool's value."""
    metric = config.WEATHER_METRICS["snowfall_days"]
    conn = db.connect()
    try:
        daily = db.load_response(conn, scoring.OPEN_METEO, "denver")["daily"]
    finally:
        conn.close()
    year_values = [
        value for date, value in zip(daily["time"], daily["snowfall_sum"])
        if date[:4] == str(config.LAST_YEAR) and value is not None
    ]
    snowy = [value for value in year_values if value >= metric["threshold"]]
    recomputed = round(len(snowy) / len(year_values) * 100, 1)
    from_tool = engine("weather_stat", hub_id="denver", metric="snowfall_days",
                       year=config.LAST_YEAR)["percent_of_days"]
    return passed(recomputed == from_tool,
                  f"recomputed {recomputed}% ({len(snowy)} of {len(year_values)} days), tool {from_tool}%")


def check_midwest_ranking() -> dict:
    """Every Midwest ranking holds only Midwest hubs, sorted descending, with scores in 0-100."""
    midwest = {hub["id"] for hub in scoring.load_hubs() if hub["region"] == "Midwest"}
    problems = []
    for category in [*config.CATEGORIES, config.OVERALL]:
        ranking = engine("rank_hubs", category=category, region="Midwest", top_n=None)["ranking"]
        scores = [row["score"] for row in ranking]
        if {row["hub_id"] for row in ranking} != midwest:
            problems.append(f"{category}: hubs are not exactly the Midwest hubs")
        if scores != sorted(scores, reverse=True):
            problems.append(f"{category}: not sorted descending")
        if not all(0 <= score <= 100 for score in scores):
            problems.append(f"{category}: a score is outside 0-100")
    checked = f"{len(config.CATEGORIES) + 1} rankings of {len(midwest)} Midwest hubs"
    return passed(not problems, "; ".join(problems) or f"{checked}: all hold")


def check_same_inputs_same_scores() -> dict:
    """Scoring every hub twice gives identical results."""
    conn = db.connect()
    try:
        first, second = scoring.score_all(conn), scoring.score_all(conn)
    finally:
        conn.close()
    return passed(first == second, f"{len(first)} hubs scored twice: "
                                   + ("identical" if first == second else "DIFFERENT"))


LAYER_A = [
    ("Denver 2025 snowfall % = weather_stat", check_denver_snowfall),
    ("Midwest rankings: region, order, range", check_midwest_ranking),
    ("Same inputs give same scores", check_same_inputs_same_scores),
]


# --- Layer B: checks on one agent answer ------------------------------------------------
# Each check gets its spec from cases.json, the final answer (a dict), and the tool
# calls of the last turn as (tool name, arguments dict) pairs.

def _numbers_in_text(text: str) -> set[float]:
    return {float(number) for number in re.findall(r"\d+(?:\.\d+)?", text)}


def _words(text: str) -> str:
    """Lowercase, with hyphens and underscores as spaces: 'Severe-Storm' -> 'severe storm'."""
    return re.sub(r"[-_]", " ", text.lower())


def _arguments_match(expected: dict, actual: dict) -> bool:
    """Every expected argument is there. For a list, every expected item is in it."""
    for name, value in expected.items():
        if isinstance(value, list):
            if not set(value) <= set(actual.get(name) or []):
                return False
        elif actual.get(name) != value:
            return False
    return True


def tool_called(spec, answer, calls) -> dict:
    """The tool was called with (at least) these arguments.

    `arguments_if_given` are checked the same way, but only when the LLM sent
    them: an omitted (null) argument passes, because the tool then uses its default.
    """
    if_given = spec.get("arguments_if_given", {})

    def matches(arguments: dict) -> bool:
        sent = {name: value for name, value in if_given.items() if arguments.get(name) is not None}
        return _arguments_match(spec["arguments"], arguments) and _arguments_match(sent, arguments)

    ok = any(tool == spec["tool"] and matches(arguments) for tool, arguments in calls)
    made = "; ".join(f"{tool}({json.dumps(arguments)})" for tool, arguments in calls) or "no tool calls"
    wanted = f"{spec['tool']}({json.dumps(spec['arguments'])})"
    if if_given:
        wanted += f", and if given {json.dumps(if_given)}"
    return passed(ok, f"wanted {wanted}; got {made}")


def top_hubs_match_engine(spec, answer, calls) -> dict:
    """The first N hubs in the answer are the engine's top N, in order."""
    ranking = engine("rank_hubs", category=spec["category"], region=spec["region"],
                     top_n=spec["top_n"])["ranking"]
    expected = [row["hub_id"] for row in ranking]
    got = [hub["hub_id"] for hub in answer["hubs"]][: spec["top_n"]]
    return passed(got == expected, f"engine top {spec['top_n']}: {expected}; answer: {got}")


def figure_matches_tool(spec, answer, calls) -> dict:
    """The tool's value is in key_figures and in the answer text."""
    expected = engine(spec["tool"], **spec["arguments"])[spec["field"]]
    in_figures = expected in [figure["value"] for figure in answer["key_figures"]]
    in_text = expected in _numbers_in_text(answer["answer"])
    return passed(in_figures and in_text,
                  f"engine {spec['arguments']['metric']} {spec['field']} = {expected}; "
                  f"in key_figures: {in_figures}; in text: {in_text}")


def names_top_category(spec, answer, calls) -> dict:
    """The answer text names the hub's top-contributing category."""
    top = engine("explain_hub_score", hub_id=spec["hub_id"])["categories_by_contribution"][0]["category"]
    return passed(_words(top) in _words(answer["answer"]), f"top category: {top}")


def out_of_scope(spec, answer, calls) -> dict:
    return passed(answer["out_of_scope"] is True, f"out_of_scope = {answer['out_of_scope']}")


def no_hub_scores(spec, answer, calls) -> dict:
    """No hub in the answer carries a score (nothing invented for an unknown hub)."""
    scored = [hub for hub in answer["hubs"] if hub["score"] is not None]
    return passed(not scored, f"hubs with a score: {scored or 'none'}")


def contradicts_premise(spec, answer, calls) -> dict:
    """The answer gives the hub's real score for the category and says it isn't high.

    "Says it isn't high" is a phrase match (the phrases are in cases.json): a
    simple code check, not a judgment of the whole answer.
    """
    categories = engine("explain_hub_score", hub_id=spec["hub_id"])["categories_by_contribution"]
    real = next(row for row in categories if row["category"] == spec["category"])
    has_score = real["score"] in [figure["value"] for figure in answer["key_figures"]]
    text = _words(answer["answer"])
    phrase = next((p for p in spec["phrases"] if re.search(rf"\b{re.escape(p)}\b", text)), None)
    return passed(has_score and phrase is not None,
                  f"engine: {spec['category']} score {real['score']}, rank {real['rank_among_hubs']}; "
                  f"score in key_figures: {has_score}; phrase found: {phrase!r}")


def alerts_match_tool(spec, answer, calls) -> dict:
    """The answer reports only what current_alerts returns: the hubs with an active
    alert (and no others), every alert's event name, and the fetch time (as_of)."""
    result = engine("current_alerts", hub_ids=None, region=None)
    expected = sorted(result["hubs_with_active_alerts"])
    got = sorted(hub["hub_id"] for hub in answer["hubs"])
    text = answer["answer"].lower()
    events = {alert["event"] for hub in result["hubs"] for alert in hub["active_alerts"]}
    missing = sorted(event for event in events if event.lower() not in text)
    as_of_stated = result["as_of"].lower() in text
    return passed(got == expected and not missing and as_of_stated and answer["out_of_scope"] is False,
                  f"tool: hubs with alerts {expected}, as of {result['as_of']}; answer hubs: {got}; "
                  f"events missing from text: {missing or 'none'}; as_of in text: {as_of_stated}; "
                  f"out_of_scope = {answer['out_of_scope']}")


CHECKS = {
    "tool_called": tool_called,
    "top_hubs_match_engine": top_hubs_match_engine,
    "figure_matches_tool": figure_matches_tool,
    "names_top_category": names_top_category,
    "out_of_scope": out_of_scope,
    "no_hub_scores": no_hub_scores,
    "contradicts_premise": contradicts_premise,
    "alerts_match_tool": alerts_match_tool,
}


def run_case(case: dict) -> dict:
    """Run one case's turns in a fresh session, then check the last answer."""
    session_id = uuid.uuid4().hex
    tool_results = []  # every tool result of the session, for the key-figure check
    for message in case["turns"]:
        turn = agent.run_turn(message, session_id)
        tool_results += [step["result"] for step in turn["trace"]]
        if turn["error"]:
            break
    calls = [(step["tool"], json.loads(step["arguments"])) for step in turn["trace"]]
    answer = turn["answer"]
    record = {"id": case["id"], "name": case["name"], "turns": case["turns"], "answer": answer,
              "tool_calls": [{"tool": tool, "arguments": arguments} for tool, arguments in calls],
              "retries": turn["retries"], "error": turn["error"]}

    if answer is None:
        record["checks"] = {"valid_answer": passed(False, f"agent error: {turn['error']}")}
    else:
        # The two checks every case gets (§10), then the case's own.
        try:
            AgentAnswer.model_validate(answer)
            valid = passed(True, "valid AgentAnswer")
        except ValidationError as error:
            valid = passed(False, str(error))
        unsupported = agent.unsupported_key_figures(answer, tool_results)
        record["checks"] = {
            "valid_answer": valid,
            "key_figures_from_tools": passed(
                not unsupported,
                f"{len(answer['key_figures'])} key figures; not in any tool result: "
                + (", ".join(f"{f['label']} = {f['value']}" for f in unsupported) or "none")),
        }
        for number, spec in enumerate(case["checks"], start=1):
            # Numbered, because a case can use the same check type twice.
            record["checks"][f"{number}. {spec['type']}"] = CHECKS[spec["type"]](spec, answer, calls)

    record["passed"] = all(check["passed"] for check in record["checks"].values())
    return record


# --- Report -------------------------------------------------------------------------

def print_row(layer: str, name: str, record: dict) -> None:
    print(f"{'PASS' if record['passed'] else 'FAIL'}  {layer:<2} {name}")
    for check_name, check in record.get("checks", {"": record}).items():
        if not check["passed"]:
            print(f"        x {check_name}: {check['detail']}".replace("x :", "x"))


def main() -> None:
    print("Layer A: deterministic checks")
    layer_a = []
    for name, check in LAYER_A:
        record = {"name": name, **check()}
        layer_a.append(record)
        print_row("A", name, record)

    print("\nLayer B: agent cases")
    layer_b = []
    for case in json.loads(CASES_PATH.read_text()):
        record = run_case(case)
        layer_b.append(record)
        print_row("B", f"{case['id']:>2}. {case['name']}", record)

    a_passed = sum(record["passed"] for record in layer_a)
    b_passed = sum(record["passed"] for record in layer_b)
    print(f"\nSummary: Layer A {a_passed}/{len(layer_a)} passed, Layer B {b_passed}/{len(layer_b)} passed")

    RESULTS_PATH.write_text(json.dumps({
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": os.environ.get("OPENAI_MODEL"),
        "summary": {"layer_a_passed": a_passed, "layer_a_total": len(layer_a),
                    "layer_b_passed": b_passed, "layer_b_total": len(layer_b)},
        "layer_a": layer_a,
        "layer_b": layer_b,
    }, indent=2) + "\n")
    print(f"Results saved to {RESULTS_PATH.relative_to(config.ROOT)}")

    if a_passed < len(layer_a) or b_passed < len(layer_b):
        sys.exit(1)


if __name__ == "__main__":
    main()
