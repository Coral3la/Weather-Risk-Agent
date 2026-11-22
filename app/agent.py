"""The agent loop (PLAN.md §7).

    user message + history -> LLM picks tools -> code runs them -> results go back
    to the LLM -> repeat (max 5 rounds) -> final structured answer (AgentAnswer)

The LLM only chooses tools and writes the explanation. Every number comes from
a tool result (app/tools.py -> app/scoring.py).

Terminal chat: python -m app.agent
"""

import json
import os
import uuid

import openai
from dotenv import load_dotenv
from pydantic import ValidationError

from app import config, scoring, tools
from app.schemas import AgentAnswer

load_dotenv()  # reads OPENAI_API_KEY and OPENAI_MODEL from .env

SYSTEM_PROMPT = f"""\
You are a weather-risk analyst assistant for a logistics company. You help analysts decide \
which distribution hubs are most exposed to severe weather and should be prioritized for \
resilience investment.

What you have
- A fixed list of {len(scoring.load_hubs())} US hubs (call list_hubs to see them).
- Historical exposure scores, 0-100 (higher = more exposed), per hazard category \
({", ".join(config.CATEGORIES)}) and overall. They combine FEMA National Risk Index county \
data with Open-Meteo daily weather for {config.FIRST_YEAR}-{config.LAST_YEAR}.
- Yearly weather statistics per hub for {config.FIRST_YEAR}-{config.LAST_YEAR}.

Rules
1. Numbers: use only numbers that appear in tool results from this conversation, copied \
exactly. Never calculate, estimate, round or recall a number yourself. If you need a number, \
call a tool.
2. Scope: if a hub or city is not in our list, or the question needs a weather forecast, \
climate projection or business data (throughput, revenue, cost), say so, set out_of_scope \
to true, and do not guess or substitute another hub. Scores measure historical exposure, \
not future weather and not return on investment.
3. Premise: for "why is X high/low" questions, start the answer with the hub's overall rank \
among all hubs and its overall score, both from the explain_hub_score result, so the reader \
can see whether the premise holds. If the data contradicts the premise (for example "why is \
X so high?" when X is low or mid-ranked), say that clearly, with the numbers that show it.
4. Assumptions: always list the definitions and assumptions behind the answer (thresholds, \
years, what the score measures, data limits).
5. Wording: describe scores and metrics only with what the tool result states: its \
definitions, its components (NRI hazards, weather metrics) and the NRI/weather split \
(nri_share_percent, weather_share_percent). Do not add explanations, causes or adjectives of your own. A \
"cap" is only the value that scales a number to 0-100; it says nothing about the events. \
A category with nri_share_percent 100 is entirely NRI data, not "predominantly".
6. Components: when you explain a category score, name every component in its breakdown \
with its score: every entry in nri_part.hazards and every entry in weather_part.metrics. \
Skip none. A hazard with the note "no NRI record" must be named too, saying it has no NRI \
record and counts as 0 (for example "coastal flooding: no NRI record, counts as 0"), so the \
numbers shown account for the category score.
7. Follow-ups: when a question leaves something out ("and for flooding?"), reuse the \
region, hubs or year from earlier in the conversation.

Definitions
- "Last year" means {config.LAST_YEAR}.
- Regions are US Census regions: {", ".join(config.REGIONS)}.
- For any question about snow or snowfall days, call weather_stat with both snowfall_days \
(>= 0.25 cm) and snow_days (>= 1 cm), and report both with their definitions.
- Weather data is gridded reanalysis, not station observations, so counts can differ from \
local station records. NRI data is county-level.

Final answer
- key_figures: every number you mention in the answer, with the tool it came from.
- hubs: the hubs the answer is about. hubs[].score depends on how many categories the \
answer covers. One category (or only the overall score): each hub's score for it. Two or \
more categories (for example a comparison on hurricane and flood): score is null for every \
hub, and the scores go in key_figures instead.
- confidence: high when tool results answer the question directly; lower when the question \
only partly fits the data. Give the reason.
"""

# One message list per session_id. In memory only: lost when the process stops.
_sessions: dict[str, list[dict]] = {}


def _error(kind: str, message: str) -> dict:
    return {"type": kind, "message": message}


def _assistant_message(message) -> dict:
    """The LLM's reply, as a plain dict we can keep in the history."""
    entry = {"role": "assistant", "content": message.content}
    if message.tool_calls:
        entry["tool_calls"] = [
            {"id": call.id, "type": "function",
             "function": {"name": call.function.name, "arguments": call.function.arguments}}
            for call in message.tool_calls
        ]
    return entry


def _numbers_in(value) -> list[float]:
    """Every number inside a tool result (nested dicts and lists)."""
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [value]
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return [number for item in value for number in _numbers_in(item)]
    return []


def unsupported_key_figures(answer: dict, tool_results: list[dict]) -> list[dict]:
    """The answer's key_figures whose value appears in none of the tool results.

    This enforces the core design rule in code: the LLM may only report numbers
    that a tool produced. An empty list means every key figure is backed.
    """
    known = set(_numbers_in(tool_results))
    return [figure for figure in answer["key_figures"] if figure["value"] not in known]


def _session_tool_results(messages: list[dict]) -> list[dict]:
    """Every tool result in the session so far (earlier turns included)."""
    return [json.loads(m["content"]) for m in messages if m["role"] == "tool"]


def _ask_llm(client: openai.OpenAI, model: str, messages: list[dict], allow_tools: bool):
    """One LLM call. The reply is either tool calls or an answer matching AgentAnswer."""
    return client.chat.completions.parse(
        model=model,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, *messages],
        tools=tools.TOOL_SCHEMAS,
        tool_choice="auto" if allow_tools else "none",
        response_format=AgentAnswer,  # structured output: the SDK sends its JSON schema
    ).choices[0].message


def run_turn(message: str, session_id: str | None = None) -> dict:
    """Answer one user message.

    Returns {"session_id", "answer", "error", "trace", "retries"}:
      - answer:  a validated AgentAnswer as a dict, or None if there was an error
      - error:   None, or {"type", "message"}
      - trace:   every tool call of this turn, with its arguments and result
      - retries: why an answer was rejected and asked for again (usually empty)
    """
    session_id = session_id or uuid.uuid4().hex
    history = _sessions.setdefault(session_id, [])
    trace: list[dict] = []
    retries: list[str] = []

    def result(answer=None, error=None) -> dict:
        return {"session_id": session_id, "answer": answer, "error": error,
                "trace": trace, "retries": retries}

    model = os.environ.get("OPENAI_MODEL")
    if not os.environ.get("OPENAI_API_KEY") or not model:
        return result(error=_error("config", "Set OPENAI_API_KEY and OPENAI_MODEL in .env"))
    client = openai.OpenAI()

    # Work on a copy, so a failed turn leaves the session's history unchanged.
    messages = [*history, {"role": "user", "content": message}]
    rounds_used = 0

    def can_retry(reason: str) -> bool:
        """Record a rejected answer. True if the LLM gets another attempt."""
        retries.append(reason)
        return len(retries) <= config.ANSWER_RETRIES

    while True:
        try:
            # After MAX_TOOL_ROUNDS the LLM must answer with what it has.
            reply = _ask_llm(client, model, messages, allow_tools=rounds_used < config.MAX_TOOL_ROUNDS)
        except (ValidationError, json.JSONDecodeError, openai.LengthFinishReasonError,
                openai.ContentFilterFinishReasonError) as error:
            # The reply was not a valid AgentAnswer. Retry once, then give up with an error object.
            if can_retry(f"Answer failed schema validation: {error}"):
                continue
            return result(error=_error("invalid_answer", f"The model's answer failed validation: {retries}"))
        except openai.OpenAIError as error:
            return result(error=_error("llm_api", f"{type(error).__name__}: {error}"))

        if reply.tool_calls:
            rounds_used += 1
            messages.append(_assistant_message(reply))
            for call in reply.tool_calls:
                tool_result = tools.run_tool(call.function.name, call.function.arguments)
                trace.append({"tool": call.function.name,
                              "arguments": call.function.arguments,
                              "result": tool_result})
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": json.dumps(tool_result)})
            continue

        if reply.parsed is None:  # a refusal, or empty content
            if can_retry(f"No structured answer (refusal: {reply.refusal})"):
                continue
            return result(error=_error("invalid_answer", f"The model gave no structured answer: {retries}"))

        # Runtime check: every key figure must be a number that a tool returned.
        answer = reply.parsed.model_dump()
        unsupported = unsupported_key_figures(answer, _session_tool_results(messages))
        if unsupported:
            values = ", ".join(f"{figure['label']} = {figure['value']}" for figure in unsupported)
            if not can_retry(f"key_figures not found in any tool result: {values}"):
                return result(error=_error(
                    "unsupported_numbers", f"The model reported numbers no tool produced: {retries}"))
            # Show the LLM its rejected answer and name the values, then ask again.
            messages.append(_assistant_message(reply))
            messages.append({"role": "user", "content":
                f"[Automatic check] These key_figures values do not appear in any tool result: "
                f"{values}. Answer the same question again. Every key_figures value must be "
                f"copied exactly from a tool result; call a tool if you need a number, and "
                f"leave out any figure no tool gives you."})
            continue

        messages.append(_assistant_message(reply))
        history[:] = messages  # the turn succeeded: keep it for follow-ups
        return result(answer=answer)


def main() -> None:
    """Interactive terminal chat. History is kept for the run, so follow-ups work."""
    session_id = uuid.uuid4().hex
    print("Weather Risk Intelligence Agent. Ask a question (empty line or Ctrl-D to quit).")
    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not question:
            break
        turn = run_turn(question, session_id)
        for step in turn["trace"]:
            print(f"  [tool] {step['tool']}({step['arguments']})")
        for reason in turn["retries"]:
            print(f"  [retry] {reason}")
        if turn["error"]:
            print(f"\nERROR ({turn['error']['type']}): {turn['error']['message']}")
            continue
        print("\n" + turn["answer"]["answer"])
        print("\n" + json.dumps(turn["answer"], indent=2))


if __name__ == "__main__":
    main()
