"""Turn stored sources into knowledge records: claim, method, conditions,
limitations, exact source location, verification status.

Verification levels are kept distinct:
  source_claim            - the source says so; nothing run locally
  local_repro_pass        - an experiment here reproduced the effect on dev/holdout
  effective_in_user_task  - an adopted release kept the effect on a later real batch
"""
from __future__ import annotations

import re

from .db import loads, now

VERIFICATION_LEVELS = ("source_claim", "local_repro_pass", "local_repro_fail", "effective_in_user_task")

# Methods for which a code template exists (TemplateExecutor can implement them).
TEMPLATED_METHODS = ("url_canonicalization", "shingle_jaccard", "title_token_overlap")

# Methods the offline extractor recognises. With a real model adapter the model
# fills these fields from the text instead; the catalogue is the mock's "reading".
METHOD_CATALOG = {
    "url_canonicalization": {
        "patterns": [r"fragment", r"trailing\s+\"?/", r"query (parameters|variables)", r"normaliz"],
        "min_hits": 3,
        "claim": "Syntactically different URLs often identify the same resource; normalising scheme/host case, "
                 "default ports, trailing slash, fragment and unused query parameters maps them to one key.",
        "steps": "Lower-case scheme and host; drop default port; remove fragment; remove the trailing slash "
                 "consistently; drop query keys known to be unused (tracking keys such as utm_*, fbclid, spm); "
                 "sort remaining query parameters.",
        "conditions": "Applies to items whose URL is the primary identity, e.g. news and blog links.",
        "limitations": "Some normalisations change semantics on some servers (fragment-routed AJAX pages, "
                       "order-sensitive or meaningful query parameters). Cannot detect reprints on other domains.",
        "applicable": True,
    },
    "shingle_jaccard": {
        "patterns": [r"[Jj]accard", r"(shingl|n-?grams?|\d-grams?|k-grams?)"],
        "min_hits": 2,
        "claim": "Representing each document by its set of n-grams and thresholding the Jaccard index finds "
                 "approximate (near-duplicate) matches, e.g. reprints with small edits.",
        "steps": "Normalise text; build the set of n-grams per document; compare a new document with kept "
                 "ones; mark as duplicate when Jaccard >= threshold.",
        "conditions": "Near-duplicates share most of their wording. Exact Jaccard is affordable for small "
                      "batches (hundreds of items); MinHash/LSH approximates it at corpus scale.",
        "limitations": "Paraphrases and translations share few n-grams and are missed. Word n-grams need "
                       "whitespace tokenisation, which does not work for Chinese; character n-grams are a "
                       "local adaptation, not what the paper evaluated. Threshold must be tuned on labelled data.",
        "applicable": True,
    },
    "minhash_lsh": {
        "patterns": [r"MinHash", r"(LSH|locality.sensitive|permutation)"],
        "min_hits": 2,
        "claim": "MinHash signatures estimate Jaccard similarity; LSH banding finds candidate pairs without "
                 "comparing all pairs.",
        "steps": "Hash each shingle with num_perm permutations, keep minima as the signature; estimated "
                 "Jaccard = fraction of equal signature entries; bucket by bands to find candidates.",
        "conditions": "Worth it when pairwise comparison is too slow (large corpora).",
        "limitations": "Adds estimation error and a dependency; daily batches here are small, so exact "
                       "Jaccard is used instead. Recorded as not applicable at the current scale.",
        "applicable": False,
    },
    "semantic_embedding": {
        "patterns": [r"(sentence embeddings?|bi-encoder|dual encoder)", r"semantic (textual )?similarity"],
        "min_hits": 2,
        "claim": "Dense sentence embeddings place paraphrases close together, so cosine similarity between "
                 "embeddings detects rewritten copies that share few surface n-grams.",
        "steps": "Encode title+text with a pretrained sentence encoder; compare a new item with kept items by "
                 "cosine similarity; treat pairs above a tuned threshold as duplicates.",
        "conditions": "Needs a pretrained encoder model and an ML runtime (e.g. PyTorch/ONNX); threshold tuned "
                      "on labelled pairs from the target feed.",
        "limitations": "Adds a large dependency and model download; same-topic different-event items can also "
                       "embed closely, so false deletions must be measured. Not implementable with the stdlib "
                       "template executor; the dependency change touches protected pyproject.toml.",
        "applicable": True,
    },
    "cross_lingual_embedding": {
        "patterns": [r"(cross-lingual|multilingual|language-agnostic)", r"sentence embeddings?"],
        "min_hits": 2,
        "claim": "Language-agnostic sentence embeddings map translations of the same text to nearby vectors, "
                 "enabling duplicate detection across languages.",
        "steps": "Encode items with a multilingual sentence encoder; compare across languages by cosine "
                 "similarity; use a separate (usually stricter) threshold for cross-language pairs.",
        "conditions": "Needs a multilingual encoder model and ML runtime; language pairs present in the feed "
                      "(here zh/en) must be covered by the model.",
        "limitations": "Same dependency issue as semantic_embedding; quality varies by language pair; "
                       "abstract-level reading only, method details unverified.",
        "applicable": True,
    },
    "paraphrase_classifier": {
        "patterns": [r"[Pp]araphrase identification", r"(cross-encoder|classif|deep learning|neural|datasets?)"],
        "min_hits": 2,
        "claim": "Supervised paraphrase identification models (cross-encoders or bi-encoders) decide whether "
                 "two texts express the same content.",
        "steps": "Train or reuse a pair classifier on paraphrase data; score candidate pairs (e.g. those "
                 "pre-filtered by n-gram similarity); remove when the classifier is confident.",
        "conditions": "Needs labelled pairs and a trained model; pairwise scoring cost grows with batch size.",
        "limitations": "Training data domain may not match news; heavy dependency; not implementable with "
                       "the template executor.",
        "applicable": True,
    },
    "exact_substring": {
        "patterns": [r"suffix array", r"[Ee]xact[Ss]ubstr|exact substring"],
        "min_hits": 2,
        "claim": "Suffix arrays find long repeated substrings across a corpus so they can be removed.",
        "steps": "Build a suffix array over the concatenated corpus; remove repeated spans above a length.",
        "conditions": "Training-data cleaning where partial removal inside documents is acceptable.",
        "limitations": "Edits documents rather than deciding keep/drop for whole items; not suitable for a "
                       "reading digest. Not applicable.",
        "applicable": False,
    },
}


def _line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def locate(text, quote):
    """Return a locator string for a quote inside text, or None if absent."""
    pos = text.find(quote)
    if pos < 0:
        return None
    return "chars %d-%d (line %d)" % (pos, pos + len(quote), _line_of(text, pos))


def mock_extract(text):
    """Deterministic pattern-based extraction. Not model understanding."""
    out = []
    for method, spec in METHOD_CATALOG.items():
        hits = []
        for pat in spec["patterns"]:
            m = re.search(pat, text)
            if m:
                hits.append(m)
        if len(hits) < spec["min_hits"]:
            continue
        first = min(hits, key=lambda m: m.start())
        start = max(0, first.start() - 120)
        quote = text[start:first.end() + 280]
        out.append({"method": method, "claim": spec["claim"], "steps": spec["steps"],
                    "conditions": spec["conditions"], "limitations": spec["limitations"],
                    "quote": quote, "applicable": spec["applicable"]})
    return out


def extract_for_source(db, llm, source):
    """Create knowledge rows for one source (idempotent per source+method)."""
    with open(source["path"], encoding="utf-8") as f:
        text = f.read()
    items = llm.extract_knowledge(source, text)
    created = []
    for it in items:
        loc = locate(text, it["quote"])
        if loc is None:
            # A quote that is not in the source is not evidence; drop it.
            continue
        if db.one("SELECT id FROM knowledge WHERE source_id=? AND method=?", (source["id"], it["method"])):
            continue
        kid = db.insert("knowledge", {
            "source_id": source["id"], "method": it["method"], "claim": it["claim"], "steps": it["steps"],
            "conditions": it["conditions"], "limitations": it["limitations"], "locator": loc,
            "excerpt": it["quote"], "verification": "source_claim", "applicable": int(bool(it["applicable"])),
            "extractor": llm.name, "created_at": now()})
        created.append(kid)
    return created


def mark_verification(db, knowledge_ids, level, experiment_ref):
    assert level in VERIFICATION_LEVELS
    for kid in knowledge_ids:
        row = db.get("knowledge", kid)
        if not row:
            continue
        exps = loads(row["experiments"], [])
        if experiment_ref not in exps:
            exps.append(experiment_ref)
        # Never downgrade: a later failed variant does not erase an earlier pass.
        current = row["verification"]
        keep = VERIFICATION_RANK[current] > VERIFICATION_RANK[level]
        db.update("knowledge", kid, verification=current if keep else level, experiments=exps,
                  reviewed_at=now())


VERIFICATION_RANK = {"source_claim": 0, "local_repro_fail": 1, "local_repro_pass": 2,
                     "effective_in_user_task": 3}
