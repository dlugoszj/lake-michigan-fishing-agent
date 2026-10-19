"""Extraction schema and versioned prompts, shared by extract.py and the eval."""
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field


Species = Literal["chinook", "coho", "salmon_unspecified", "steelhead", "lake_trout",
                  "pink", "brown_trout",
                  "yellow_perch", "walleye", "smallmouth_bass", "largemouth_bass", "freshwater_drum",
                  "lake_whitefish", "northern_pike", "catfish",
                  "fish_unspecified", "other"]
Method = Literal["boat", "pier", "shore", "river", "unknown"]
Lure = Literal["spoons", "flies", "flasher/fly", "flashers", "rotators", "meat rigs", "plugs", "crankbaits",
               "spawn", "shrimp", "minnows", "nightcrawlers", "spikes", "wigglers", "alewife", "live bait",
               "ned rigs", "other"]


class DepthRange(BaseModel):
    min_ft: Optional[int] = Field(description="Shallow end of the range. Null if not stated or open-ended ('up to 80')")
    max_ft: Optional[int] = Field(description="Deep end of the range. Null if not stated or open-ended ('beyond 120', '120 and deeper')")


class Observation(BaseModel):
    species: Species
    method: Method
    bottom_depth_ft: Optional[DepthRange] = Field(
        description='Water depth where boats fished, e.g. "in 120 feet of water". Null if not stated.')
    fish_depth_ft: Optional[DepthRange] = Field(
        description='Depth of lures/fish below the surface, e.g. "35 feet down". Null if not stated.')
    lures: list[Lure] = Field(description="Lure/bait types anglers used, whether or not they worked")
    lure_details: Optional[str] = Field(
        description="Colors, sizes and rigging for those lures as written, e.g. 'green and orange spoons; glow plugs'. Null if none.")
    catch_quality: Optional[int] = Field(
        ge=0, le=4,
        description="0 none/slow, 1 few, 2 fair/decent, 3 good, 4 limits/excellent. Null if not stated.")
    evidence: list[str] = Field(
        description="The source sentences supporting this observation, one sentence per item, each copied verbatim")


class ChunkExtraction(BaseModel):
    observations: list[Observation] = Field(description="One per species per fishing method mentioned")
    access_issues: Optional[str] = Field(description="Launch closures, pier closures etc. Null if none mentioned")
    no_fishing_info: bool = Field(description="True if the text has no catch information at all")


# Every prompt version that has been run is a plain text file in prompts/, never edited after it's run,
# so results stay reproducible. New version = new file + bump PROMPT_VERSION. What changed and why: EVAL_LOG.md.
#   v10      rules after the first eval rounds (depth ranges, "fish" refers back, method from context, ...)
#   v10-def  v10 with catch quality as level definitions instead of a word table (won the A/B test)
#   v11      v10 + rule 10 limited to results, effort statements, access issues, lure scope,
#            named fish outside the list, "also/too" refers back, depth sentences stay boat
#   v11-def  v11 with the level definitions (current)
PROMPTS_DIR = Path(__file__).parent / "prompts"
PROMPTS = {f.stem: f.read_text() for f in sorted(PROMPTS_DIR.glob("*.txt"))}
PROMPT_VERSION = "v11-def"
SYSTEM_PROMPT = PROMPTS[PROMPT_VERSION]


def user_prompt(port: str, date: str, text: str) -> str:
    return f"Port: {port}\nReport date: {date}\n\n<report_text>\n{text}\n</report_text>"
