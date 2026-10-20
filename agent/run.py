"""Salmon fishing agent: answers questions from DNR weekly reports through read-only SQL tools.

Usage:
  python -m agent.run "Where should I fish for salmon out of Holland this weekend?"
  python -m agent.run --today 2025-09-12 "..."     # pretend it's another date (backtesting; no future data visible)

No framework: a plain tool-use loop. The model decides which tools to call and when it has enough
to answer; this code runs the tools and feeds results back. Every step is printed.
"""
import argparse
import json
import sys
import textwrap
import time
from datetime import date

import anthropic
from dotenv import load_dotenv

from agent.tools import TOOLS, run_tool
from config.models import AGENT_DEFAULT_CLI, AGENT_MODELS
from config.pricing import cost, usage_tokens

load_dotenv()

MAX_TURNS = 8

SYSTEM = """You help an angler decide where to fish for salmon (Chinook and coho) on Lake Michigan in Southwest Michigan, \
using Michigan DNR weekly fishing reports that have been extracted into a database you query with tools.

Ports from north to south: Muskegon, Grand Haven, Holland, South Haven, St. Joseph. Holland sits between Grand Haven \
(to the north) and South Haven (to the south), each roughly 20-25 miles away. The DNR rarely reports on Holland, so for \
Holland questions use Grand Haven and South Haven and say that you did.

How to answer:
- Ground every claim in the tool results. After each claim, cite the report date and port, e.g. (Grand Haven, 2026-09-23). \
Never state fishing details the reports don't contain.
- Say how old the newest report is. Reports are weekly and often weeks old, and the season changes fast in the fall, \
so treat old reports as a guide, not as current conditions.
- Recent reports are the strongest signal. Use earlier seasons at the same time of year to fill gaps and to say what \
usually happens next, and label that clearly as history.
- Catch quality runs 0 (slow) to 4 (excellent). "Not rated" means the fish were reported but the report didn't say how \
well fishing went: treat it as a weak positive, not as bad fishing.
- Water depth ("in 120 feet of water") and fish depth ("55 feet down") are different; give both when you have them.
- Mention any access issue (closed pier or launch) in the reports.
- Water and weather: call get_current_conditions for the ports you recommend (and when the question is about now or this weekend). Surface water temperature comes from buoys and is MEASURED; the cold-layer depth is MODELED by NOAA (LMHOFS) and approximate (its surface was ~2 C off the buoys in testing), so give it as a range and call it modeled. The buoys sit miles offshore; for pier fishing use the modeled pierhead water and its ~3-day change (a sharp drop is the upwelling sign that brings salmon to the piers). Report weeks come with that week's buoy surface temperature, so you can compare this year's reports and earlier seasons with today's water; find_similar_weeks matches on surface temperature as well as calendar, so call get_current_conditions before it and pass the measured buoy temperature.
- Answer format (Markdown, concise; the reader should get the plan from the first section alone):
  1. "## Recommendation": the plan as 2-4 short bullets of plain advice: boat or pier, where (water depth or area), how \
deep to run lines, which lures. Advice only: no reasons, numbers from sources, or citations here.
  2. "## Why": 2-4 bullets, one plain sentence each, giving the main reason behind one part of the plan. The reason must \
directly support that advice (e.g. "Boats beat the pier: the latest reports rated boats good and the pier slow"), never a \
loosely related fact. Name the source in ordinary words inside the sentence ("the Sept 30 Grand Haven report", "today's \
Holland buoy", "the NOAA lake model", "past Octobers"). No bracketed tags. Start your own inferences with "I'd expect".
  3. "## Evidence": short subsections by source (Recent reports, Past seasons, Conditions now) with the details and quotes \
behind the recommendation, cited as (Port, date). Skip anything that doesn't support or qualify it.
  4. "## Caveats": 2-4 bullets (report age, modeled vs measured, gaps).
  5. "## Sources": the bulletin links you relied on.
- Keep three kinds of statements apart: what the reports say (cite them), general guidance from the reference (name the \
source), and your own inferences from combining them. Phrase your own inferences as \
expectations ("I'd expect..."), never as facts.
- Use the reference below for what conditions mean. Label anything from it as general guidance, separate from the reports, and never present general guidance as something a report said.

Reference (general guidance, cite the source when you use it):
- Preferred water temperature (NY DEC, "Trolling for Lake Ontario trout and salmon"): Chinook and coho 52-58 F (optimum 53), steelhead 55-65 F, brown trout 54-63 F, lake trout 42-52 F. Salmon hold at whatever depth gives them that temperature, which is why the cold-layer depth matters more than the surface.
- Michigan Sea Grant / MSU Extension: mature Chinook in early fall hold in or near water of 55 F or less. North or east winds push warm surface water offshore and cold water wells up along Michigan's shore (upwelling), which brings salmon toward pierheads and river mouths. A sharp drop in buoy surface temperature is the sign of an upwelling.
- When the lake is stratified (roughly June-September) the thermocline is a narrow band of fast-dropping temperature (NY DEC: often only 5-15 ft thick); in spring and late fall the lake mixes and depth matters less.
- Wind and waves: for small boats, say so plainly when the forecast is strong (e.g. sustained 20+ mph or high waves) and suggest the pier or river as the alternative; safety comes before the bite."""


HISTORY_NOTE = (
    "\n\nConditions history is on: report weeks and conditions also show (1) surface water near the pier from the GLSEA "
    "satellite analysis (NOAA GLERL), a cell ~1-2.5 km off the pierhead, closer to shore than the buoys and available when "
    "they are not; it blends several days of satellite passes, so it can show a fast change 1-2 days late, and the buoy is "
    "better for timing when it has data; and (2) that week's wind. Upwelling wind = hours of wind from the north round to "
    "the east at 8+ mph, which pushes warm surface water offshore so cold water wells up along this shore (Michigan Sea "
    "Grant). Weigh wind, buoy and satellite together; label satellite values as satellite.")


NO_REPORTS_NOTE = ("\n\nNo DNR fishing reports are available for this question, and the report tools are switched off. "
                   "Base your answer on the current conditions and the reference above, and say so.")


def run_agent(question: str, today: str, model: str = AGENT_DEFAULT_CLI, report_tools: bool = True,
              conditions_history: bool = False):
    """The agent loop. Yields one event dict per step, so the CLI can print them and the web page can stream them.

    report_tools=False leaves only get_current_conditions (used by the agent eval to test conditions alone).
    conditions_history=True adds GLSEA satellite water near the pier and the past week's wind to report weeks and
    conditions (off until the eval shows it helps).
    """
    client = anthropic.Anthropic()
    tools = TOOLS if report_tools else [t for t in TOOLS if t["name"] == "get_current_conditions"]
    system = (SYSTEM + f"\n\nToday is {today}." + ("" if report_tools else NO_REPORTS_NOTE)
              + (HISTORY_NOTE if conditions_history else ""))
    messages = [{"role": "user", "content": question}]
    total = {"input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0}
    t0 = time.monotonic()

    for turn in range(1, MAX_TURNS + 1):
        resp = client.beta.messages.create(
            model=model, max_tokens=16000, system=system, tools=tools, messages=messages,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",  # retry on a fallback model if declined
        )
        tok = usage_tokens(resp.usage)
        for k, v in tok.items():
            total[k] += v
        chars = lambda kind: sum(len(getattr(b, kind, "") or "") for b in resp.content)
        yield {"type": "model_call", "turn": turn, "model": resp.model, **tok, "cost": cost(model, **tok),
               "thinking_chars": chars("thinking"), "text_chars": chars("text")}

        if resp.stop_reason == "refusal":
            yield {"type": "error", "message": "the request was declined"}
            break
        tool_calls = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason != "tool_use" or not tool_calls:
            yield {"type": "answer", "text": "\n".join(b.text for b in resp.content if b.type == "text"),
                   "truncated": resp.stop_reason == "max_tokens"}
            break

        messages.append({"role": "assistant", "content": resp.content})  # includes thinking + tool_use blocks
        results = []
        for call in tool_calls:
            yield {"type": "tool_call", "turn": turn, "name": call.name, "input": call.input}
            try:
                out, is_error = run_tool(call.name, call.input, today, conditions_history), False
            except Exception as e:  # report tool failures to the model instead of crashing
                out, is_error = f"Tool error: {e}", True
            yield {"type": "tool_result", "turn": turn, "name": call.name, "chars": len(out), "content": out, "is_error": is_error}
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": out, "is_error": is_error})
        messages.append({"role": "user", "content": results})  # all results for this turn in one message
    else:
        yield {"type": "error", "message": f"stopped after {MAX_TURNS} turns without a final answer"}

    yield {"type": "done", "model_calls": turn, **total, "cost": cost(model, **total), "seconds": time.monotonic() - t0}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--model", default=AGENT_DEFAULT_CLI, choices=AGENT_MODELS)
    ap.add_argument("--today", default=date.today().isoformat(), help="treat this as today; later reports are invisible")
    args = ap.parse_args()

    for e in run_agent(args.question, args.today, args.model):
        if e["type"] == "model_call":
            print(f"  [call {e['turn']}: {e['input_tokens']:,} in + {e['cache_read_tokens']:,} cache read + "
                  f"{e['cache_write_tokens']:,} cache write / {e['output_tokens']:,} out · ${e['cost']:.3f} · "
                  f"thinking {e['thinking_chars']:,} chars, text {e['text_chars']:,} chars · {e['model']}]", file=sys.stderr)
        elif e["type"] == "tool_call":
            print(f"\n→ turn {e['turn']}: {e['name']}({json.dumps(e['input'])})")
        elif e["type"] == "tool_result":
            print(f"  ← {e['chars']:,} chars: {textwrap.shorten(e['content'].replace(chr(10), ' | '), 160)}")
        elif e["type"] == "answer":
            if e["truncated"]:
                print("\n[answer cut off at max_tokens]")
            print("\n" + "=" * 80 + "\n" + e["text"])
        elif e["type"] == "error":
            print(f"\n[{e['message']}]")
        elif e["type"] == "done":
            print(f"\n[{e['model_calls']} model calls · {e['input_tokens']:,} in + {e['cache_read_tokens']:,} cache read + "
                  f"{e['cache_write_tokens']:,} cache write / {e['output_tokens']:,} out tokens · "
                  f"${e['cost']:.3f} · {e['seconds']:.0f}s]", file=sys.stderr)


if __name__ == "__main__":
    main()
