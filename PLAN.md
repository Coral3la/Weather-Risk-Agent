# Weather Risk Intelligence Agent — Plan

_Status: v1, approved 2026-11-22 · Coral (planned with Claude)_

This file covers **what** to build. **How** Claude Code works in this repo (rules, conventions, commands, gate reports) is in `CLAUDE.md`.

## 1. Goal

An agent that helps logistics analysts decide which distribution hubs are most exposed to severe-weather disruption and should be prioritized for resilience investment. Analysts ask questions in a chat; the agent answers using public weather and hazard data plus a deterministic scoring model, and explains its reasoning.

**Guiding principle:** the LLM decides *what to look up* and *how to explain it*. Python code computes *every number*.

## 2. Scope

**In scope**
- A fixed set of 14 hubs, defined by us (§4).
- Historical exposure scoring per hub, by hazard category, plus an overall score.
- Question types: ranking, comparison, weather statistics, score explanation, conversational follow-ups.
- Chat UI → API → agent. JSON-schema-enforced LLM output. Small eval set + runner.

**Out of scope** (stated in the README, and by the agent when asked)
- Forecasting future weather; climate projections.
- Business impact (hub throughput, revenue, upgrade cost). We rank *exposure*, not ROI. Real prioritization = exposure × business impact; we don't have data for the second factor.
- Hubs or cities outside the list.
- Voice, authentication, multi-user persistence.

## 3. Assumptions & definitions

| Topic | Decision |
|---|---|
| Hub list | The company didn't provide one. We define 14 hubs in major logistics cities, placed at city-center coordinates. |
| Regions | US Census Bureau regions. Midwest = IL, IN, IA, KS, MI, MN, MO, NE, ND, OH, SD, WI. |
| "Exposure" | How often and how strongly hazards have historically hit the hub's location. Not a forecast. |
| "Last year" | The previous full calendar year (2025). |
| "Day with snowfall" | Daily `snowfall_sum` ≥ 0.25 cm (0.1 in, the NWS threshold for measurable snowfall; less counts as a trace). Answers also report ≥ 1 cm for context. |
| Observation window | Weather metrics in the score = yearly average over 2021–2025. |
| Data resolution | Open-Meteo historical data is gridded reanalysis (~9–25 km), not station observations, so counts may differ from local NOAA station records. NRI is county-level and relative to all US counties. |
| Thresholds & weights | All live in one config file and are listed in the README. |

## 4. Hubs (proposal)

| id | City | State | Region | County (FIPS) | lat, lon |
|---|---|---|---|---|---|
| chicago | Chicago | IL | Midwest | Cook (17031) | 41.88, -87.63 |
| indianapolis | Indianapolis | IN | Midwest | Marion (18097) | 39.77, -86.16 |
| columbus | Columbus | OH | Midwest | Franklin (39049) | 39.96, -83.00 |
| kansas_city | Kansas City | MO | Midwest | Jackson (29095) | 39.10, -94.58 |
| minneapolis | Minneapolis | MN | Midwest | Hennepin (27053) | 44.98, -93.27 |
| dallas | Dallas | TX | South | Dallas (48113) | 32.78, -96.80 |
| houston | Houston | TX | South | Harris (48201) | 29.76, -95.37 |
| miami | Miami | FL | South | Miami-Dade (12086) | 25.76, -80.19 |
| atlanta | Atlanta | GA | South | Fulton (13121) | 33.75, -84.39 |
| memphis | Memphis | TN | South | Shelby (47157) | 35.15, -90.05 |
| denver | Denver | CO | West | Denver (08031) | 39.74, -104.99 |
| los_angeles | Los Angeles | CA | West | Los Angeles (06037) | 34.05, -118.24 |
| phoenix | Phoenix | AZ | West | Maricopa (04013) | 33.45, -112.07 |
| newark | Newark | NJ | Northeast | Essex (34013) | 40.74, -74.17 |

Covers every city in the assignment's example questions, plus 5 Midwest hubs so the Midwest ranking is meaningful. Phase 1 verifies that the county name NRI returns matches each FIPS code.

## 5. Data sources (public, no paid keys)

| Source | What we use | Access |
|---|---|---|
| Open-Meteo Historical Weather API (`archive-api.open-meteo.com/v1/archive`) | Daily `snowfall_sum`, `precipitation_sum`, `temperature_2m_max`, `temperature_2m_min`, `wind_gusts_10m_max` | Free, no key |
| FEMA National Risk Index (NRI), county level, version "December 2025" | Per-hazard fields for hurricane (HRCN), strong wind (SWND), inland flooding (IFLD), coastal flooding (CFLD), winter weather (WNTW), ice storm (ISTM), cold wave (CWAV), tornado (TRND), hail (HAIL), heat wave (HWAV) | FEMA's ArcGIS feature layer `National_Risk_Index_Counties` (layer 0), queried by `STCOFIPS`: `https://services.arcgis.com/XG15cJAlne2vxtgt/ArcGIS/rest/services/National_Risk_Index_Counties/FeatureServer/0/query`. Free, no key. |
| NWS API (`api.weather.gov/alerts/active`) | Active alerts at each hub's point. Bonus only (§11). | Free; requires a `User-Agent` header |

**NRI status.** In December 2025 FEMA retired the standalone NRI website and its downloads, and moved NRI data into its Resilience Analysis and Planning Tool (RAPT). The December 2025 release is the latest, and DHS has told GAO there will be no ongoing updates. That release replaced riverine flooding (RFLD) with inland flooding (IFLD). The same ArcGIS org also hosts older NRI layers (names ending in `(March_2023)` or `v117`); don't use them.

All responses are cached in SQLite (§9), so after one refresh the app runs without calling external APIs. This matters for the live demo.

## 6. Scoring model (deterministic)

Five hazard categories, each scored 0–100 per hub:

| Category | NRI hazards | Open-Meteo metrics (yearly avg, 2021–2025) |
|---|---|---|
| winter | WNTW, ISTM, CWAV | snow days (≥ 1 cm); severe-cold days (Tmin ≤ −18 °C / 0 °F) |
| hurricane_wind | HRCN, SWND | high-wind days (gusts ≥ 93 km/h / 58 mph, the NWS severe-wind threshold) |
| flood | IFLD, CFLD | heavy-rain days (≥ 50 mm) |
| severe_storm | TRND, HAIL | none (NRI only) |
| heat | HWAV | extreme-heat days (Tmax ≥ 38 °C / 100 °F) |

How the numbers combine:
- **NRI part** = mean of the category's NRI hazard values, on a 0–100 scale.
- **Weather part** = each metric scaled against a fixed cap, `min(value / cap, 1) × 100`, then averaged. Fixed caps (instead of min-max across hubs) keep each hub's score stable when hubs are added, and make it explainable ("31 snow days/yr against a cap of 60 → 52"). Cap values are set in config and reviewed at Gate 2.
- **Category score** = 0.5 × NRI part + 0.5 × weather part. Where a category has no weather metric, it's 100% NRI.
- **Overall score** = weighted mean of the category scores. Default: equal weights, configurable.
- Every score comes back with its full breakdown (inputs → parts → category → overall). The agent uses this breakdown to answer "why" questions.

**Why snow days in the score use ≥ 1 cm, not the 0.25 cm "day with snowfall" threshold (§3).** The two answer different questions. The snowfall-% question asks whether it snowed at all. The score asks how often snow disrupts hub operations, and a thin layer that melts doesn't. 1 cm is our judgment call, not an official standard. Models may also produce many days with tiny snowfall amounts, which a higher threshold filters out; Gate 1 checks this on Denver's data before we rely on it.

**NRI field: annualized frequency (`*_AFREQ`), decided at Gate 1.** It measures how often each hazard occurs in the county, which is the closest NRI measure to "how often could this hub be disrupted". The alternatives mix in things that don't describe a hub's exposure: `*_RISKS` adds the county's social vulnerability and community resilience, and `*_EALS` depends on the value of the county's buildings, population and agriculture. Trade-offs we accept:
- AFREQ counts how often a hazard happens, not how severe it is.
- Units and ranges differ by hazard (across our 14 hubs, winter weather reaches ~17 per year while hurricane peaks at 0.3), so each hazard gets its own fixed cap, like the weather metrics. Caps are proposed and reviewed at Gate 2.
- Some AFREQ values are null. For our hubs these are hazards that don't plausibly occur there: coastal flooding in inland counties, hurricanes in Denver and Minneapolis, ice storms in Phoenix, Los Angeles and Miami. A null counts as 0, and the breakdown marks it as "no NRI record".
- NRI values cover the whole county, not the hub's point (e.g. Los Angeles County includes mountain areas with winter weather). The point-level Open-Meteo half of each category score partly offsets this.

## 7. Agent design

- Use the OpenAI Python SDK directly, with no LangChain or agent framework. Fewer layers means every step is visible and explainable. The model name comes from the `OPENAI_MODEL` env var.
- The loop: user message + history → LLM picks tools → code runs them → results go back to the LLM → repeat (max 5 rounds) → final structured answer.

**Tools.** Each has a strict JSON schema, and code re-validates the arguments with Pydantic.

| Tool | Purpose |
|---|---|
| `list_hubs(region?)` | Which hubs and regions we cover |
| `rank_hubs(category, region?, top_n?)` | Ranking from the scoring engine |
| `compare_hubs(hub_ids, categories?)` | Side-by-side scores with breakdowns |
| `explain_hub_score(hub_id)` | Full breakdown of one hub's score |
| `weather_stat(hub_id, metric, year)` | e.g. % of days with snowfall in a given year |

**Final answer.** The LLM returns an `AgentAnswer` (a Pydantic model → JSON schema, via OpenAI structured outputs):
- `answer`: str, the text shown to the user
- `hubs`: list of `{hub_id, name, score?}`
- `key_figures`: list of `{label, value, unit, source_tool}`
- `assumptions`: list[str]
- `confidence`: `"high" | "medium" | "low"`, plus `confidence_reason`
- `out_of_scope`: bool

If parsing or validation fails, the agent retries once. If it fails again, it returns a clear error object, never unvalidated free text.

**System-prompt rules**
- Use only numbers that came from tool results.
- If a hub or city isn't in the list, or the question needs a forecast, say so. Don't guess.
- If the question's premise contradicts the data (e.g. "why is X high?" when it isn't), say so.
- Always state the assumptions behind the answer.

**Follow-ups.** Conversation history is kept per `session_id` (in memory), so "and what about flooding?" reuses the region and hubs from the previous turn.

## 8. API & chat UI

FastAPI endpoints:
- `POST /chat` `{message, session_id?}` → `{session_id, answer: AgentAnswer, trace}`. `trace` lists each tool call with its arguments and a short result.
- `GET /hubs`, `GET /scores?category=&region=`
- `GET /health`
- `POST /scores/refresh`: bonus (§11)

UI: one static page (`web/index.html`, plain HTML + JS) served by FastAPI. It talks only to `/chat`. It shows the answer plus a collapsible "How I got this" panel with the tools used, assumptions and confidence. One command runs everything.

## 9. Storage: SQLite

**Why:** a single file, no server, in Python's standard library, and more than enough for 14 hubs. It's also easy to inspect live during the interview. Postgres would add setup with no benefit at this size. With no storage at all, every question would re-call the external APIs: slow, exposed to rate limits, the demo breaks if an API is down, and there's no score history for alerts.

**Tables**
- `api_cache(source, cache_key, fetched_at, payload_json)`
- `score_snapshots(hub_id, computed_at, overall, breakdown_json)`

The hub list and scoring config are version-controlled files (`data/hubs.json`, `app/config.py`), not DB rows. The database itself is `data/cache.db`. It's created by `python -m app.refresh` and isn't committed to git (`.gitignore`), so the README tells you to run refresh first. Chat sessions live in memory and are lost on restart; that's acceptable for a demo and stated in the README.

**Data refresh:** `python -m app.refresh` fetches all sources into the cache, then computes and stores a score snapshot.

## 10. Evaluation

`python -m evals.run` runs two layers, prints a pass/fail table with a summary, and saves results to `evals/results.json`.

**Layer A: deterministic checks (no LLM)**
- Denver's 2025 snowfall %, recomputed directly from the cached daily data, equals the `weather_stat` tool's value.
- The Midwest ranking contains only Midwest hubs, sorted descending, and all scores are within 0–100.
- Same inputs give the same scores (run twice and compare).

**Layer B: agent cases (LLM), checked by code rather than by another LLM**
1. Midwest winter ranking → `rank_hubs` called with region=Midwest, category=winter; the answer's top hub = the engine's top hub.
2. Miami vs Houston on hurricane + flood → `compare_hubs` called with both hubs.
3. % of days with snowfall in Denver last year → the value matches the tool output.
4. Why is Dallas's risk high? → `explain_hub_score(dallas)` is called; the answer names Dallas's top-contributing category.
5. Follow-up: "Rank Midwest hubs for winter" → "And for flooding?" → the second turn uses region=Midwest, category=flood.
6. Unknown hub ("our Boston hub") → `out_of_scope` = true, and no invented score.
7. False premise ("Why is Phoenix's winter risk so high?") → the answer contradicts the premise.
8. Top 3 hubs overall for investment → matches the engine's top 3.
9. Forecast question ("Will it snow in Chicago next week?") → flagged out of scope.
10. Chicago snow days in 2024 → the value matches the tool output.

Every case also checks that the output is a valid `AgentAnswer` and that each `key_figures` value appears in a tool result.

## 11. Bonus (only after §1–10 work)

**Alert when a hub's risk changes.** The investment exposure score is meant to be stable: it changes only when the data or the weights change. That makes it the wrong signal for day-to-day alerts. The plan:
- Add a separate `current_risk` per hub, based on NWS active alerts (alert severity mapped to 0–100).
- `POST /scores/refresh` recomputes both scores, compares them to the last snapshot, and if a change crosses the threshold, POSTs to `ALERT_WEBHOOK_URL` (or logs it if the URL isn't set). Cron or any external scheduler can call this endpoint, which covers both the "scheduled" and "webhook-triggered" options.

**Deploy** to a public URL (e.g. Render) if time remains. **Voice:** not planned.

## 12. Repository structure

```
weather-risk-agent/
├── README.md            # how to run, assumptions, limitations
├── CLAUDE.md            # working rules for Claude Code
├── PLAN.md              # this file
├── DESIGN.md            # architecture, repo structure, storage choice
├── requirements.txt
├── .env.example         # OPENAI_API_KEY, OPENAI_MODEL, ALERT_WEBHOOK_URL
├── .gitignore           # .env, .venv/, caches, data/*.db
├── data/
│   ├── hubs.json
│   └── cache.db         # created by refresh; not in git
├── app/
│   ├── main.py          # FastAPI routes; serves the UI
│   ├── config.py        # thresholds, caps, weights
│   ├── db.py            # SQLite cache + score snapshots
│   ├── refresh.py       # fetch all data, compute + store scores
│   ├── sources/
│   │   ├── open_meteo.py
│   │   ├── fema_nri.py
│   │   └── nws.py
│   ├── scoring.py       # deterministic scoring engine
│   ├── tools.py         # tool functions + their JSON schemas
│   ├── schemas.py       # Pydantic models (tool args, AgentAnswer)
│   ├── agent.py         # LLM loop
│   └── alerts.py        # bonus
├── web/
│   └── index.html
├── evals/
│   ├── cases.json
│   └── run.py
└── ai-sessions/         # exported AI agent sessions (deliverable)
```

## 13. Build phases & gates

### How every phase runs

1. **[Coral]** Opens a new Claude Code session in the repo folder and sends this prompt, with N filled in:
   ```
   Execute Phase N only: PLAN.md §13 and the sections it references. Stop at Gate N.
   ```
   Claude Code loads `CLAUDE.md` automatically, so the working rules don't need repeating in the prompt. One session per phase keeps each session focused. PLAN.md carries the context, and the exported build sessions (Phase 8) line up with the phases.
2. **[CC]** (Claude Code) Does the steps marked [CC], in order, following `CLAUDE.md`. If something doesn't match this plan, or an API returns something unexpected, it stops and reports.
3. **[CC]** At the gate, shows the listed output and stops.
4. **[Coral]** Reviews against the gate checklist. If something needs fixing, she says what; Claude Code fixes it and shows the gate output again.
5. **[Coral]** When the gate passes, commits with the suggested message and moves to the next phase.

### Overview

| Phase | Build | Target |
|---|---|---|
| 0. Setup | git repo, `.gitignore`, `.env.example`, `requirements.txt`, venv | ~15:15 |
| 1. Data | `hubs.json`, `db.py`, Open-Meteo + NRI clients, `refresh` | ~16:00 |
| 2. Scoring | `config.py`, `scoring.py`, score snapshots | ~17:30 |
| 3. Agent | schemas, tools, agent loop, terminal CLI | ~19:30 |
| 4. API + UI | FastAPI, `index.html` | ~21:00 |
| 5. Evals | cases + runner | ~22:00 |
| 6. Docs | README, DESIGN.md | ~23:00 |
| 7. Bonus | NWS client, alerts, deploy | morning, optional |
| 8. Submit | session export, clean run | before the deadline |

### Phase 0: Setup

**Steps**
1. **[Coral]** In the `weather-risk-agent` folder, run `git init`.
2. **[CC]** Create `.gitignore` with: `.env`, `.venv/`, `__pycache__/`, `*.pyc`, `data/*.db`.
3. **[CC]** Create `.env.example` with `OPENAI_API_KEY=`, `OPENAI_MODEL=`, `ALERT_WEBHOOK_URL=` (empty values).
4. **[CC]** Create `requirements.txt` with only what Phase 1 needs: `requests`, pinned to an exact version. Each later phase adds its own dependencies.
5. **[CC]** Create the virtual environment (`python3 -m venv .venv`) and install `requirements.txt` into it.
6. **[Coral]** Run `cp .env.example .env`. The values get filled in at Phase 3.

**Gate 0: Claude Code shows**
- The Python version in `.venv`, and confirmation that the install finished without errors.

**Gate 0: Coral checks**
- Run `git status`. It lists `.gitignore`, `.env.example`, `requirements.txt`, `PLAN.md` and `CLAUDE.md`, and does **not** list `.env` or `.venv/`.

**Commit:** `phase 0: project setup`

### Phase 1: Data

**Steps**
1. **[CC]** Create `data/hubs.json` from the §4 table, with every column.
2. **[CC]** Create `app/db.py`. The SQLite file is `data/cache.db`, with both tables from §9. `score_snapshots` stays empty until Phase 2.
3. **[CC]** Create `app/sources/open_meteo.py`: for each hub, the daily variables in §5 from 2021-01-01 to 2025-12-31, with `timezone=auto` so days follow local time. Metric units (Open-Meteo's default).
4. **[CC]** Create `app/sources/fema_nri.py`. Query the §5 ArcGIS layer by `STCOFIPS` and confirm `NRI_VER` is "December 2025". Fetch the §5 hazards for the 14 FIPS codes, for all three candidate field types (`*_RISKS`, `*_AFREQ`, `*_EALS`), so Gate 1 can compare them. Store only the 14 counties' rows.
5. **[CC]** Create `app/refresh.py`. `python -m app.refresh` calls the two clients and writes every response to `api_cache`. Refresh always fetches fresh data; everything else in the app reads only from the cache.
6. **[CC]** Run the refresh.

**Gate 1: Claude Code shows**
- For Denver and Miami: a sample of raw daily Open-Meteo values, and the NRI values for all three field types.
- A table of all 14 hubs: FIPS code → the county name NRI returned.
- The `NRI_VER` value returned (expected: "December 2025").
- Denver 2025: days with snowfall ≥ 0.25 cm, days with ≥ 1 cm, and days above 0 but below 0.25 cm.
- A proposal for the NRI field type: the three types side by side for a few hazards, the scale of each, and a recommendation with its reason.

**Gate 1: Coral checks**
- The values are plausible: Denver has snow days and Miami has almost none; Miami's hurricane values are high.
- All 14 county names match §4.
- Decide the NRI field type. **[CC]** then writes the decision into §6.

**Commit:** `phase 1: data sources and cache`

### Phase 2: Scoring

**Before you start:** the NRI field type was decided at Gate 1.

**Steps**
1. **[CC]** Create `app/config.py` with every threshold (§3, §6), the cap for each weather metric and each NRI hazard's AFREQ, the NRI/weather split and the category weights (§14). Propose each cap value with a one-line reason.
2. **[CC]** Create `app/scoring.py` per §6: NRI part, weather part, category scores, overall score, and the full breakdown. It reads only from the cache.
3. **[CC]** In `app/scoring.py`, add the yearly weather statistics that the Phase 3 `weather_stat` tool will use (e.g. % of days with snowfall ≥ 0.25 cm, snow days ≥ 1 cm), computed from the cached daily data.
4. **[CC]** Extend `refresh`: after fetching, compute the scores and write one `score_snapshots` row per hub.
5. **[CC]** Run the refresh.

**Gate 2: Claude Code shows**
- A ranking table of all 14 hubs: the overall score and the five category scores.
- Dallas's full breakdown: inputs → parts → categories → overall.
- Denver 2025: % of days with snowfall (≥ 0.25 cm) and with ≥ 1 cm.
- The proposed cap values with their reasons.

**Gate 2: Coral checks**
- The ranking makes sense (e.g. Miami high on hurricane_wind, Minneapolis high on winter, Phoenix high on heat and low on winter).
- Dallas's breakdown adds up.
- Approve or change the caps.

**Commit:** `phase 2: scoring engine`

### Phase 3: Agent

**Before you start:** **[Coral]** fills in `OPENAI_API_KEY` and `OPENAI_MODEL` in `.env`.

**Steps**
1. **[CC]** Add `openai`, `pydantic` and `python-dotenv` to `requirements.txt` (pinned) and install them.
2. **[CC]** Create `app/schemas.py`: Pydantic models for each tool's arguments and for `AgentAnswer` (§7).
3. **[CC]** Create `app/tools.py`: the five tools in §7, each with a strict JSON schema, each calling the scoring engine.
4. **[CC]** Create `app/agent.py`: the loop, retry and error handling, and system-prompt rules in §7, with history per `session_id` kept in memory.
5. **[CC]** Add a terminal CLI: `python -m app.agent` opens an interactive prompt and keeps history for the run, so follow-ups work.

**Gate 3: Claude Code shows** the answers to the four assignment questions (§10, cases 1–4), each with its tool calls and the full `AgentAnswer` JSON.

**Gate 3: Coral checks**
- Every number in each answer appears in a tool result.
- The tools called make sense for each question.

**Commit:** `phase 3: agent and tools`

### Phase 4: API + UI

**Steps**
1. **[CC]** Add `fastapi` and `uvicorn` to `requirements.txt` (pinned) and install them.
2. **[CC]** Create `app/main.py` with the endpoints in §8, except `POST /scores/refresh` (Phase 7). It also serves `web/index.html`.
3. **[CC]** Create `web/index.html` per §8.
4. **[CC]** Confirm the single run command, `uvicorn app.main:app`, with the app at `http://localhost:8000`.

**Gate 4: Claude Code shows** the run command, and that `GET /health` responds.

**Gate 4: Coral checks**, in the browser:
- Ask "Rank Midwest hubs for winter", then "And for flooding?". The second answer covers Midwest hubs and flooding.
- The "How I got this" panel shows the tools used, the assumptions and the confidence.

**Commit:** `phase 4: API and chat UI`

### Phase 5: Evals

**Steps**
1. **[CC]** Create `evals/cases.json` with the ten cases in §10 Layer B and their checks.
2. **[CC]** Create `evals/run.py` per §10: Layer A and Layer B, a pass/fail table with a summary, results saved to `evals/results.json`.
3. **[CC]** Run `python -m evals.run`.

**Gate 5: Claude Code shows** the pass/fail table and, for each failure, its cause and a proposed fix.

**Gate 5: Coral checks**
- Decide which fixes to make. Claude Code makes them and reruns.
- No case's expected result is changed to make it pass without Coral's approval.

**Commit:** `phase 5: evals`

### Phase 6: Docs

**Steps**
1. **[CC]** Write `README.md`:
   - What the agent does, in a few lines.
   - Setup from a fresh clone, as exact commands: create the venv, install, `cp .env.example .env` and fill it in, run `python -m app.refresh` (required, because the cache isn't in git), start the server, run the evals.
   - Assumptions (§3), thresholds and weights (from `config.py`), out of scope (§2), and limitations (data resolution, NRI's final December 2025 release, in-memory sessions).
2. **[CC]** Write `DESIGN.md`: architecture, repo structure, scoring model, storage choice (§9), and why there's no agent framework (§7).

**Gate 6: Coral checks**
- Both files match what was actually built.
- Neither contains a claim she can't defend.

**Commit:** `phase 6: docs`

### Phase 7: Bonus (optional)

**Before you start:** Phases 0–6 are committed.

**Steps**
1. **[CC]** Create `app/sources/nws.py`: active alerts at each hub's point (§5), sent with a `User-Agent` header. Add it to `refresh`.
2. **[CC]** Create `app/alerts.py` and `POST /scores/refresh` per §11, including `current_risk` from NWS alerts.
3. **[CC]** Only if time remains (§14): deploy to Render and add the public URL to the README.

**Gate 7: Claude Code shows** an alert firing (the webhook POST or the log line) and how it was triggered.

**Commit:** `phase 7: bonus`

### Phase 8: Submit

**Steps**
1. **[Coral]** Export the AI sessions she's submitting (the planning chat and the Claude Code build sessions) into `ai-sessions/`.
2. **[CC]** Clean-run test: clone the repo into a new temporary folder, copy in the existing `.env`, and follow only the README, command by command. Report every step that didn't work as written.
3. **[CC]** Fix what the clean run found, with Coral's approval.
4. **[Coral]** Make the final commit and submit.

## 14. Decisions (approved by Coral, 2026-11-22)

1. Hub list in §4: approved as is.
2. Scoring: 50/50 NRI vs observed weather, equal category weights: approved.
3. UI: a single HTML page served by FastAPI.
4. Deploy to a public URL: only at the end, if time remains.

## 15. Risks

| Risk | Mitigation |
|---|---|
| NRI is no longer updated, and its old website and downloads are gone | Use FEMA's ArcGIS layer (December 2025, the latest release), cache it after the first refresh, and state the version in the README |
| An external API is down during the live demo | Everything is served from the SQLite cache after one refresh |
| The LLM states numbers that didn't come from tools | System-prompt rule, plus an eval check that every key figure appears in a tool output |
| Running out of time | Phases are ordered by value, the bonus comes last, and the scope is narrow |
