# Weather Risk Intelligence Agent

A chat agent that helps logistics analysts decide which US distribution hubs are most exposed to severe weather, and so should come first for resilience investment.

- It covers a fixed list of 14 hubs (`data/hubs.json`).
- It scores each hub's historical exposure, 0–100, in five hazard categories (winter, hurricane, flood, severe storm, heat) and overall.
- It answers rankings, comparisons, weather statistics, "why is this hub's score high?" questions and follow-ups.
- **The LLM doesn't compute numbers.** Python code computes every score, statistic and ranking. The LLM picks which tool to call and explains the result.

Data: Open-Meteo daily weather history (2021–2025) and the FEMA National Risk Index (county level, December 2025 release). Both are free and need no key.

The design is explained in [DESIGN.md](DESIGN.md). The build plan and its decisions are in [PLAN.md](PLAN.md).

## How it works

Two separate paths: refresh fetches the data into a SQLite cache, and chat answers questions from that cache.

```mermaid
flowchart TB
    subgraph chat["Chat (online)"]
        browser["Browser (web/index.html)"]
        api["FastAPI (app/main.py)"]
        agent["Agent loop (app/agent.py)"]
        openai["OpenAI API<br/>(picks tools, writes the answer)"]
        tools["Tools (app/tools.py)<br/>arguments validated by Pydantic (app/schemas.py)"]
        scoring["Scoring engine (app/scoring.py)<br/>thresholds, caps, weights: app/config.py"]

        browser -->|"POST /chat {message, session_id}"| api
        api --> agent
        agent <--> openai
        agent -->|"tool name + arguments"| tools
        tools --> scoring
    end

    cache[("SQLite cache (data/cache.db)")]

    subgraph refresh["Refresh (offline)"]
        refresh_cmd["python -m app.refresh"]
        sources["Open-Meteo, FEMA NRI<br/>(the only external data calls)"]

        refresh_cmd --> sources
    end

    scoring -->|"reads only"| cache
    refresh_cmd -->|"writes"| cache
```

Each part is explained in [DESIGN.md](DESIGN.md#architecture).

## Setup

You need Python 3 (built and run on 3.14; older versions are untested) and an OpenAI API key.

```bash
git clone https://github.com/Coral3la/Weather-Risk-Agent.git
cd Weather-Risk-Agent

# 1. Virtual environment and dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configuration
cp .env.example .env
# Open .env and fill in:
#   OPENAI_API_KEY=<your key>
#   OPENAI_MODEL=<a model that supports structured outputs and tool calling; built and evaluated with gpt-4o>
# ALERT_WEBHOOK_URL is not used yet; leave it empty.

# 3. Fetch the data and compute the scores (required)
python -m app.refresh

# 4. Start the server
uvicorn app.main:app
# Open http://localhost:8000
```

**Step 3 is required.** The database (`data/cache.db`) isn't in git; refresh creates it. Refresh is the only command that calls the weather and NRI APIs. It needs no API key. If refresh hits Open-Meteo's free-tier rate limit, it prints "Open-Meteo rate limit reached; waiting …" and retries after the wait the API asks for.

After the refresh, the app reads only from the cache. The only network call left is to OpenAI.

### Other commands

| What | Command |
|---|---|
| Chat in the terminal instead of the browser | `python -m app.agent` |
| Run the evals | `python -m evals.run` |

### API

| Endpoint | What it returns |
|---|---|
| `GET /` | The chat page |
| `POST /chat` with `{"message": "...", "session_id": "..."}` | `{session_id, answer, error, trace, retries}`. Omit `session_id` to start a conversation; send it back for follow-ups. |
| `GET /hubs` | The 14 hubs |
| `GET /scores?category=&region=` | Hubs ranked by score. `category` is one of the five categories or `overall` (the default). |
| `GET /health` | `{"status": "ok", "scores_computed_at": ...}`: the time of the last refresh, or null if it never ran |

## Assumptions

| Topic | Decision |
|---|---|
| Hub list | The assignment gave no hub list. We defined 14 hubs in major US logistics cities, each placed at its city-center coordinates. |
| Regions | US Census Bureau regions. Midwest = IL, IN, IA, KS, MI, MN, MO, NE, ND, OH, SD, WI. |
| "Exposure" | How often hazards have historically hit the hub's location. It is not a forecast. |
| "Last year" | The last full calendar year in the data: 2025. |
| "Day with snowfall" | Daily snowfall ≥ 0.25 cm (0.1 in, the NWS threshold for measurable snowfall). Snow answers also report days with ≥ 1 cm. |
| Observation window | Weather metrics in the score are yearly averages over 2021–2025. |
| Missing NRI values | A hazard with no NRI record for a county (e.g. coastal flooding inland) counts as 0, and the breakdown says "no NRI record". |

## Scoring: thresholds, caps and weights

Every value below lives in `app/config.py`. Nothing is hard-coded elsewhere.

Each category score combines two parts, each on a 0–100 scale:

- **NRI part:** for each hazard, `min(annualized frequency / cap, 1) × 100`, then the mean over the category's hazards.
- **Weather part:** for each metric, `min(days per year / cap, 1) × 100`, then the mean over the category's metrics.
- **Category score** = 50% NRI part + 50% weather part. A category with no weather metric is 100% NRI.
- **Overall score** = the mean of the five category scores (equal weights).

| Category | NRI hazards | Weather metrics |
|---|---|---|
| winter | winter weather, ice storm, cold wave | snow days, severe-cold days |
| hurricane | hurricane | none (NRI only) |
| flood | inland flooding, coastal flooding | heavy-rain days |
| severe_storm | tornado, hail, strong wind | none (NRI only) |
| heat | heat wave | extreme-heat days |

**Weather metrics** (Open-Meteo daily data)

| Metric | A day counts when | Cap (days per year = score 100) | In the score? |
|---|---|---|---|
| Days with snowfall | snowfall ≥ 0.25 cm | n/a | No, statistics only |
| Snow days | snowfall ≥ 1 cm | 30 | Yes |
| Severe-cold days | minimum temperature ≤ −18 °C (0 °F) | 30 | Yes |
| Heavy-rain days | precipitation ≥ 50 mm | 5 | Yes |
| Extreme-heat days | maximum temperature ≥ 38 °C (100 °F) | 30 | Yes |

The weather thresholds and caps are our judgment calls, not official standards (apart from the 0.25 cm snowfall threshold). The reason for each is in a comment next to it in `app/config.py`.

**NRI hazard caps** (occurrences per year = score 100)

| Hazard | Cap | Hazard | Cap |
|---|---|---|---|
| Hurricane | 0.32 | Ice storm | 2.0 |
| Strong wind | 7.0 | Cold wave | 20.0 |
| Inland flooding | 5.5 | Tornado | 1.3 |
| Coastal flooding | 9.0 | Hail | 10.0 |
| Winter weather | 40.0 | Heat wave | 22.0 |

Rule: each cap is the 99th percentile of that hazard's annualized frequency across all US counties in the December 2025 NRI release, rounded. A county in the national top 1% scores 100. The query used to compute them is recorded in `app/config.py`.

**Other settings:** scores are reported to 1 decimal; the agent may request tools at most 5 times per question; an answer that fails validation is retried once.

## Out of scope

The agent says so (and sets `out_of_scope`) when asked for any of these:

- **Forecasts and climate projections.** The scores describe history, not next week or 2050.
- **Business impact:** hub throughput, revenue, upgrade cost. The agent ranks *exposure*, not return on investment. A real investment priority is exposure × business impact, and we have no data for the second factor.
- **Hubs or cities outside the list of 14.**

Not built: voice, authentication, multi-user persistence, live alerts, a public deployment.

## Limitations

**Data**

- **Weather data is gridded, not measured at a station.** Open-Meteo's history is reanalysis on a grid of roughly 9–25 km. Day counts can differ from local NOAA station records, and peak values are smoothed. That is why the score has no wind-gust metric.
- **Each hub is one point at the city center,** not the real site of a warehouse.
- **NRI is county-level.** A value covers the whole county, not the hub's point. Los Angeles County, for example, includes mountains with winter weather.
- **NRI is frozen.** December 2025 is its final release: FEMA retired the standalone NRI site and has said there will be no ongoing updates. Refresh fails with a clear error if the layer ever reports a different version.
- **NRI frequency is not severity.** The annualized-frequency field counts how often a hazard occurs, not how damaging it is.
- **Five years of weather is a short window.** One unusual year moves a hub's average.
- **Hurricane and severe-storm scores rest on NRI alone,** with no observed-weather half.

**Scoring**

- **The weights and caps are judgment calls.** Equal category weights and the 50/50 NRI/weather split are defaults, not calibrated against real disruption data. Changing them changes the ranking.
- **Scores are exposure only.** They don't account for how well a hub is already protected.

**Agent**

- **Sessions are in memory.** Conversation history is lost when the server restarts, and nothing is shared between server processes. There are no user accounts.
- **The number check covers `key_figures` only, not the answer text.** At run time, code checks that every value in the answer's `key_figures` list appears in a tool result, and rejects the answer if one doesn't. It does not scan the prose. A number written only in the answer text is held back by the system prompt, not by code.
- **The check matches values, not meaning.** It confirms a number came from some tool result in the session, not that it carries the right label.

**Evals**

- **Layer B needs an OpenAI key and costs API calls.** It runs the real agent. Layer A (the deterministic checks) needs neither, but the runner always runs both layers.
- **The model is non-deterministic.** Layer B was run 3 times during the build; only the last run, after the final fixes, is saved (`evals/results.json`: gpt-4o, Layer A 3/3, Layer B 10/10). A pass shows the agent can answer a case correctly, not that it always will.
- **Case 7 (false premise) is a phrase match.** It passes when the answer carries Phoenix's real winter score in `key_figures` and its text contains one of a fixed list of phrases such as "not high" or "low" (listed in `evals/cases.json`). It doesn't judge whether the answer as a whole rejects the premise.
- **The `key_figures` eval check has the same limit as the runtime check:** it covers `key_figures`, not numbers in the answer text. Only cases 3 and 10 also look for a specific value in the text.
- **Ten cases is a small set.** It covers the four assignment questions and six edge cases, not the range of questions an analyst might ask.
