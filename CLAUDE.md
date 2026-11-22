# CLAUDE.md

Working rules for Claude Code in this repo. **What** to build is in `PLAN.md`; **how** to work is here.

## The project

Weather Risk Intelligence Agent, a 24-hour take-home assignment: a chat agent that helps logistics analysts decide which US distribution hubs are most exposed to severe weather. `PLAN.md` holds the scope, assumptions, data sources, scoring model, agent design, repo structure (§12) and build phases (§13). It is the source of truth. If a request, or anything in this file, seems to conflict with it, stop and ask.

## How a phase runs

- Each session runs one phase, which Coral names ("Execute Phase N"). Do only that phase's steps marked **[CC]** in PLAN.md §13, plus the sections they reference.
- Steps marked **[Coral]** are hers. Don't do them. If one is blocking you (e.g. `.env` isn't filled in yet), say so and stop.
- At the gate, show exactly the "Claude Code shows" items, then stop and wait for Coral's review. Never start the next phase.
- If Coral asks for fixes at a gate, make them and show the full gate output again.
- When Coral makes a decision that the plan says to record (e.g. the NRI field type → §6), write it into PLAN.md.

## Rules

- Anything not in PLAN.md (a file outside §12, a new dependency, an extra feature): ask first.
- No tests beyond the eval set in PLAN.md §10.
- Never run `git commit` or `git push`. Coral reviews and commits after each gate.
- Never invent data values. If an external API returns something unexpected, or a step doesn't match the plan, stop and report it.
- Never weaken a check or change an expected result to make something pass without Coral's approval.
- Never print, log or commit the contents of `.env` or any API key.
- Report only what you actually ran and saw. If something is unverified or uncertain, say so.

## Core design rule

The LLM never produces numbers. Deterministic Python code computes every score, statistic and ranking; the LLM picks tools and explains their results. No code path may show the user a number that didn't come from a tool result.

## Stack & conventions

- Python 3, virtual environment in `.venv` (`python3 -m venv .venv`).
- `requirements.txt` with exact pinned versions. Each phase adds only the dependencies it needs.
- OpenAI Python SDK directly; no LangChain or other agent frameworks. The model comes from `OPENAI_MODEL` in `.env`.
- SQLite through Python's standard library. The database is `data/cache.db` and is not committed to git.
- Only `python -m app.refresh` calls external APIs. Everything else reads from the cache.
- Thresholds, caps, weights and paths live in `app/config.py`. No magic numbers elsewhere.
- Hub data lives only in `data/hubs.json`.
- Code, comments and docs in English. Keep the code simple and readable: Coral will walk through it live in the interview.

## Commands

| What | Command | Available from |
|---|---|---|
| Activate the venv | `source .venv/bin/activate` | Phase 0 |
| Install dependencies | `pip install -r requirements.txt` | Phase 0 |
| Fetch data (+ compute scores) | `python -m app.refresh` | Phase 1 (scores from Phase 2) |
| Agent in the terminal | `python -m app.agent` | Phase 3 |
| Run the app | `uvicorn app.main:app` → http://localhost:8000 | Phase 4 |
| Run the evals | `python -m evals.run` | Phase 5 |

## Gate report format

1. What was built: files created or changed.
2. The gate output listed for this phase in PLAN.md §13.
3. Anything that deviated from the plan, and why.
4. Open questions for Coral.
