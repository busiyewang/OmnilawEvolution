"""Sentence-embedding runtime for the collector (protected; not a candidate file).

Candidates may ``from omnilaw_evolution.collector.semantic import encode``.
The model is pinned by revision and file hash and must already be on disk
(``omnilaw-evolution models fetch``); this module never downloads anything,
so it works inside the network-less evaluation sandbox.

Optional dependencies: ``pip install -e '.[semantic]'`` (onnxruntime,
tokenizers, numpy). ``available()`` reports why the runtime cannot be used.
"""
from __future__ import annotations

import hashlib
import json
import os

MODEL = {
    "name": "paraphrase-multilingual-MiniLM-L12-v2",
    "repo": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "revision": "e8f8c211226b894fcb81acc59f3b34ba3efd5f42",
    "license": "Apache-2.0",
    "files": {
        "onnx/model.onnx": "10f7a088420252b26caf819236ca2c9d2987afd0fc06fec7553b542a5655a05a",
        "tokenizer.json": "2c3387be76557bd40970cec13153b3bbf80407865484b209e655e5e4729076b8",
        "config.json": "6300193cb75e01cf80c96decef7187dfb33094d97cc1490b7ead6ff134476e4e",
        "1_Pooling/config.json": "4be450dde3b0273bb9787637cfbd28fe04a7ba6ab9d36ac48e92b11e350ffc23",
    },
    "max_tokens": 128,
}

_state = {}


def model_dir():
    base = os.environ.get("OMNILAW_MODEL_DIR") or os.path.join(os.path.expanduser("~"), ".cache",
                                                               "omnilaw-evolution", "models")
    return os.path.join(base, MODEL["name"])


def missing_files(verify_hash=False):
    out = []
    for rel, sha in MODEL["files"].items():
        p = os.path.join(model_dir(), rel)
        if not os.path.isfile(p):
            out.append(rel)
        elif verify_hash:
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != sha:
                out.append(rel + " (hash mismatch)")
    return out


def available(verify_hash=False):
    """Return (ok, reason)."""
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
    except ImportError as exc:
        return False, "missing dependency: %s (pip install -e '.[semantic]')" % exc.name
    miss = missing_files(verify_hash)
    if miss:
        return False, "model files missing under %s: %s (omnilaw-evolution models fetch)" % (model_dir(), miss)
    return True, "ok"


def _load():
    if "session" in _state:
        return _state
    import onnxruntime as ort
    from tokenizers import Tokenizer

    d = model_dir()
    with open(os.path.join(d, "1_Pooling", "config.json"), encoding="utf-8") as f:
        if not json.load(f).get("pooling_mode_mean_tokens"):
            raise RuntimeError("unexpected pooling config")
    tok = Tokenizer.from_file(os.path.join(d, "tokenizer.json"))
    tok.enable_truncation(MODEL["max_tokens"])
    tok.enable_padding()
    opts = ort.SessionOptions()
    # A fixed thread count keeps results bit-identical across runs (checked by
    # the harness); 4 threads bring a laptop CPU to ~12 ms per item.
    opts.intra_op_num_threads = min(4, os.cpu_count() or 1)
    opts.inter_op_num_threads = 1
    sess = ort.InferenceSession(os.path.join(d, "onnx", "model.onnx"), opts, providers=["CPUExecutionProvider"])
    _state.update(session=sess, tokenizer=tok, inputs={i.name for i in sess.get_inputs()})
    return _state


def encode(texts, batch_size=16):
    """L2-normalised mean-pooled embeddings, shape (len(texts), 384), as a
    numpy array. Cosine similarity of two rows is their dot product."""
    import numpy as np

    texts = list(texts)
    if not texts:
        return np.zeros((0, 384), dtype=np.float32)
    st = _load()
    out = []
    for i in range(0, len(texts), batch_size):
        enc = st["tokenizer"].encode_batch(texts[i:i + batch_size])
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in st["inputs"]:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = st["session"].run(None, feed)[0]
        m = mask[..., None].astype(np.float32)
        vec = (hidden * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)
        vec /= np.clip(np.linalg.norm(vec, axis=1, keepdims=True), 1e-12, None)
        out.append(vec.astype(np.float32))
    return np.concatenate(out, axis=0)


def warm_up():
    """Load the model now (module import time) rather than inside a timed call."""
    _load()
