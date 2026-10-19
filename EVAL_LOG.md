# Extraction eval log

What was run, what was found, and what changed, in order. Generated from `eval_history.py`.
Scores are as measured at the time; `review_page.py` recomputes all versions against the current labels.

## 2026-10-16 · v1: Smoke test

**Ran:** Opus 5 on 5 chunks from one 2022 report ($0.12, 4–11 s per chunk).

**Found:**
- Structured output and the verbatim-evidence check worked first time (5/5 ok).
- Water depth vs. fish depth was right on every chunk, the confusion the prompt was designed around.
- Several judgment calls with no written rule: 'slow for all species', sub-locations like Muskegon Lake, implied conditions, shared ratings.

**Changed:**
- Wrote LABELING_GUIDE.md and picked 30 chunks to hand-label: stratified by port and season, plus 7 hand-picked hard cases; 23 dev / 7 test, split within each port.
- Excluded the smoke-test report from the eval set, since its model output had already been seen.

## 2026-10-16 – 17 · v2 → v8: Labeling, and writing every labeling decision into the prompt

**Ran:** No model runs. 30 chunks hand-labeled: all 7 test and 11 dev by the labeler without seeing model output; 12 dev drafted by Claude and reviewed one by one (marked labeled_by in the files).

**Found:**
- Each labeling disagreement exposed a rule the prompt didn't state, so the prompt was versioned with every decision to keep the eval measuring the model, not differing definitions.

**Changed:**
- v2: species 'fish_unspecified' for fish with no species named.
- v3: 8 common non-salmon species (yellow perch, walleye, drum, …) so the eval checks identification, not just 'not salmon'.
- v4: method describes how anglers fished, not where (boats in a river are 'boat').
- v5: 'all species' statements produce a fish_unspecified row.
- v6: 'the fish' right after a named species is that species.
- v7: lures restricted to 18 types; colors, sizes and rigging moved to a new lure_details field.
- v8: a shared rating applies to listed species, except one with its own quantity word ('a few steelhead' = 1).

## 2026-10-17 · v8: First real eval: Opus 5 vs. Haiku 4.5

**Ran:** Both models on all 30 labeled chunks (Opus $1.12, Haiku $0.17). Scored with eval.py: rows matched by (species, method), then field by field; salmon-only and all-species scopes; dev and test separately.

**Scores:** Dev, salmon only, as measured then: Opus recall 95.2%, precision 88.9%, water depth 87.5%, catch quality 87.5% (within 1: 92.5%). Haiku 90.5%, 88.4%, 84.2%, 78.9% (94.7%). Opus 6.4× the cost per chunk. Test row recall was 68.4% for both models, identical numbers that pointed at label/rule disagreement.

**Found:**
- Error review of the 34 Opus dev mismatches: about 12 were rules decided while labeling but missing from the prompt (A), 5 chunks had labels that contradicted the written rules (B), about 6 were real model mistakes (C).
- Biggest single cause: 'out past 100 ft', 'well beyond 120 ft' – labels used an open range, models wrote 100 or 120.
- Two chunks failed the evidence check because the model joined non-adjacent sentences into one quote.
- Zero water/fish depth swaps for either model.

**Changed:**
- Built the review page (labels vs. each model, observation by observation) to make the error review inspectable.

## 2026-10-17 – 18 · v9 → v10: Fixing what the error review found

**Ran:** No model runs; prompt, schema and label changes only.

**Changed:**
- v9: open vs. closed depth ranges: the word next to each number decides ('beyond 100' → 100–null, 'up to 80' → null–80).
- v10: dropped 'conditions'. Weather and water conditions will come from measured data (GLSEA, weather records), not report text.
- v10: where fish are ('along the beach') is not a separate observation and doesn't change the method.
- v10: a general method statement ('Pier fishing has slowed.') applies to the species named right after it; otherwise it becomes a fish_unspecified row.
- v10: evidence is a list of verbatim sentences, checked one by one, so real quotes from different parts of a paragraph pass.
- Labels corrected to match the written rules (decided by the labeler, with the text in front of them): 39e2127-014 methods → pier and no generic pier row; 3f5314a-008 depth 50–null, pier quality [0, 1], perch method unknown; 39b4ba0-010 'found Chinook' → null; 3f7335a-010 'decent numbers' → 2; 425abbc-004 offshore 'found a mix' → null; 'except freshwater drum' → null in 3eab77c-008 and in test chunk 3b2f6aa-009.
- New rule for the agent (AGENT_NOTES.md): null catch quality means 'present, not rated', ranked as a weak positive.

> **Caveat:** Several label corrections were made after seeing v8 output on dev and happened to agree with it, so dev scores for v8 rose after the fact. The test split is the safeguard: no decision was made by looking at model output on test.

## 2026-10-18 · v10 vs. v10-def: A/B test: catch-quality word table vs. level definitions

**Ran:** Both prompt variants × both models on the 23 dev chunks ($2.01; 92/92 calls ok, zero evidence mismatches). The variants differ only in the catch_quality rule.

**Scores:** Dev, salmon only: water depth 84.6% → 97.4% (Opus) and 81.1% → 97.3% (Haiku) vs. v8. Opus precision 86.7% → 92.9%. Catch quality, Opus: tie (36/39 correct with either variant). Haiku: word table 81.1% exact / 86.5% within 1; definitions 83.8% / 94.6%.

**Found:**
- The word table made Haiku over-rate plain 'were caught' and 'decent'; definitions kept it at its v8 level.
- Exact-match difference is 1 row; the within-1 gap for Haiku (3 rows) is the clearer signal. Definitions also handle wording the table doesn't list.
- A rule conflict introduced in v10: the prompt sends mixed wording to the lower level, the guide accepts both levels; two labels (42c1dbe-007, 3f0b284-007 perch) are affected.

**Changed:**
- Proposed: adopt v10-def as the default. Pending: the two mixed-wording labels, then one run on the 7 test chunks.

## 2026-10-18 · v11: Rule 10 limited to results

**Ran:** No model runs yet.

**Found:**
- Reviewing 39cb2bc-012 on the report page: both models added fish_unspecified/boat for 'Very few boat anglers made it out on the big lake.' in v10-def but not in v8. The v10 rule 10 said 'a general statement about a method', which covers effort as well as results, and both models followed it literally. The label (no boat row) was right.
- Only this dev chunk was affected; other effort sentences ('fishing pressure was low', 'a few boats fishing very deep') produced no rows from this rule.

**Changed:**
- v11: rule 10 applies only to statements about how fishing went; new rule: statements about how many people fished or whether they could get out are not observations (guide rules 10 and 11).
- Prompt text for every run version (v10, v10-def) is now frozen in schema.py; v11 / v11-def are separate copies, so old results stay reproducible.
- v11 (from the v10-def error review): a lure sentence naming no species applies to every species fished by that method; a named fish not in the species list is 'other' (fish_unspecified only when no kind is named); access_issues only when fishing is not possible from a location (pier/launch/beach/harbor closed), not openings, weather or low pressure.
- v11: rule 4 extended: 'fish' with 'also', 'too' or 'as well' refers back to the species just named, even across a method change (42c1dbe-008: both models had read 'A few fish were also taken by anglers trolling' as fish_unspecified; the labeler kept chinook/boat and coho/boat).
- v11: the depth → boat rule holds even when the depth sentence follows a pier sentence or names a pier-relative location ('south of the piers'); from 3f0b284-007 and 3abfd6e-010, where perch drifted to pier/shore/unknown.
- Labels: 42c1dbe-007 split into boat (lower river) and river (Berrien Springs Dam, no method stated, rule 7); mixed-wording labels 42c1dbe-007 and 3f0b284-007 perch → [0, 1].
- Decision: catch quality by level definitions. v11-def is the default prompt; the guide's catch_quality section now uses the same definitions, with examples as illustrations rather than a complete list.

## 2026-10-18 · v11-def: Dev run with all v11 fixes

**Ran:** v11-def, both models, 23 dev chunks (Opus $0.97, Haiku $0.13; 46/46 calls ok).

**Scores:** Dev, salmon only, vs. v10-def: Opus recall 95.2% → 100%, precision 95.2% → 100%, lures 97.5% → 100%, catch quality 95.0% → 100%; water depth 97.6%. Haiku recall 88.1% → 95.2%, precision 86.0% → 93.0%, catch quality 83.8% → 87.5%. All species: Opus 3 mismatches in 2 chunks, Haiku 31 in 14.

**Found:**
- Opus's remaining mismatches: steelhead given j-plugs in 3b2f6aa-007 (follows the new lure-scope rule; see edge case), Chinook depth null in 3eab77c-008 (borderline), and drum rated 2 from 'except freshwater drum' (model mistake).
- Haiku still breaks structural rules: 'river' for boats in a river, perch on shore/pier, lure mappings ignored ('flashers and flies', plain 'bass'), and over-rating presence ('found' → 3). More prompt rules haven't fixed these; Opus follows them. Decision basis for using Opus for extraction.
- Known edge case (open): 3b2f6aa-007, 'Chrome or glow j-plugs were found to be productive' sits between a channel-trolling sentence and an offshore steelhead sentence. The lure-scope rule gives the steelhead plugs; the labeler reads it as channel-only and kept the label. The lure rule may need a refinement for sentence placement.

> **Caveat:** Dev is now close to perfect for Opus because the rules were written from dev chunks and several dev labels were corrected after seeing model output. Dev measures fit to the examples; the held-out test split is the number to report.

## 2026-10-18 · v11-def · test: First held-out test run

**Ran:** v11-def, both models, the 7 test chunks, run once ($0.34). Recorded before anyone looked at the test mismatches.

**Scores:** Test, salmon only (9 label rows): Opus recall 77.8% (7/9), precision 63.6% (7/11), water depth 85.7%, catch quality 71.4%. Haiku recall 77.8%, precision 63.6%, water depth 100%, catch quality 85.7%. All species (19 rows): both models 68.4% recall (13/19), the same 13 rows as in v8.

**Found:**
- Far below dev (Opus 100%). Both models matching the same 13 rows suggests label/rule drift rather than model mistakes: test labels were made on day one and, unlike dev, were not updated as rules changed (only catch-quality fixes).

## 2026-10-18 · v11-def · test aligned: Test labels brought up to the current rules

**Ran:** No new model runs; the same v11-def test outputs re-scored after the label review.

**Scores:** Test after alignment, salmon only (10 rows): Opus recall 90.0% (9/10), precision 81.8% (9/11), water depth 100%, catch quality 88.9%. Haiku 90.0%, 81.8%, 88.9%, 100%. All species (21 rows): Opus 90.5% recall / 86.4% precision, Haiku 90.5% / 82.6%. Original held-out numbers (78% / 64% salmon) stay on record above.

**Found:**
- Reviewing the test mismatches once: most were drift, test labels written on day one under rules that later changed (depth → boat, method from context, lures = what anglers used, one row per method), not model mistakes.
- Real model mistakes on test: drum rated 2 from 'except freshwater drum' (both models, also on dev); Opus split 'Flasher-fly combinations caught a few salmon' into a separate salmon row; Haiku made 'shallower than 80 feet' a fish depth on a separate Chinook row and turned 'No perch boats made it out' into a perch row.
- Open gap, left alone on purpose: 'casting' alone with no boat or pier named. The labeler kept method unknown in 3f1ef58-003; both models said boat from context. A rule written from a test chunk would tune the prompt to the test set.

**Changed:**
- Test labels aligned by applying the written rules, decided by the labeler with the text in front of them: 3b2f6aa-009 salmon and perch → boat (depth rule), perch quality [0, 1]; 39e2127-012 drum lures spoons, steelhead → pier, Chinook quality 3 ('high numbers') and water null–120; 3fa205d-001 added steelhead/shore ('surf'), whitefish → unknown ('the channel'); 3f1ef58-003 kings/steelhead kept unknown; 3f5314a-010 kept 3.
- New label feature: optional rows ('optional': true) for text that supports both 'row' and 'no row'. Used once: 3f1ef58-003 'Pier fishing was also hampered by the lake conditions.'

> **Caveat:** The test set has now been seen and its labels revised, so these aligned numbers are no longer a clean held-out measurement. A clean number needs fresh chunks labeled under the current rules before any model runs on them.


## 2026-10-18 · v11-def · Haiku 5.5: Cheap-model check

**Ran:** v11-def, claude-haiku-5-5, all 30 labeled chunks, run once ($0.035, $0.0012/chunk vs Opus 5 $0.043). Same prompt, no changes for the new model.

**Scores:** Dev (23 chunks), salmon only (42 rows): Haiku 5.5 recall 95.2%, precision 97.6%, water depth 92.5%, lures exact 92.5%, catch quality 90.0%. Opus 5 100% on all (dev is fit to Opus, see caveat above); Haiku 4.5 95.2% / 93.0% / 95.0% / 92.5% / 87.5%. All species: Haiku 5.5 90.1% recall / 91.4% precision (Opus 100/100, Haiku 4.5 88.7/87.5). Test (7 chunks, already seen), salmon: Haiku 5.5 recall 100% (10/10), precision 83.3%, catch quality 100%; Opus 90.0% / 81.8% / 88.9%.

**Found:**
- Most dev misses are method from context: 'Lots of drum and catfish' and 'Perch fishing ... picked up' → unknown instead of pier/boat; 'salmon fishing was slow' → unknown instead of boat; walleye 'in the lower river' trolling → river instead of boat. Same family Haiku 4.5 missed.
- Real mistakes: perch bait (spikes, wigglers, minnows) carried onto the Chinook/coho/brown rows in 3df50cf-011; the perch '30 feet of water' sentence used as the boat fish_unspecified depth in 3f0b284-007; 'slow slightly from last week' rated 1 where the label says 2-3.
- Outputs ~1,380 tokens/chunk vs Opus ~750 (adaptive thinking is on by default); still 37x cheaper per chunk.

> **Caveat:** 7 test chunks can't separate the models; dev favors Opus because the rules were written from Opus's dev errors. Use Haiku 5.5 for cheap iteration; decide the final extraction model on fresh labeled chunks.
