# eval/ — protected evaluation data

Candidates can never write here (policy guard), never see it in their checkout
(sparse checkout excludes `/eval/`), and cannot read it while running (OS sandbox).

* `datasets/dedup/dev.jsonl` — 26 items. Candidate selection (threshold screening) only.
* `datasets/dedup/holdout.jsonl` — 28 items. Scored once per run, for the dev-selected variant, to decide adoption.
* `datasets/dedup/batch2.jsonl` — 16 items. A later batch used to check transfer of an adopted release
  and to record new failure classes.
* `datasets/dedup/MANIFEST.json` — version and sha256 of each split. Its hash is part of the evaluator version.

All items are fictional. Each has a gold `cluster`, a `case` type (exact-url, tracking-params,
scheme-and-www, fragment-and-slash, reprint, near-duplicate-title, same-topic-different-event,
same-topic-new-info, cross-language, paraphrase, original) and a `critical` flag (unique information
that must never be removed). This is mechanism-demo scale, not a benchmark.

Regenerate with `python3 scripts/build_datasets.py`. Changing the data or the evaluator is a separate,
reviewed change and requires a new baseline.
