"""Freeze a small PAWS-X sample without selecting on candidate performance.

Download pinned TSVs with --download, or pass --source-dir containing the four
TSVs. Only source files with the recorded hashes are accepted. Normal dataset
builds use the checked-in snapshots and require no network or extra packages.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
from pathlib import Path
import unicodedata
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = ROOT / "eval" / "sources" / "paws-x"
REVISION = "db2e10f2e7106a5a9e56fab3a791f68976f2c0b2"
BASE_URL = "https://raw.githubusercontent.com/airaria/TextPruner/" + REVISION + "/examples/datasets/pawsx/"
SOURCE_HASHES = {
    "dev-en.tsv": "db21decf9b774486984046a324597a1996a7f65c75ecdcdcf65d2473f4c80db5",
    "dev-zh.tsv": "d4a897e6a9f141a6908a57f528c24ac2d861113166bc465ac5a9050ff4eb93a7",
    "test-en.tsv": "73268ee50daa70273709243c8d111a7d446c1e325afe16747791202cbd441eb2",
    "test-zh.tsv": "7ea4cfb07467c0e76e97f64ede87335c100eaeb46058c2610b0ca04fe2998a29",
}
MODES = ("en-en", "zh-zh", "en-zh", "zh-en")
PER_STRATUM = 15  # 4 language directions x 2 labels x 15 = 120 pairs per split.


def normalise(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def source_rows(directory, split):
    languages = {}
    for lang in ("en", "zh"):
        name = split + "-" + lang + ".tsv"
        data = (directory / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != SOURCE_HASHES[name]:
            raise ValueError("source hash mismatch: " + name)
        languages[lang] = [line.split("\t") for line in data.decode("utf-8").splitlines()]
    if len(languages["en"]) != len(languages["zh"]):
        raise ValueError("unaligned source files")
    rows = []
    for line, (en, zh) in enumerate(zip(languages["en"], languages["zh"]), 1):
        if len(en) != 3 or len(zh) != 3 or en[2] not in ("0", "1") or en[2] != zh[2]:
            continue
        texts = en[:2] + zh[:2]
        if any(normalise(t) in ("", "ns") for t in texts):
            continue
        if normalise(en[0]) == normalise(en[1]) or normalise(zh[0]) == normalise(zh[1]):
            continue
        rows.append({"source_split": split, "source_line": line, "label": int(en[2]),
                     "en": en[:2], "zh": zh[:2]})
    return rows


def select(directory):
    used_texts, used_pairs = set(), set()
    result = {}
    for target, source in (("dev", "dev"), ("holdout", "test"), ("batch2", "test")):
        rows = sorted(source_rows(directory, source), key=lambda r: hashlib.sha256(
            ("omnilaw-pawsx-v1|%s|%d" % (source, r["source_line"])).encode()).hexdigest())
        counts, chosen = collections.Counter(), []
        for row in rows:
            pair = (source, row["source_line"])
            texts = {normalise(t) for lang in ("en", "zh") for t in row[lang]}
            if pair in used_pairs or texts & used_texts:
                continue
            modes = [mode for mode in MODES if counts[(mode, row["label"])] < PER_STRATUM]
            if not modes:
                continue
            mode = min(modes, key=lambda m: (counts[(m, row["label"])], MODES.index(m)))
            chosen.append(dict(row, languages=mode))
            counts[(mode, row["label"])] += 1
            used_pairs.add(pair)
            used_texts.update(texts)
        if len(chosen) != len(MODES) * 2 * PER_STRATUM:
            raise ValueError("insufficient independent pairs for " + target)
        result[target] = chosen
    return result


def to_items(split, pairs):
    items = []
    for index, pair in enumerate(pairs):
        group = "pawsx-%s-%03d" % (split, index)
        languages = pair["languages"].split("-")
        for side, lang in enumerate(languages):
            iid = "%s-%d" % (group, side)
            duplicate = side == 1 and pair["label"] == 1
            case = ("cross-language" if languages[0] != languages[1] else "paraphrase") if duplicate else (
                "hard-negative" if side else "original")
            items.append({"id": iid, "url": "https://paws-x.example/" + iid, "title": "",
                          "text": pair[lang][side], "lang": lang, "published": "",
                          "label": {"cluster": group if pair["label"] else iid, "case": case,
                                    "critical": not duplicate, "group": group,
                                    "source": {"dataset": "PAWS-X", "revision": REVISION,
                                               "file": pair["source_split"] + "-" + lang + ".tsv",
                                               "line": pair["source_line"], "column": side + 1}}})
    return items


def load_snapshot(split):
    meta = json.loads((SNAPSHOTS / "SOURCE.json").read_text(encoding="utf-8"))
    data = (SNAPSHOTS / (split + ".jsonl")).read_bytes()
    if hashlib.sha256(data).hexdigest() != meta["snapshots"][split]["sha256"]:
        raise ValueError("snapshot hash mismatch: " + split)
    return [json.loads(line) for line in data.decode("utf-8").splitlines()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    if args.download:
        args.source_dir.mkdir(parents=True, exist_ok=True)
        for name in SOURCE_HASHES:
            with urllib.request.urlopen(BASE_URL + name, timeout=30) as response:
                (args.source_dir / name).write_bytes(response.read())
    snapshots = select(args.source_dir)
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    meta = {"dataset": "PAWS-X", "upstream": "https://github.com/google-research-datasets/paws/tree/master/pawsx",
            "mirror": BASE_URL, "mirror_revision": REVISION, "source_sha256": SOURCE_HASHES,
            "selection": "SHA256 order; 15 per label/language direction; no shared normalised source sentences",
            "snapshots": {}}
    for split, rows in snapshots.items():
        data = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows).encode()
        (SNAPSHOTS / (split + ".jsonl")).write_bytes(data)
        meta["snapshots"][split] = {"pairs": len(rows), "sha256": hashlib.sha256(data).hexdigest()}
    (SNAPSHOTS / "SOURCE.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
