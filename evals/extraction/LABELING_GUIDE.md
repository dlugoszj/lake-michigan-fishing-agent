# Labeling Guide (matches prompt v11-def)

Ground truth for the extraction eval. The goal is labels a second person would reproduce from this guide alone. When a case isn't covered, decide, label it, and **add the rule here** so later labels stay consistent.

> The extraction prompt (`extraction/prompts/<PROMPT_VERSION>.txt`) must follow the same rules. If you change a rule here, write the changed prompt as a new version file and bump `PROMPT_VERSION` in `extraction/schema.py`; otherwise the eval measures disagreement about definitions instead of model quality.

## Workflow

1. Open a file in `labels/`. Read `text` only (the paragraph, one sentence per line). **Do not look at any model output** for that chunk (no `fishing.db`, no extract logs). Labels that start from model output inflate that model's score.
2. Fill in the fields (rules below). Each file starts with 4 blank observations (always 4, regardless of the paragraph). Fill in as many as you need; unused blank rows are ignored, so you don't have to delete them. Need more than 4? Copy `_observation_template` into the list.
3. Set `"status": "done"`. Use `"skip"` only if the chunk is unlabelable (e.g., a parser error merged two ports), and note why.
4. Run `python -m evals.extraction.labels_tool check` and fix anything it flags.
5. Work through **dev** files first. `test` files (7) are held out: label them, then don't look at the model output for them while tuning the prompt. They give the final number.

Budget: ~2–3 min per chunk, about 1–1.5 h for 30.

## What counts as an observation

One observation = one **(species, method)** pair that the text says something about. If the text gives catch info, depths, or lures for a species and method, it's an observation.

- `"Chinook and coho 30 to 90 feet down"` → two observations (chinook, coho) with the same depths and lures.
- The same (species, method) twice in one chunk → **merge** into one observation (the checker flags duplicates). Use the widest depth range stated and the union of lures; if catch quality differs, use the overall impression and list both values as acceptable (see catch_quality).
- A species mentioned only as the target, with no outcome or details ("anglers targeting salmon"), is **not** an observation by itself.

### Optional rows
When the text honestly supports both "this observation exists" and "it doesn't" (e.g., "Pier fishing was also hampered by the lake conditions": a poor result, or just conditions/effort?), add the row with `"optional": true` and explain in `notes`. The eval then accepts either: if the model leaves it out, it isn't counted as missed; if the model includes it, it's matched and its fields are scored. Use it as sparingly as quality ranges.

## Fields

### species
Salmon and trout: `chinook | coho | salmon_unspecified | steelhead | lake_trout | pink | brown_trout`

Other species: `yellow_perch | walleye | smallmouth_bass | largemouth_bass | freshwater_drum | lake_whitefish | northern_pike | catfish`

Catch-alls: `fish_unspecified | other`

- Use `chinook`/`coho` only when the text names them. `"kings"` = chinook. `"lakers"` = lake_trout. `"browns"` = brown_trout.
- `"salmon"` alone → `salmon_unspecified`. Never infer the species from the season ("it's September so it's kings").
- `"fish"` that refers back to a species just named counts as that species: "Salmon fishing was slow. The fish were spread from 65 to 120 feet" → the depths go on the `salmon_unspecified` row.
- `"fish"` with no species named nearby → `fish_unspecified`. "Boat anglers had a tough week. Some fish were caught … in 95 feet of water" is a `fish_unspecified` / `boat` observation, even if salmon seem likely.
- `"perch"` = yellow_perch. `"drum"` / `"sheepshead"` = freshwater_drum. `"whitefish"` = lake_whitefish. `"pike"` = northern_pike.
- Bass: `smallmouth_bass` / `largemouth_bass` only when the text says which. Plain `"bass"` → `other`, with `"bass"` in notes.
- A kind of fish that is **named** but not in the lists (e.g., panfish, bluegill, crappie, carp) → `other`, with the fish named in `notes`. Several of those for the same method share one `other` row. `fish_unspecified` is only for fish whose kind isn't named ("fish", "some fish").
- `"mixed bag of Chinook, coho and steelhead"` → one observation per named species.

### method
`boat | pier | shore | river | unknown`

Method describes **how** anglers fished, not where. The port name already says whether it's a river.

- `boat`: trolling, "boat anglers", "boats", drifting from a boat, including boats near the pier heads **and boats in a river**.
- `pier`: anglers fishing *from* the pier or breakwall.
- `shore`: beach, surf, bank, or shore anglers, **including on a river bank**.
- `river`: fishing in a river when the text **doesn't say** whether from a boat or the bank ("anglers saw salmon from the lower river up to the dam").
- If the text gives water depths or trolling details without naming the method ("Chinook action was good in 70 to 90 feet of water"), use `boat`.
- "Casting or trolling the river" (mixed, no mention of shore or bank) → `boat`.
- A depth sentence right after a pier sentence is still `boat`, and a location ("south of the piers") doesn't make it pier or shore fishing (prompt v11).
- `unknown`: no way to tell.

### bottom_depth_ft and fish_depth_ft
Each is `null` or `{"min_ft": int|null, "max_ft": int|null}`.

| Text | bottom_depth_ft | fish_depth_ft |
|---|---|---|
| "in 120 feet of water" | `{120, 120}` | null |
| "in 70 to 90 feet" (of water implied) | `{70, 90}` | null |
| "around 110 feet of water" | `{110, 110}` | null |
| "35 feet down" | null | `{35, 35}` |
| "30 to 90 down in 90 to 140 feet of water" | `{90, 140}` | `{30, 90}` |
| "from 50 feet down to the bottom" | null | `{50, null}` |
| "top 30 feet" | null | `{0, 30}` |
| "beyond / past / over / more than 100 feet" | `{100, null}` | null |
| "120 feet and deeper", "120 feet or more" | `{120, null}` | null |
| "from 45 feet out to beyond 100 feet", "45ft out past 100ft" | `{45, null}` | null |
| "up to / less than / inside / shallower than 80 feet" | `{null, 80}` | null |
| "deeper water" / "shallow" | null | null |

**Open vs. closed ranges** (prompt v9): the word right before or after each number decides whether that end of the range is closed or open. A number with an open-ended word ("beyond", "past", "over", "and deeper", "up to") marks where the range *starts* being open; never use it as the closed end. With the merge rule, one open mention makes the merged range open too ("50 to around 100 feet, with some boats fishing well beyond that" → `{50, null}`).

- A depth given for one sentence about one species applies to the other species in that same sentence, not to later sentences unless the text clearly links them.
- Depths for perch or walleye stay with those observations; don't copy them to salmon rows.

### lures and lure_details
`lures` is a list of lure/bait **types** from this fixed list (prompt v7):

`spoons | flies | flasher/fly | flashers | rotators | meat rigs | plugs | crankbaits | spawn | shrimp | minnows | nightcrawlers | spikes | wigglers | alewife | live bait | ned rigs | other`

- List what anglers **used**, whether or not it worked ("tried casting spoons but were unsuccessful" → `["spoons"]`).
- `"crawlers"` = nightcrawlers, `"j-plugs"` = plugs, flasher-fly combos / "flashers and flies" = `flasher/fly`.
- Colors, sizes, and rigging go in **`lure_details`**, as written: `"green and orange spoons; glow plugs"`, `"shrimp fished under bobbers"`. Empty string if none.
- Techniques (trolling, casting, drifting) are not lures; put them in `notes`.
- A lure sentence that names no species ("Glow plugs worked well in low-light conditions.", "Most fish were caught on spoons.") applies to **every species in the paragraph fished by that method** (prompt v11). Lures named for one species ("meat rigs caught the bigger Chinook") stay with that species.

### catch_quality
How well fishing went for that species and method, judged from the report's wording and context (prompt v11-def). `0–4`, `null`, or a **list of acceptable values** when the wording is honestly between two levels.

| Value | Meaning | Example wording (illustrations, not a complete list) |
|---|---|---|
| 0 | little or nothing caught | slow, tough, unsuccessful, "no reports", "a hard time getting them to bite" |
| 1 | some fish, but below average | a few, some, a couple, spotty, inconsistent |
| 2 | average | fair, decent, moderate |
| 3 | above average | good, successful, lots of fish, good numbers |
| 4 | exceptional | limits, excellent, very good |
| null | the text says nothing about how well fishing went for that species/method | |

Judge wording that isn't listed by what it says about the catch.

- Presence is not a rating → `null`: "Finding salmon 55 to 100 feet down", "Boat anglers **found** Chinook…", "found **a mix of** steelhead and coho", "slow for all species **except** freshwater drum" (says drum wasn't slow, not how good). The observation row already records that the fish were there; the agent treats null as a weak positive (see `AGENT_NOTES.md`).
- Mixed wording ("a little slow … a couple of salmon caught", "slow, however a few were caught") → both levels, e.g. `[0, 1]`. (The prompt tells the model to pick the lower level unless the good part dominates; listing both levels accepts that.)
- Use a list sparingly, e.g., `"decent catches, though limits were scarce"` → `[2, 3]` only if you truly can't pick. The eval accepts any listed value.

### conditions — dropped (prompt v10)
Weather and water conditions will come from measured data (GLSEA water temperature, weather records), not from report text. Existing `conditions` values in label files are ignored.

### access_issues (chunk level)
`true` only when **fishing is not possible from a location**: a pier, launch, beach or harbor that is closed or blocked ("the north pier is closed", "the launch is closed for dredging"). Openings and facilities ("the fish cleaning station is now open"), weather keeping boats in, and low fishing pressure are **not** access issues → `false` (prompt v11). Scored as true/false; the model's free-text description isn't compared.

### no_fishing_info (chunk level)
`true` only if there are zero observations. Must be `false` if you added any observation.

### notes
Free text: anything ambiguous, so we can revisit it. Not scored.

## Settled judgment calls (from the smoke test)

1. **Statements about all species → `fish_unspecified`** (prompt v5). "Pier fishing was slow for all species" → `fish_unspecified` / `pier` / `catch_quality 0`. "All species" doesn't say salmon were there. Species named separately ("however, a few steelhead were caught") get their own rows.
2. *(Retired in v10: conditions are no longer extracted.)*
3. **Shared ratings** (prompt v8). If one sentence rates the fishing and the next lists species ("decent fishing … a mixed bag of Chinook, coho and steelhead"), the rating applies to all listed species, **except** a species with its own quantity word, which is rated by that word from the catch_quality table: "good numbers of Chinook along with a few steelhead" → chinook 3, steelhead 1; "good, with some coho mixed in" → coho 1.

4. **"Fish" → the species it refers back to, else `fish_unspecified`** (prompt v6). "The fish" right after a named species is that species. **"Also", "too" or "as well" makes "fish" refer back even when the method changes** (prompt v11): "Shore anglers caught Chinook salmon and coho salmon. A few fish were also taken by anglers trolling." → `chinook / boat`, `coho / boat`. Only "fish" with no species named nearby is `fish_unspecified`. Never guess salmon from general context.
5. **Sub-locations go in `notes`.** "At Muskegon Lake boat anglers were catching a few salmon" inside the Muskegon chunk: label as normal (the merge rule applies) and write the sub-location in that observation's `notes`, e.g. `"includes Muskegon Lake (inland)"`. The schema has no location field for now; revisit if the agent needs it.
6. **All fish are labeled, with their own species value** (prompt v3). Common non-salmon species have their own values so the eval checks that the model identifies them, not just that they aren't salmon. `other` is only for species not in the list.
7. **Method is how, not where** (prompt v4). Boats in a river are `boat`, bank anglers are `shore`; `river` only when the text doesn't say.
8. **"No reports" → quality 0.** "No perch reports" / "no salmon reported from the piers" → an observation for that species with `catch_quality 0` and `notes: "no reports"`. Knowing a species wasn't reported is useful information.
9. **Where fish are ≠ how they were caught** (prompt v10). "There are also some coho salmon still yet along the beach" says where fish are, not that anyone shore-fished. It is not a separate observation and doesn't change the method; put it in the `notes` of the observation the paragraph is describing (e.g. `coho / boat`: "some coho still along the beach").
10. **General statements about results take their species from what follows** (prompt v10, limited to results in v11). This covers statements about how fishing went for a method ("Pier fishing has slowed.", "Boat anglers had a tough week."). "Pier fishing has slowed. Coho salmon and a few steelhead were still being caught." → the statement is about those species: `coho / pier`, `steelhead / pier`, no `fish_unspecified` row. If the following sentences name no species, or switch to a different method or place ("Pier fishing was also slow. Anglers fishing the Black River…"), add `fish_unspecified` for that method. Explicit "all species" wording always gets the `fish_unspecified` row (rule 1).
11. **Effort is not an observation** (prompt v11). Statements about how many people fished or whether they could get out ("Very few boat anglers made it out on the big lake.", "Fishing pressure was low.") say nothing about results, so they get no row.

## Worked examples (from chunks not in the eval set)

**Grand Haven, 2022-09-14**
> The salmon action was decent 55 to 110 feet down in 120 to 210 feet of water. Spoons in green, yellow and orange produced good numbers of salmon. Green and white flies worked well deeper in the water column. The pier action was slow for salmon.

```json
"observations": [
  {"species": "salmon_unspecified", "method": "boat",
   "bottom_depth_ft": {"min_ft": 120, "max_ft": 210}, "fish_depth_ft": {"min_ft": 55, "max_ft": 110},
   "lures": ["spoons in green, yellow and orange", "green and white flies"],
   "catch_quality": [2, 3], "notes": "'decent' action but spoons 'produced good numbers'"},
  {"species": "salmon_unspecified", "method": "pier",
   "bottom_depth_ft": null, "fish_depth_ft": null, "lures": [], "catch_quality": 0, "notes": ""}
],
"access_issues": false, "no_fishing_info": false
```

**Holland, 2022-09-14**
> Chinook salmon action was good in 70 to 90 feet of water between Holland and Saugatuck. Fish were found from 50 feet down to the bottom and were hitting green flies and also hitting on spoons.

```json
"observations": [
  {"species": "chinook", "method": "boat",
   "bottom_depth_ft": {"min_ft": 70, "max_ft": 90}, "fish_depth_ft": {"min_ft": 50, "max_ft": null},
   "lures": ["green flies", "spoons"], "catch_quality": 3, "notes": "method inferred: depths imply trolling"}
]
```
