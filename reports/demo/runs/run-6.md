# Run 6 — REJECTED

- Goal: 减少技术日报中的重复材料，同时保留带有新信息的条目 (reduce duplicates, keep items with new information)
- Proposal #1 (planner: `mock`)
- Mode: `shadow`; executor: `template`; model adapter: `mock` (offline mock — no model was called)
- Baseline SHA: `a11b475f44ad5093a1b2013617757ff477577197`
- Candidate SHA: `fcea79ffc851603a6bd94946443eb6c6d84ecd83`
- Data split version: `2026-10-25.1`; evaluator version: `3b131d0def0673d0`
- Patch: `<demo>/experiments/run-6/candidate.patch`

## Hypothesis

**Problem.** Baseline only removes items whose URL strings are identical; tracking parameters, scheme/host variants and reprints on other sites stay in the digest.

**Hypothesis.** Canonicalising URLs removes link-variant duplicates, and character-shingle Jaccard over title+text catches reprints and lightly edited copies without merging different events, because those share little wording.

**Methods:** url_canonicalization, shingle_jaccard

## Sources and knowledge used

- Knowledge #1 `url_canonicalization` — verification now: **effective_in_user_task**
  - Source #1 (web, fulltext): https://en.wikipedia.org/w/index.php?title=URI_normalization&oldid=1359957963
  - Revision `oldid=1359957963 (2026-06-18T08:56:47Z)`; license CC BY-SA 4.0; location chars 123-531 (line 3)
  - Conditions: Applies to items whose URL is the primary identity, e.g. news and blog links.
  - Limitations: Some normalisations change semantics on some servers (fragment-routed AJAX pages, order-sensitive or meaningful query parameters). Cannot detect reprints on other domains.
- Knowledge #3 `shingle_jaccard` — verification now: **effective_in_user_task**
  - Source #3 (paper, excerpt): https://arxiv.org/abs/2107.06499v2
  - Revision `arXiv:2107.06499v2`; license arXiv.org perpetual non-exclusive license (excerpt stored); location chars 1850-2257 (line 7)
  - Conditions: Near-duplicates share most of their wording. Exact Jaccard is affordable for small batches (hundreds of items); MinHash/LSH approximates it at corpus scale.
  - Limitations: Paraphrases and translations share few n-grams and are missed. Word n-grams need whitespace tokenisation, which does not work for Chinese; character n-grams are a local adaptation, not what the paper evaluated. Threshold must be tuned on labelled data.

## Candidates

- v0 {'threshold': 0.45, 'k': 4} — patched (sha `fcea79ffc851`)
- v1 {'threshold': 0.6, 'k': 4} — patched (sha `54793bface59`)
- v2 {'threshold': 0.75, 'k': 4} — patched (sha `b26853f361e3`)

## Dev split (candidate selection)

| variant | n | precision | recall | f1 | false_deletions | false_deletion_rate | wrong_links | critical_lost | ms_per_item |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 26 | 1.0 | 0.9167 | 0.9565 | 0 | 0.0 | 0 | 0 | 0.0571 |
| v0 {'threshold': 0.45, 'k': 4} | 26 | 1.0 | 0.9167 | 0.9565 | 0 | 0.0 | 0 | 0 | 0.057 |
| v1 {'threshold': 0.6, 'k': 4} | 26 | 1.0 | 0.9167 | 0.9565 | 0 | 0.0 | 0 | 0 | 0.0578 |
| v2 {'threshold': 0.75, 'k': 4} | 26 | 1.0 | 0.8333 | 0.9091 | 0 | 0.0 | 0 | 0 | 0.059 |

## Holdout split (adoption decision, scored once)

| variant | n | precision | recall | f1 | false_deletions | false_deletion_rate | wrong_links | critical_lost | ms_per_item |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 28 | 1.0 | 0.9167 | 0.9565 | 0 | 0.0 | 0 | 0 | 0.0577 |
| v0 (selected) | 28 | 1.0 | 0.9167 | 0.9565 | 0 | 0.0 | 0 | 0 | 0.0559 |

Outcome counts on holdout (outcome/case): missed-duplicate/cross-language=1, ok/exact-url=1, ok/fragment-and-slash=1, ok/near-duplicate-title=2, ok/original=10, ok/reprint=3, ok/same-topic-different-event=5, ok/same-topic-new-info=1, ok/scheme-and-www=1, ok/tracking-params=3

Isolation for candidate execution: `sandbox-exec`.

Protected contract tests on candidate: **pass**

## Decision

Current state **REJECTED**; evaluation decision **REJECTED**.

- f1 delta +0.000 < required +0.050

## Cost

- Model calls: 0 (input tokens 0, output tokens 0, estimated by the mock adapter)
- Money: not applicable (no paid API call)
- Candidates: 3; sandbox seconds: 1.26; wall seconds: 1.78

## Event log

- 2026-10-25T10:20:00-0700 `state` {"from": "QUEUED", "to": "RESEARCHING"}
- 2026-10-25T10:20:00-0700 `state` {"from": "RESEARCHING", "to": "PLANNED"}
- 2026-10-25T10:20:00-0700 `state` {"from": "PLANNED", "to": "PATCHING"}
- 2026-10-25T10:20:00-0700 `state` {"from": "PATCHING", "to": "BUDGET_EXHAUSTED"}
- 2026-10-25T10:20:00-0700 `budget-extended` {"max_candidates": 2}
- 2026-10-25T10:20:00-0700 `state` {"from": "BUDGET_EXHAUSTED", "to": "PLANNED"}
- 2026-10-25T10:20:00-0700 `resumed` {"from_checkpoint": ["patched", "planned", "researched"]}
- 2026-10-25T10:20:00-0700 `state` {"from": "PLANNED", "to": "PATCHING"}
- 2026-10-25T10:20:00-0700 `state` {"from": "PATCHING", "to": "EVALUATING"}
- 2026-10-25T10:20:02-0700 `state` {"from": "EVALUATING", "to": "REJECTED"}
