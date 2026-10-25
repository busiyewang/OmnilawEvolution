"""Incremental source discovery.

Every candidate a search returns is remembered in ``discoveries`` (keyed by a
normalised identity such as the arXiv id without version or the GitHub repo),
so repeated searches only ingest what is new. Candidates are scored for
relevance to the topic before anything is downloaded; low scorers are kept as
``irrelevant`` with their score, not silently dropped.

The relevance score is a transparent keyword heuristic (topic-term coverage
plus configured positive/negative terms). It is not model judgement.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
from xml.etree import ElementTree

from .db import now
from .sources import ARXIV, GITHUB_REPO, Fetcher, ingest_url, store_source

ATOM = {"a": "http://www.w3.org/2005/Atom"}
STOP = {"a", "an", "the", "of", "for", "and", "or", "in", "on", "with", "to", "by", "via"}


def tokens(text):
    return [t for t in re.findall(r"[a-z0-9]+|[一-鿿]{2,}", (text or "").lower())]


def normalise_topic(topic):
    return " ".join(tokens(topic))


def candidate_key(kind, url):
    m = ARXIV.search(url)
    if m:
        return "arxiv:" + m.group(1)
    m = re.match(r"https://github\.com/([^/]+)/([^/#?]+)", url)
    if m:
        return "github:%s/%s" % (m.group(1).lower(), m.group(2).lower())
    return "%s:%s" % (kind, url.rstrip("/"))


def _stem(word):
    # Crude prefix stem so "deduplicating" matches "deduplication"; no linguistics.
    return word[:6] if len(word) > 6 else word


def relevance(topic, title, summary, cfg):
    rc = cfg["research"]
    text = " ".join(tokens((title or "") + " " + (summary or "")))
    words = set(text.split())
    stems = {_stem(w) for w in words}
    terms = [t for t in tokens(topic) if t not in STOP] or tokens(topic)
    coverage = sum(1 for t in terms if t in words or _stem(t) in stems) / float(len(terms) or 1)
    score = coverage
    score += 0.15 * sum(1 for t in rc.get("positive_terms", []) if t.lower() in text)
    score -= 0.5 * sum(1 for t in rc.get("negative_terms", []) if t.lower() in words)
    return round(max(0.0, min(1.0, score)), 3)


class LiveProvider:
    name = "live"

    def __init__(self, cfg, budget=None):
        self.cfg = cfg
        self.fetcher = Fetcher(cfg, budget)
        self.budget = budget

    def search(self, topic):
        rc = self.cfg["research"]
        terms = [t for t in tokens(topic) if t not in STOP] or tokens(topic)
        found = []
        try:
            q = "+AND+".join("all:" + urllib.parse.quote(t) for t in terms)
            feed = self.fetcher.get("https://export.arxiv.org/api/query?search_query=%s&max_results=%d"
                                    % (q, rc["arxiv_max_results"])).decode("utf-8")
            for e in ElementTree.fromstring(feed).findall("a:entry", ATOM):
                found.append({"kind": "paper", "url": e.find("a:id", ATOM).text.strip(),
                              "title": " ".join(e.find("a:title", ATOM).text.split()),
                              "summary": " ".join(e.find("a:summary", ATOM).text.split())})
        except Exception as exc:
            found.append({"kind": "error", "url": "arxiv", "title": "%s: %s" % (type(exc).__name__, exc)})
        try:
            q = urllib.parse.quote(" ".join(terms))
            res = self.fetcher.get_json("https://api.github.com/search/repositories?q=%s&sort=stars&per_page=%d"
                                        % (q, rc["github_max_results"]))
            for r in res.get("items", []):
                found.append({"kind": "github", "url": r["html_url"], "title": r["full_name"],
                              "summary": r.get("description") or "",
                              "note": "stars=%s pushed=%s license=%s" % (
                                  r.get("stargazers_count"), r.get("pushed_at"),
                                  (r.get("license") or {}).get("spdx_id"))})
        except Exception as exc:
            found.append({"kind": "error", "url": "github", "title": "%s: %s" % (type(exc).__name__, exc)})
        for u in rc.get("seed_urls", []):
            found.append({"kind": "web", "url": u, "title": "(configured seed)", "summary": "", "seed": True})
        return found

    def ingest(self, db, cand):
        return ingest_url(db, self.cfg, cand["url"], self.budget)


class SnapshotProvider:
    """Replays a recorded discovery feed (topic -> candidates with stored
    text). Used for offline tests and the reproducible demo."""
    name = "snapshot"

    def __init__(self, cfg, manifest_path):
        self.cfg = cfg
        self.base = os.path.dirname(os.path.abspath(manifest_path))
        with open(manifest_path, encoding="utf-8") as f:
            self.manifest = json.load(f)

    def search(self, topic):
        key = normalise_topic(topic)
        for entry in self.manifest["topics"]:
            if normalise_topic(entry["topic"]) == key:
                return [dict(c) for c in entry["candidates"]]
        return []

    def ingest(self, db, cand):
        if not cand.get("file"):
            raise ValueError("snapshot has no stored text for %s" % cand["url"])
        with open(os.path.join(self.base, cand["file"]), encoding="utf-8") as f:
            text = f.read()
        rec = {k: cand[k] for k in ("kind", "url", "title", "revision", "license", "read_status", "fetched_at")
               if k in cand}
        rec.update(text=text, meta=cand.get("meta", {}))
        return store_source(db, self.cfg, rec, origin="snapshot:discovery")


def make_provider(cfg, budget=None, snapshot=None):
    snapshot = snapshot or cfg["research"].get("snapshot_manifest")
    if snapshot:
        return SnapshotProvider(cfg, snapshot)
    return LiveProvider(cfg, budget)


def discover_incremental(db, cfg, topic, provider, max_ingest=None):
    """Search, remember, score and ingest only new relevant candidates.
    Returns a report dict."""
    rc = cfg["research"]
    max_ingest = rc.get("max_ingest_per_task", 3) if max_ingest is None else max_ingest
    report = {"topic": topic, "provider": provider.name, "found": 0, "new": [], "seen_before": [],
              "irrelevant": [], "ingested": [], "errors": []}
    for cand in provider.search(topic):
        if cand["kind"] == "error":
            report["errors"].append(cand["title"])
            continue
        report["found"] += 1
        key = candidate_key(cand["kind"], cand["url"])
        row = db.one("SELECT * FROM discoveries WHERE key=?", (key,))
        if row:
            db.execute("UPDATE discoveries SET last_seen=?, times_seen=times_seen+1 WHERE id=?", (now(), row["id"]))
            report["seen_before"].append({"key": key, "status": row["status"]})
            if row["status"] != "new":
                continue
            did = row["id"]
        else:
            # Configured seeds were chosen by the user: exempt from the relevance filter
            # (not from anything else - their text is still untrusted data).
            score = 1.0 if cand.get("seed") else relevance(topic, cand.get("title"), cand.get("summary"), cfg)
            did = db.insert("discoveries", {
                "key": key, "kind": cand["kind"], "url": cand["url"], "title": cand.get("title"),
                "summary": (cand.get("summary") or "")[:2000], "topic": topic, "relevance": score,
                "status": "new" if score >= rc.get("min_relevance", 0.5) else "irrelevant",
                "note": cand.get("note"), "first_seen": now(), "last_seen": now()})
            report["new"].append({"key": key, "title": cand.get("title"), "relevance": score})
        row = db.get("discoveries", did)
        if row["status"] == "irrelevant":
            report["irrelevant"].append({"key": key, "title": row["title"], "relevance": row["relevance"]})
            continue
        if len(report["ingested"]) >= max_ingest:
            continue  # stays 'new' for a later task
        try:
            sid, created = provider.ingest(db, cand)
            db.execute("UPDATE discoveries SET status='ingested', source_id=? WHERE id=?", (sid, did))
            report["ingested"].append({"key": key, "source_id": sid, "new_source": created})
        except Exception as exc:
            db.execute("UPDATE discoveries SET status='error', note=? WHERE id=?",
                       ("%s: %s" % (type(exc).__name__, exc), did))
            report["errors"].append("%s: %s" % (key, exc))
    return report
