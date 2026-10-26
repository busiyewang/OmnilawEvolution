# PAWS-X sample provenance

PAWS-X was created by Yinfei Yang, Yuan Zhang, Chris Tar and Jason Baldridge,
Google Research. See [PAWS-X: A Cross-lingual Adversarial Dataset for Paraphrase
Identification](https://aclanthology.org/D19-1382/) (EMNLP 2019) and the
[upstream documentation](https://github.com/google-research-datasets/paws/blob/02b29f3af1143620d1b7f352247e65c0bdd2ec18/pawsx/README.md).
The original dataset terms are reproduced in `LICENSE`; they allow use for any
purpose. These data retain their upstream terms, rather than the project's MIT license.

The upstream Google Storage download returned HTTP 403 when this snapshot was
prepared. The four TSVs were obtained from the
[TextPruner research repository](https://github.com/airaria/TextPruner/tree/db2e10f2e7106a5a9e56fab3a791f68976f2c0b2/examples/datasets/pawsx)
at commit `db2e10f2e7106a5a9e56fab3a791f68976f2c0b2`. This mirror retains 2,000
aligned lines per language/split, with the original ID column removed. We record
**mirror file + one-based line + column**, not a fabricated upstream ID.
All four file hashes and the selected-snapshot hashes are in `SOURCE.json`.
The unavailable original archive was not independently byte-compared to this mirror.

Only evaluation material is used: development pairs for `dev`, and disjoint
subsets of test pairs for `holdout` and `batch2`. Training translations are not
used. The source describes evaluation translations as human translations.
The English and Chinese labels must agree; missing/`NS` sentences, identical
normalised sentence pairs, and reused normalised sentences are excluded.
Rows are ordered by a fixed hash and stratified into 15 pairs for each of
`en-en`, `zh-zh`, `en-zh`, `zh-en` and each binary label. No model scores enter selection.

The cross-language examples combine the aligned English/Chinese sentences and
inherit the agreed source pair label. They are a derived evaluation task, not
an official PAWS-X cross-language score. Each pair is evaluated independently:
the upstream annotation does not establish relations between arbitrary pairs.
Wikipedia-based adversarial sentences are a useful external stress test, but
do not establish performance on a user's real news feed. Translation and
annotation errors may remain; this sample has not received an additional human audit.

Reproduce the snapshots with standard-library Python:

```bash
python3 scripts/import_pawsx.py --source-dir /tmp/pawsx-tsv --download
python3 scripts/build_datasets.py
```

Alternatively supply the four pinned TSVs in `--source-dir` without `--download`.
Normal `build_datasets.py` runs use the checked-in snapshots and are fully offline.
