# Lake Michigan Salmon Agent

Ask where to fish for salmon in Southwest Michigan and get an answer grounded in Michigan DNR weekly fishing reports, live buoy water temperatures, a NOAA lake model of the cold layer, and the wind forecast. Every claim cites the report or source behind it.

```
$ python -m agent.run "I'm launching out of Holland this weekend. Where and how deep should I fish for salmon?"

→ turn 1: list_ports({})
→ turn 1: get_recent_reports({"ports": ["Holland", "Grand Haven", "South Haven"], "weeks_back": 6, ...})
→ turn 1: get_current_conditions({"ports": ["Holland", "Grand Haven", "South Haven"]})
→ turn 1: find_similar_weeks({"ports": [...], "around_date": "2026-10-18", "window_days": 14, ...})
→ turn 2: find_similar_weeks({..., "surface_temp_f": 54, "temp_tolerance_f": 6})
...
## Recommendation
Troll 110–160 ft of water with lines 50–85 ft down ... but today is too rough for a small boat (buoy 45029: 20 mph,
5.9 ft waves); Tuesday's east wind is the window ...
```

## How it works

```
 DNR weekly reports (GovDelivery HTML)                NOAA buoys (NDBC)   NOAA LMHOFS 3D model   Open-Meteo
        │                                                     │                    │                 │
 extraction/fetch_reports.py  split by region/port (no LLM)  │                    │                 │
        ▼                                                     ▼                    ▼                 ▼
 data/chunks.jsonl ─► extraction/extract.py ─► data/fishing.db ◄── conditions/: buoy history, daily lake-model
                      LLM, structured           observations       profiles (pier + offshore), wind forecast
                      output, verbatim          per species
                      evidence check            and method
                            ▲                         │
   evals/extraction/score.py┘                         ▼
   30 hand-labeled paragraphs     agent/tools.py (read-only SQL + conditions) ◄── agent/run.py: tool-use loop
```

1. **Fetch and split (no LLM).** `extraction/fetch_reports.py` downloads report web copies and splits them into one paragraph per port with BeautifulSoup. Deterministic parsing wherever possible; the LLM only does what needs judgment.
2. **Extract (LLM).** `extraction/extract.py` turns each paragraph into observations: species, method (boat / pier / shore / river), water depth, fish depth, lures, catch quality 0–4, plus verbatim evidence sentences. It uses structured outputs (schema-constrained decoding plus Pydantic validation). Code then checks that every evidence sentence appears word for word in the report, with one retry if not. Every row records model, prompt version, tokens, latency, and cost.
3. **Evaluate.** `evals/extraction/score.py` scores any model and prompt version against hand labels. Details below.
4. **Conditions.** `conditions/` adds measured surface water temperature from the nearest buoy (with the week's trend and a possible-upwelling flag), the modeled water at each pierhead with its ~3-day change, and the modeled cold-layer depth offshore: the depth where water first reaches 55 °F, and the 52–58 °F band where Chinook and coho prefer to hold.
5. **Agent.** `agent/run.py` is a plain tool-use loop over four read-only tools. The model decides which tools to call and when it has enough. Answers separate what the **reports** say, **general guidance** (cited: NY DEC, Michigan Sea Grant), and the agent's **own read**.

## Design decisions

| Decision | Why |
|---|---|
| **SQL tools, not vector search (RAG)** | The data is structured, and the questions are filters and rankings ("recent", "this port", "salmon"). SQL answers those exactly. |
| **No agent framework** | The loop is ~50 lines against the Messages API. Every step is visible, and the turn limit, error handling, and as-of date are explicit. The SDK's tool runner would work too; worth switching as it grows. |
| **Claude Opus 5 for extraction and the agent** | Measured, not assumed: see the model comparison. Haiku 4.5 is 7× cheaper but breaks structural rules that more prompting didn't fix. |
| **Null catch quality ≠ bad fishing** | "Boat anglers found Chinook" says the fish were there, not how well fishing went. Extraction stays faithful to the text; the agent treats null as a weak positive. Assumptions live in the agent, not the data. |
| **Measured vs. modeled, always labeled** | Buoy surface temperature is measured. The cold-layer depth is a model whose surface was ~2 °C off the buoys in a 7-point check, so it's given as a range and called "modeled". |
| **No peeking ahead** | Every tool takes an as-of date and never returns later data, so `--today 2025-09-12` is an honest backtest. |
| **Conditions from data, not report text** | Wind and water temperature come from buoys and models instead of the DNR's wording, which was coarse and inconsistent. |

## How we know it works

### Extraction eval
- **Answer key:** 30 report paragraphs, stratified by port and season, plus 7 hand-picked hard cases. 23 are dev (used for tuning), 7 are test (held out). Labeled under a written [labeling guide](evals/extraction/LABELING_GUIDE.md); the test labels and 11 dev labels were made without seeing any model output.
- **Scoring:** deterministic (`evals/extraction/score.py`), no LLM judge. Rows are matched by (species, method), then each field is compared: water depth, fish depth, lures (a fixed list of 18 types), and catch quality (labels can accept a range like `[2, 3]` where the wording is honestly in between). Rows can be marked optional when the text supports both readings. Results come in two scopes, all species and salmon only, with dev and test reported separately. Test mismatches are hidden by default so the prompt can't be tuned against them.
- **The scorer is tested too:** `evals/extraction/selftest.py` checks that a perfect copy of the labels scores 100% and that each of 8 planted mistakes is counted exactly once.

### Results (salmon observations)

| | Opus 5 | Haiku 4.5 |
|---|---|---|
| **Dev, first eval (v8)** | recall 95% · precision 89% · water depth 85% · quality 98% | 90% · 88% · 82% · 84% |
| **Dev, current prompt (v11-def)** | recall **100%** · precision **100%** · water depth 98% · quality **100%** | 95% · 93% · 95% · 88% |
| **Test, held out, as first measured** | recall 78% · precision 64% | 78% · 64% |
| **Test, after aligning old labels to current rules** | recall 90% · precision 82% · water depth 100% · quality 89% | 90% · 82% · 89% · 100% |
| **Cost per paragraph** | $0.042 | $0.006 |

Except for "as first measured", every row is scored against the current labels, so they're comparable (the numbers as measured at each step are in the eval log).

How to read this honestly:
- **Dev is now tuned to its own examples.** The prompt rules were written from dev paragraphs, and some dev labels were corrected after seeing model output. Read the dev numbers as fit, not accuracy. That's also why v8's catch quality looks high (98%): several quality labels were corrected toward what v8 had already output.
- **The first test number (78% / 64%) mostly measured label drift,** not model mistakes: the test labels were written on day one under rules that changed later. Both models matched exactly the same rows, which is what pointed there. After aligning those labels to the written rules (decided with the text in front of the labeler, not by copying model output), test is 90% / 82%. The test set has been seen now, so the clean next step is labeling fresh held-out paragraphs.
- **Opus's remaining mistakes are few and specific,** e.g. rating drum from "slow for all species *except* freshwater drum". Haiku's are structural: `river` for boats in a river, duplicate rows, ignored lure mappings. That's the reason for Opus.

### What the eval changed
The full record is in [EVAL_LOG.md](evals/extraction/EVAL_LOG.md), generated from `evals/extraction/history.py`. Highlights:
- **Open vs. closed depth ranges:** "out past 100 ft" was being read as a closed 100, so a new rule was added and water-depth accuracy rose from 85% to 98% on dev.
- **A/B test on catch quality,** a word table vs. level definitions: definitions won, mainly for Haiku (within-1 accuracy 87% → 95%), and they handle wording the table doesn't list.
- **Evidence is a list of sentences** after the model joined non-adjacent sentences into one quote. Evidence failures went to zero.
- **A rule bug caught by the eval:** "Very few boat anglers made it out" created a row; rule 10 is now limited to statements about results.

### The agent
Not formally evaluated yet. What exists: every claim cites a report date and port, with evidence sentences and bulletin links in the tool results; the general reference is cited by source; the agent's own inferences are separated under "My read"; and backtests can't see the future. Spot checks of claims against the database have matched. **Next:** an LLM-judge faithfulness check calibrated on hand-graded answers, and a backtest (did the next week's report confirm the recommended ports?).

## Data sources
- **Michigan DNR Weekly Fishing Report:** public web copies of GovDelivery bulletins. 132 reports, April 2022 to September 2026; 498 Lake Michigan port paragraphs, of which the 116 from the first 27 reports are extracted so far (386 observations).
- **NOAA NDBC buoys:** 45161 Muskegon, 45029 Holland, 45168 South Haven, 45026 Stevensville (St. Joseph). Surface water temperature, wind, waves; history 2022–today.
- **NOAA LMHOFS** (Lake Michigan–Huron Operational Forecast System): temperature on 25 fixed depths from the public AWS archive. Fixed-depth (regular-grid) files start around March 2026; the native-mesh files go back to November 2024. `conditions/lake_model.py` stores one profile per day at each port's pierhead and at ~40/80/120/160 ft of water, reading only those cells over HTTP byte ranges (~10 MB per day instead of the 146 MB file). Modeled.
- **Open-Meteo:** wind forecast.
- **Reference for what conditions mean:** NY DEC, *Trolling for Lake Ontario Trout and Salmon* (preferred temperatures); Michigan Sea Grant / MSU Extension (upwelling, mature Chinook in water of 55 °F or less).

## Run it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env

# Run everything from the project root (python -m <folder>.<file>)
python -m extraction.fetch_reports                                 # download + split reports (data/urls.txt)
python -m extraction.extract --model claude-opus-5                 # LLM extraction of Lake Michigan paragraphs
python -m conditions.water_weather build                           # buoy history into data/fishing.db
python -m conditions.lake_model backfill 2026-03-02 2026-10-18     # daily lake-model profiles (free, ~1 h)
python -m agent.run "Where should I fish for salmon out of Holland this weekend?"
python -m agent.run --today 2025-09-12 "..."                       # backtest as of a past date
python -m agent.run --model claude-sonnet-5-5 "..."                # same question, another model

python -m evals.extraction.score --model claude-opus-5 --model claude-haiku-5-5 --split dev --errors
python -m evals.extraction.selftest                                # the scorer's own test
python -m evals.extraction.labels_tool check                       # validate hand labels
python -m evals.extraction.review_page out.html                    # HTML eval report
python -m evals.extraction.history > evals/extraction/EVAL_LOG.md  # regenerate the eval log
```

## Project layout
The folders follow the data: reports come in through `extraction/`, water and weather through `conditions/`, both land in `data/fishing.db`, and `agent/` answers questions from it. `evals/` is how we know each step works.

```
extraction/        DNR reports → observations
  fetch_reports.py   download bulletins, split into one paragraph per port (no LLM)
  schema.py          what an observation looks like + every prompt version that was run
  extract.py         LLM: paragraph → observations in data/fishing.db
conditions/        water and weather
  water_weather.py   buoys (history + live), wind forecast, the text the agent sees
  lake_model.py      daily NOAA LMHOFS profiles at each pierhead and offshore point
agent/             answering questions
  tools.py           the four read-only tools (SQL over fishing.db + conditions)
  run.py             the tool-use loop (CLI)
evals/extraction/  how we know extraction works
  score.py           deterministic scorer: model output vs. hand labels
  selftest.py        proves the scorer counts planted mistakes exactly
  labels/            30 hand-labeled paragraphs; labels_tool.py picks and validates them
  LABELING_GUIDE.md  the labeling rules
  history.py         the record of every eval run → EVAL_LOG.md
  review_page.py     HTML report comparing labels with each model
data/              everything the code reads and writes
  fishing.db         reports, paragraphs, observations, buoy history, lake-model profiles
  urls.txt, reports.json, chunks.jsonl, raw/   bulletin links, metadata, paragraphs, saved HTML
docs/AGENT_NOTES.md  agent design decisions
experiments/       one-off feasibility checks (water_column.py)
paths.py           every file location in one place
```

## Limitations and next steps
- **Stale coverage:** the newest report is often weeks old, and the agent says so. 105 more reports (2022–2026) are collected but not extracted yet.
- **Holland is almost never reported;** it's served from Grand Haven and South Haven, and the agent says that.
- **The cold layer is modeled,** and only its surface has been checked against measurements. Next: check the modeled cold-layer depth against the fish depths extracted from reports.
- **Fresh held-out test paragraphs** for a clean extraction number; formal agent evals (faithfulness, backtest).
- **Later:** full temperature profiles at depth, historical offshore profiles, more report years.
