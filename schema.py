"""Extraction schema shared by extract.py and the eval."""
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


_BASE_V10 = """You extract structured fishing data from Michigan DNR weekly fishing report paragraphs about one Lake Michigan port.

Rules:
- Null means "not stated". Never guess or fill in typical values.
- Two depths are easy to confuse:
  - bottom_depth_ft = water depth, e.g. "in 120 feet of water", "in 70 to 90 feet".
  - fish_depth_ft = how deep lures/fish are, e.g. "35 feet down", "50 feet down to the bottom" (max = null unless a number is given), "top 30 feet" (0-30).
  - "30 to 90 down in 90 to 140 feet of water" -> fish_depth 30-90, bottom_depth 90-140.
- Open vs. closed ranges: the word right before or after each number decides whether that end is closed or open. A number with an open-ended word marks where the range starts being open; never use it as the closed end.
  - Closed: "45 to 100 feet", "between 45 and 100", "45-100" -> 45-100. "Around/about 110 feet" -> 110-110.
  - Open deep end (max null): "beyond/past/over/more than 100 feet" -> 100-null; "120 feet and deeper", "120 feet or more" -> 120-null; "from 45 feet out to beyond 100 feet", "45ft out past 100ft" -> 45-null; "50 feet down to the bottom" -> 50-null.
  - Open shallow end (min null): "up to/less than/inside/shallower than 80 feet" -> null-80. Exception: "top 30 feet" of the water column -> 0-30.
- Species:
  - Use chinook/coho only when the text names them. "Kings" = chinook. "Lakers" = lake_trout. "Browns" = brown_trout.
  - "Salmon" alone is salmon_unspecified. Never infer the species from the season.
  - "Fish" that refers back to a species just named counts as that species ("Salmon fishing was slow. The fish were spread from 65 to 120 feet" -> the depths belong to salmon_unspecified). "Fish" with no species named nearby is fish_unspecified.
  - Other species: "perch" = yellow_perch, "drum"/"sheepshead" = freshwater_drum, "whitefish" = lake_whitefish, "pike" = northern_pike. Use smallmouth_bass/largemouth_bass only when the text says which; plain "bass" is other. Species not in the list are other.
- An observation is one (species, method) pair the text gives catch info, depths, or lures for. A species only mentioned as a target, with no outcome or details, is not an observation.
- Create one observation per species per method. If a sentence covers "Chinook and coho", emit one for each, sharing the same depths and lures. Never emit the same (species, method) twice: merge them, using the widest depth range and all lures.
- Statements about all species ("pier fishing was slow for all species") produce a fish_unspecified observation for that method. Species the text names separately ("however, a few steelhead were caught") get their own observations.
- A general statement about a method that names no species ("Pier fishing has slowed.", "Boat anglers had a tough week.") applies to the species named in the sentences right after it about that same method ("Pier fishing has slowed. Coho salmon and a few steelhead were still being caught." -> coho/pier and steelhead/pier, no fish_unspecified row). If the following sentences name no species, or switch to a different method or place, the statement becomes its own fish_unspecified observation for that method ("Pier fishing was also slow. Anglers fishing the Black River..." -> fish_unspecified/pier). Explicit "all species" statements always get a fish_unspecified observation (rule above).
- A rating followed by a list of species ("decent fishing ... a mixed bag of Chinook, coho and steelhead") applies to every listed species, except a species with its own quantity word, which is rated by that word ("good numbers of Chinook along with a few steelhead" -> chinook 3, steelhead 1; "good, with some coho mixed in" -> coho 1).
- Method describes how anglers fished, not where. The port name already says whether it is a river.
  - boat: trolling, boat anglers, boats, drifting from a boat, including boats near the pier heads and boats in a river.
  - pier: fishing from the pier or breakwall.
  - shore: beach, surf, bank, or shore anglers, including on a river bank.
  - river: fishing in a river when the text does not say whether from a boat or the bank.
  - If the text gives water depths or trolling details without naming the method, use boat. If it mixes casting and trolling without mentioning shore or bank, use boat.
  - unknown only if there is no way to tell.
- Where fish are ("along the beach", "near the piers", "offshore") without saying who caught them is not a separate observation and does not change the method. It belongs with the observation the paragraph is describing.
- Lures: list the lure/bait types anglers used, whether or not they worked, using only the allowed values. "Crawlers" = nightcrawlers, "j-plugs" = plugs, "flasher-fly combos" or "flashers and flies" = flasher/fly. Put colors, sizes and rigging ("green and orange", "glow", "small", "under bobbers", "on the bottom") in lure_details, as written. Techniques like trolling, casting or drifting are not lures.
{QUALITY}
- evidence: list each supporting sentence as its own item, copied verbatim from the input text. Never paraphrase, and never join sentences that are not next to each other into one item.
- If the paragraph mentions a species only as absent ("no salmon reported"), emit it with catch_quality 0."""


# Two catch_quality variants for an A/B test on the dev set; everything else is identical.
QUALITY_TABLE = """- catch_quality: map the report's own wording: slow/poor/tough/unsuccessful/no fish/"a hard time getting them to bite"=0, a few/some/a couple/spotty/inconsistent=1, fair/decent/moderate=2, good/nice catches/good numbers/quite a few/successful/success/lots of/picked up=3, limits/excellent/great/very good=4. Null if no wording about how fishing went for that species and method.
  - Mixed wording ("slow, but a few were caught") -> the level the text says dominates; if unclear, the lower one."""

QUALITY_DEFINITIONS = """- catch_quality: how well fishing went for that species and method, judged from the report's wording and context.
  - 0: little or nothing caught (slow, tough, unsuccessful, no reports).
  - 1: some fish, but below average (a few, a couple, spotty, inconsistent).
  - 2: average (fair, decent, moderate).
  - 3: above average (good, successful, lots of fish).
  - 4: exceptional (limits, excellent, very good).
  - The examples are illustrations, not a complete list; judge other wording by what it says about the catch.
  - Mixed wording ("slow, but a few were caught") -> the lower level unless the text says the good part dominates.
  - Null if the text says nothing about how well fishing went for that species and method: depths, lures, or locations alone are not a rating."""

# v11: rule 10 covers only statements about results; effort statements are not observations.
_RULE10_V10 = ('- A general statement about a method that names no species ("Pier fishing has slowed.", "Boat anglers had a tough week.") '
               'applies to the species named')
_RULE10_V11 = ('- A general statement about how fishing went for a method that names no species ("Pier fishing has slowed.", '
               '"Boat anglers had a tough week.") applies to the species named')
_EFFORT_V11 = ('\n- Statements about how many people fished or whether they could get out ("Very few boat anglers made it out on the big lake.", '
               '"Fishing pressure was low.") say nothing about results. They are not observations.')
_ALL_SPECIES = 'Explicit "all species" statements always get a fish_unspecified observation (rule above).'
assert _RULE10_V10 in _BASE_V10 and _ALL_SPECIES in _BASE_V10
# v11 also: lure scope, named fish outside the list, and what counts as an access issue.
_LURES_V10 = "Techniques like trolling, casting or drifting are not lures."
_LURE_SCOPE_V11 = (" A sentence about lures that names no species (\"Glow plugs worked well in low-light conditions.\", "
                   "\"Most fish were caught on spoons.\") applies to every species in the paragraph fished by that method; "
                   "lures named for one species (\"meat rigs caught the bigger Chinook\") stay with that species.")
_SPECIES_V10 = "Species not in the list are other."
_OTHER_V11 = (" A kind of fish that is named but not in the list (panfish, bluegill, crappie, carp) is other; "
              "fish_unspecified is only for fish whose kind isn't named (\"fish\", \"some fish\").")
_ACCESS_V11 = ('\n- access_issues: set only when fishing is not possible from a location: a pier, launch, beach or harbor that is closed or '
               'blocked ("the north pier is closed", "the launch is closed for dredging"). Openings and facilities ("the fish cleaning '
               'station is now open"), weather keeping boats in, and low fishing pressure are not access issues; leave it null.')
assert _LURES_V10 in _BASE_V10 and _SPECIES_V10 in _BASE_V10
# rule 4: "also / too / as well" makes "fish" refer back even when the method changes
_FISH_V10 = '-> the depths belong to salmon_unspecified).'
_FISH_ALSO_V11 = (' "Fish" with "also", "too" or "as well" refers back to the species just named, even when the method changes '
                  '("Shore anglers caught Chinook salmon and coho salmon. A few fish were also taken by anglers trolling." '
                  '-> chinook/boat and coho/boat).')
assert _FISH_V10 in _BASE_V10
# depth -> boat holds even right after a pier sentence or with a pier-relative location
_DEPTH_BOAT_V10 = "If it mixes casting and trolling without mentioning shore or bank, use boat."
_DEPTH_CONTEXT_V11 = (' A sentence that gives a water depth is not about the pier or shore just because it follows a pier sentence, '
                      'and a location like "south of the piers" does not make it pier or shore fishing.')
assert _DEPTH_BOAT_V10 in _BASE_V10
_BASE_V11 = (_BASE_V10.replace(_RULE10_V10, _RULE10_V11)
             .replace(_ALL_SPECIES, _ALL_SPECIES + _EFFORT_V11 + _ACCESS_V11, 1)
             .replace(_LURES_V10, _LURES_V10 + _LURE_SCOPE_V11, 1)
             .replace(_SPECIES_V10, _SPECIES_V10 + _OTHER_V11, 1)
             .replace(_FISH_V10, _FISH_V10 + _FISH_ALSO_V11, 1)
             .replace(_DEPTH_BOAT_V10, _DEPTH_BOAT_V10 + _DEPTH_CONTEXT_V11, 1))

# Prompt text for every version that has been run is kept here unchanged, so results stay reproducible.
PROMPTS = {
    "v10": _BASE_V10.replace("{QUALITY}", QUALITY_TABLE),
    "v10-def": _BASE_V10.replace("{QUALITY}", QUALITY_DEFINITIONS),
    "v11": _BASE_V11.replace("{QUALITY}", QUALITY_TABLE),
    "v11-def": _BASE_V11.replace("{QUALITY}", QUALITY_DEFINITIONS),
}
PROMPT_VERSION = "v11-def"  # default: catch_quality by level definitions (won the v10 A/B, see EVAL_LOG.md)
SYSTEM_PROMPT = PROMPTS[PROMPT_VERSION]


def user_prompt(port: str, date: str, text: str) -> str:
    return f"Port: {port}\nReport date: {date}\n\n<report_text>\n{text}\n</report_text>"
