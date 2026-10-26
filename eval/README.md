# eval/ — protected evaluation data

The default benchmark is version **2026-10-25.2**, containing **790 items**.
It combines the original fictional daily-feed fixtures with **360 public PAWS-X
pairs (720 sentences)**. It is an external proxy benchmark, not a measurement of
performance on the user's real news feed.

| Split | Feed fixtures | Public sentences | Total | Use |
|---|---:|---:|---:|---|
| dev | 26 | 240 | 266 | Candidate selection |
| holdout | 28 | 240 | 268 | One selected candidate per run |
| batch2 | 16 | 240 | 256 | Later evaluation; never candidate selection |

Each public split contains 30 same-language paraphrases, 30 cross-language
duplicates, 60 hard negatives, and their 120 first sentences. Both `en→zh` and
`zh→en` are represented. Source sentences and pairs do not repeat between the
three selected subsets. Provenance, selection, licence and limitations are in
[`sources/paws-x/NOTICE.md`](sources/paws-x/NOTICE.md).

## Evaluation protocol

* The fictional feed is evaluated as one batch. Every public pair is its own
  two-item batch: PAWS-X annotates pair equivalence, not arbitrary inter-pair relations.
* `label.cluster` is the gold identity; `label.case` describes the case;
  `label.critical` protects unique information. `label.group` marks independent
  pairs, and `label.source` records source file, revision, line and column.
* All `label` data are stripped before the sandbox. Candidate code receives only
  ordinary unlabeled item batches. Pair grouping is fixed independently of labels.
* Aggregate precision/recall/F1 use the combined decisions. False deletions,
  critical losses, determinism, empty-input handling and per-batch output contracts
  remain gates. F1 alone is insufficient to adopt a candidate.
* Adoption requires either an absolute F1 gain ≥ 0.05, or a reduction of the
  remaining F1 gap `(candidate_f1 - baseline_f1) / (1 - baseline_f1)` ≥ 50% with
  **zero false deletions**. Both paths require a strict improvement and all other
  safety/performance checks. An already perfect baseline cannot improve.

## Rebuilding and versioning

Run `python3 scripts/build_datasets.py` to reproduce the benchmark offline.
`--profile demo` generates the original 70 fictional records for mechanism demos;
`demo`, `demo2` and their integration tests explicitly use that profile in their
temporary instances. `--out /tmp/eval/datasets/dedup` writes to a separate directory.

`MANIFEST.json` records split sizes, case coverage and SHA256 hashes. The evaluator
checks the actual dataset bytes against the manifest before reading or using cached
results. Its version covers the data, manifest, scoring code, sandbox harness and
adoption policy. Proposals freeze that version and their criteria.

After a data/rule change, create a new proposal and run: the active baseline and
candidates must be measured under the new version. Old proposals/READY runs cannot
be executed/applied, and a resumed run cannot mix old checkpoints with new data.
Changing criteria also permits a new proposal instead of suppressing it as a repeat.
Historical reports retain their original results; no existing candidate is
retroactively accepted or automatically deployed.

Candidates cannot write `eval/` (scope guard), see it in their sparse checkout,
or read it during sandboxed evaluation. Public benchmark texts may already be
known to pretrained models; this is not a private, contamination-free holdout.
