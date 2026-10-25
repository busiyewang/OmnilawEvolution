"""Source discovery and reading: web pages, GitHub files (pinned to a commit
SHA) and arXiv papers. Downloads have a timeout, a size cap and a cache;
identical content is de-duplicated by hash.

Fetched text is untrusted data. It is stored and scanned for instruction-like
content (flags are recorded), and it is never executed or used to change
configuration.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from xml.etree import ElementTree

from .db import now

INJECTION_PATTERNS = [
    (r"ignore (all |any )?(previous|prior|above) (instructions|prompts)", "override-instructions"),
    (r"(disregard|forget) (your|the) (rules|instructions|system prompt)", "override-instructions"),
    (r"忽略(之前|以上|所有)?的?(指令|指示|规则)", "override-instructions"),
    (r"(curl|wget)\s+[^\n|]+\|\s*(ba|z)?sh", "pipe-to-shell"),
    (r"\brm\s+-rf\b", "destructive-command"),
    (r"(writable_paths|protected_paths|auto_apply|max_daily_cost|api[_-]?key)", "config-or-secret-reference"),
    (r"(修改|更改)(配置|权限|预算)", "config-or-secret-reference"),
    (r"(run|execute|执行)\s*(the following|以下)\s*(command|命令)", "command-request"),
]


def scan_untrusted(text):
    flags = []
    for pat, name in INJECTION_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m and name not in [f["kind"] for f in flags]:
            flags.append({"kind": name, "match": text[max(0, m.start() - 40):m.end() + 40].replace("\n", " ")})
    return flags


def html_to_text(raw):
    raw = re.sub(r"(?is)<(script|style|math|svg|nav|footer).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|h[1-6]|li|section|tr)>", "\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
    return re.sub(r"\n\s*\n+", "\n\n", raw).strip()


class Fetcher:
    def __init__(self, cfg, budget=None, fresh=False):
        self.cfg = cfg
        self.budget = budget
        self.fresh = fresh  # bypass cached responses (knowledge review)
        self.cache_dir = cfg.subdir("cache")

    def get(self, url, accept=None):
        key = hashlib.sha256(url.encode()).hexdigest()
        cpath = os.path.join(self.cache_dir, key + ".bin")
        if os.path.exists(cpath) and not self.fresh:
            with open(cpath, "rb") as f:
                return f.read()
        fc = self.cfg["fetch"]
        if self.budget:
            self.budget.check("download_bytes", 0)
        req = urllib.request.Request(url, headers={"User-Agent": fc["user_agent"],
                                                   "Accept": accept or "*/*"})
        with urllib.request.urlopen(req, timeout=fc["timeout"]) as resp:
            data = resp.read(fc["max_bytes"] + 1)
        if len(data) > fc["max_bytes"]:
            raise ValueError("response larger than fetch.max_bytes (%d): %s" % (fc["max_bytes"], url))
        if self.budget:
            self.budget.charge("download_bytes", len(data))
        with open(cpath, "wb") as f:
            f.write(data)
        return data

    def get_json(self, url):
        return json.loads(self.get(url, "application/json").decode("utf-8"))


GITHUB_BLOB = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$")
GITHUB_REPO = re.compile(r"^https://github\.com/([^/]+)/([^/#?]+)/?$")
ARXIV = re.compile(r"arxiv\.org/(?:abs|pdf|html)/(\d{4}\.\d{4,5})(v\d+)?")
WIKIPEDIA = re.compile(r"^https://([a-z]+)\.wikipedia\.org/wiki/([^#?]+)")


def classify(url):
    if GITHUB_BLOB.match(url) or GITHUB_REPO.match(url):
        return "github"
    if ARXIV.search(url):
        return "paper"
    return "web"


def fetch_github(f, url):
    m = GITHUB_BLOB.match(url)
    if m:
        owner, repo, ref, path = m.groups()
    else:
        owner, repo = GITHUB_REPO.match(url).groups()
        ref, path = "HEAD", None
    api = "https://api.github.com/repos/%s/%s" % (owner, repo)
    sha = f.get_json("%s/commits/%s" % (api, ref))["sha"]
    try:
        lic = (f.get_json(api + "/license").get("license") or {}).get("spdx_id")
    except Exception:
        lic = "unknown"
    if path is None:
        readme = f.get_json("%s/readme?ref=%s" % (api, sha))
        path = readme["path"]
    raw = f.get("https://raw.githubusercontent.com/%s/%s/%s/%s" % (owner, repo, sha, path)).decode("utf-8", "replace")
    return {"kind": "github", "url": "https://github.com/%s/%s/blob/%s/%s" % (owner, repo, sha, path),
            "title": "%s/%s:%s" % (owner, repo, path), "revision": sha, "license": lic,
            "read_status": "fulltext", "text": raw, "meta": {"repo": "%s/%s" % (owner, repo), "path": path,
                                                         "ref": None if ref == "HEAD" or re.fullmatch(r"[0-9a-f]{40}", ref) else ref}}


def fetch_arxiv(f, url):
    aid, ver = ARXIV.search(url).groups()
    feed = f.get("https://export.arxiv.org/api/query?id_list=" + aid).decode("utf-8")
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entry = ElementTree.fromstring(feed).find("a:entry", ns)
    if entry is None:
        raise ValueError("arXiv id not found: " + aid)
    abs_url = entry.find("a:id", ns).text.strip()
    version = ver or re.search(r"(v\d+)$", abs_url).group(1)
    title = " ".join(entry.find("a:title", ns).text.split())
    authors = [a.find("a:name", ns).text for a in entry.findall("a:author", ns)]
    abstract = " ".join(entry.find("a:summary", ns).text.split())
    text, status = "ABSTRACT\n" + abstract, "abstract"
    try:
        body = html_to_text(f.get("https://arxiv.org/html/%s%s" % (aid, version)).decode("utf-8", "replace"))
        if len(body) > 5 * len(abstract):
            text, status = body, "fulltext"
    except Exception:
        pass
    return {"kind": "paper", "url": "https://arxiv.org/abs/%s%s" % (aid, version), "title": title,
            "revision": "arXiv:%s%s" % (aid, version), "license": "arXiv (see paper page)",
            "read_status": status, "text": text,
            "meta": {"arxiv_id": aid, "version": version, "authors": authors, "abstract": abstract}}


def fetch_web(f, url):
    m = WIKIPEDIA.match(url)
    if m:
        lang, title = m.groups()
        api = ("https://%s.wikipedia.org/w/api.php?action=query&prop=revisions&rvprop=ids|timestamp"
               "&format=json&titles=%s" % (lang, title))
        page = list(f.get_json(api)["query"]["pages"].values())[0]
        rev = page["revisions"][0]["revid"]
        text = f.get("https://%s.wikipedia.org/w/index.php?title=%s&action=raw&oldid=%s"
                     % (lang, title, rev)).decode("utf-8")
        return {"kind": "web", "url": "https://%s.wikipedia.org/w/index.php?title=%s&oldid=%s" % (lang, title, rev),
                "title": page["title"] + " (Wikipedia)", "revision": "oldid=%s" % rev,
                "license": "CC BY-SA 4.0", "read_status": "fulltext", "text": text, "meta": {}}
    raw = f.get(url, "text/html").decode("utf-8", "replace")
    t = re.search(r"(?is)<title>(.*?)</title>", raw)
    return {"kind": "web", "url": url, "title": html.unescape(t.group(1).strip()) if t else url,
            "revision": "fetched " + now(), "license": "unknown", "read_status": "fulltext",
            "text": html_to_text(raw), "meta": {}}


def store_source(db, cfg, rec, origin):
    """Persist a fetched record. Returns (source_id, created)."""
    text = rec["text"]
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    existing = db.one("SELECT id FROM sources WHERE content_hash=?", (digest,))
    if existing:
        return existing["id"], False
    path = os.path.join(cfg.subdir("sources"), digest[:16] + ".txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    flags = scan_untrusted(text)
    sid = db.insert("sources", {
        "kind": rec["kind"], "url": rec["url"], "title": rec.get("title"),
        "fetched_at": rec.get("fetched_at") or now(), "content_hash": digest,
        "revision": rec.get("revision"), "license": rec.get("license"), "read_status": rec["read_status"],
        "path": path, "size": len(text.encode("utf-8")), "origin": origin, "flags": flags,
        "meta": rec.get("meta", {})})
    return sid, True


def ingest_url(db, cfg, url, budget=None):
    f = Fetcher(cfg, budget)
    kind = classify(url)
    rec = {"github": fetch_github, "paper": fetch_arxiv, "web": fetch_web}[kind](f, url)
    return store_source(db, cfg, rec, origin="live")


def ingest_snapshot(db, cfg, manifest_path):
    """Load pre-fetched sources described by a manifest (offline replay)."""
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    base = os.path.dirname(os.path.abspath(manifest_path))
    out = []
    for entry in manifest["sources"]:
        with open(os.path.join(base, entry["file"]), encoding="utf-8") as fh:
            text = fh.read()
        rec = dict(entry, text=text)
        sid, created = store_source(db, cfg, rec, origin="snapshot:" + os.path.basename(manifest_path))
        out.append((sid, created))
    return out


def discover(cfg, topic, budget=None):
    """Find candidate sources for a topic (arXiv, GitHub, configured seeds).
    Kept for the phase-1 ``research`` command; see ``discovery`` for the
    incremental, relevance-filtered version used by the queue."""
    from .discovery import LiveProvider
    return LiveProvider(cfg, budget).search(topic)
