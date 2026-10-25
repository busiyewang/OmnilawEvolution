# Run 2 — REJECTED

- Goal: 减少技术日报中的重复材料，同时保留带有新信息的条目 (reduce duplicates, keep items with new information)
- Proposal #2 (planner: `mock`)
- Mode: `shadow`; executor: `template`; model adapter: `mock` (offline mock — no model was called)
- Baseline SHA: `87038d6af0021562967a9cec888c0622f61c7d3e`
- Candidate SHA: `—`
- Data split version: `2026-10-25.1`; evaluator version: `3b131d0def0673d0`
- Patch: `—`

## Hypothesis

**Problem.** Same as above, at lower cost: shingling full text is the most expensive step.

**Hypothesis.** Contrast hypothesis (planner-generated, not taken from a source): titles carry most of an item's identity, so Jaccard over title tokens is enough.

**Methods:** url_canonicalization, title_token_overlap

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

- v0 {'threshold': 0.3} — patched (sha `10ef8a95402c`)
- v1 {'threshold': 0.5} — patched (sha `3a9de1e40027`)

## Dev split (candidate selection)

| variant | n | precision | recall | f1 | false_deletions | false_deletion_rate | wrong_links | critical_lost | ms_per_item |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 26 | 1.0 | 0.0833 | 0.1538 | 0 | 0.0 | 0 | 0 | 0.0005 |
| v0 {'threshold': 0.3} | 26 | 0.7692 | 0.8333 | 0.8 | 3 | 0.2143 | 0 | d10, d13, d22 | 0.0134 |
| v1 {'threshold': 0.5} | 26 | 0.9 | 0.75 | 0.8182 | 1 | 0.0714 | 0 | d13 | 0.0146 |

## Decision

Current state **REJECTED**; evaluation decision **REJECTED**.

- no variant passed dev screening
- v0 {'threshold': 0.3}: f1=0.800 false_deletion_rate=0.214 critical_lost=['d10', 'd13', 'd22']
- v1 {'threshold': 0.5}: f1=0.818 false_deletion_rate=0.071 critical_lost=['d13']

## Cost

- Model calls: 0 (input tokens 0, output tokens 0, estimated by the mock adapter)
- Money: not applicable (no paid API call)
- Candidates: 2; sandbox seconds: 0.63; wall seconds: 0.78

## Event log

- 2026-10-25T10:19:49-0700 `state` {"from": "QUEUED", "to": "RESEARCHING"}
- 2026-10-25T10:19:49-0700 `state` {"from": "RESEARCHING", "to": "PLANNED"}
- 2026-10-25T10:19:49-0700 `state` {"from": "PLANNED", "to": "PATCHING"}
- 2026-10-25T10:19:49-0700 `state` {"from": "PATCHING", "to": "EVALUATING"}
- 2026-10-25T10:19:50-0700 `state` {"from": "EVALUATING", "to": "REJECTED"}
