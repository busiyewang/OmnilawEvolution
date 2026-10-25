"""Knowledge review: re-check stored sources and mark knowledge whose source
changed.

* Wikipedia: compare the stored ``oldid`` with the page's latest revision.
* GitHub file: compare the pinned file with the same path at the branch head
  (content hash, so a moved branch with an unchanged file is "unchanged").
* arXiv: compare the stored version with the latest version.
* Other web pages: re-fetch (bypassing the cache) and compare content hashes.

A changed source is stored as a new source row with ``supersedes`` pointing to
the old one; old and new knowledge are both kept (conflicts are not
overwritten), and the old rows get ``review_status='source_changed'``.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import urllib.parse

from .db import loads, now
from .sources import ARXIV, Fetcher, fetch_arxiv, fetch_github, fetch_web, store_source

WIKI_OLDID = re.compile(r"^https://([a-z]+)\.wikipedia\.org/w/index\.php\?title=([^&]+)&oldid=(\d+)")


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_source(f, src):
    """Return (changed: bool, new_record_or_None, note)."""
    meta = loads(src["meta"], {})
    url = src["url"]
    if src["kind"] == "web":
        m = WIKI_OLDID.match(url)
        if m:
            lang, title, oldid = m.groups()
            api = ("https://%s.wikipedia.org/w/api.php?action=query&prop=revisions&rvprop=ids|timestamp"
                   "&format=json&titles=%s" % (lang, title))
            page = list(f.get_json(api)["query"]["pages"].values())[0]
            latest = str(page["revisions"][0]["revid"])
            if latest == oldid:
                return False, None, "latest revision is still oldid=%s" % oldid
            rec = fetch_web(f, "https://%s.wikipedia.org/wiki/%s" % (lang, urllib.parse.unquote(title)))
            return True, rec, "page revised: oldid=%s -> %s" % (oldid, latest)
        raw_hash_before = src["content_hash"]
        rec = fetch_web(f, url)
        if _hash(rec["text"]) == raw_hash_before:
            return False, None, "content unchanged"
        return True, rec, "content changed"
    if src["kind"] == "github":
        repo, path, ref = meta.get("repo"), meta.get("path"), meta.get("ref")
        if not (repo and path):
            return False, None, "no repo/path metadata; cannot review"
        api = "https://api.github.com/repos/" + repo
        if not ref:
            ref = f.get_json(api)["default_branch"]
        head = f.get_json("%s/commits?path=%s&sha=%s&per_page=1" % (api, urllib.parse.quote(path), ref))
        if not head:
            return False, None, "path no longer exists on %s" % ref
        head_sha = head[0]["sha"]
        if head_sha == src["revision"]:
            return False, None, "no newer commit touching %s on %s" % (path, ref)
        rec = fetch_github(f, "https://github.com/%s/blob/%s/%s" % (repo, head_sha, path))
        rec["meta"]["ref"] = ref
        if _hash(rec["text"]) == src["content_hash"]:
            return False, None, "branch %s moved to %s but %s is byte-identical" % (ref, head_sha[:12], path)
        return True, rec, "%s changed on %s: %s -> %s" % (path, ref, src["revision"][:12], head_sha[:12])
    if src["kind"] == "paper":
        m = ARXIV.search(url)
        aid, ver = m.group(1), m.group(2) or ""
        rec = fetch_arxiv(f, "https://arxiv.org/abs/" + aid)
        latest = rec["meta"]["version"]
        if latest == ver:
            if src["read_status"] in ("abstract", "excerpt") and rec["read_status"] == "fulltext":
                return True, rec, "same version %s, full text now available" % ver
            return False, None, "latest version is still %s" % ver
        return True, rec, "new paper version: %s -> %s" % (ver, latest)
    return False, None, "kind %s not reviewable" % src["kind"]


def due_sources(db, older_than_days, limit):
    cutoff = (dt.datetime.now() - dt.timedelta(days=older_than_days)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = db.all("SELECT * FROM sources s WHERE kind IN ('web','github','paper') "
                  "AND NOT EXISTS (SELECT 1 FROM sources n WHERE n.supersedes = s.id) "
                  "AND url NOT LIKE '%attacker.example%' "
                  "AND (reviewed_at IS NULL OR reviewed_at < ?) ORDER BY COALESCE(reviewed_at, ''), id", (cutoff,))
    return rows[:limit]


def review_sources(db, cfg, budget=None, limit=5, older_than_days=None, offline=False, fetcher=None):
    older = cfg["review_after_days"] if older_than_days is None else older_than_days
    f = fetcher or Fetcher(cfg, budget, fresh=True)
    report = []
    for src in due_sources(db, older, limit):
        entry = {"source_id": src["id"], "url": src["url"]}
        if offline:
            db.update("sources", src["id"], review_note="review skipped: offline")
            entry.update(status="skipped", note="offline")
            report.append(entry)
            continue
        try:
            changed, rec, note = check_source(f, src)
        except Exception as exc:
            db.update("sources", src["id"], review_note="review error: %s: %s" % (type(exc).__name__, exc))
            entry.update(status="error", note=str(exc))
            report.append(entry)
            continue
        entry["note"] = note
        if changed:
            new_id, created = store_source(db, cfg, rec, origin="review:%d" % src["id"])
            if created:
                db.update("sources", new_id, supersedes=src["id"], reviewed_at=now(), review_note=note)
            db.update("sources", src["id"], reviewed_at=now(), review_note="superseded: " + note)
            db.execute("UPDATE knowledge SET review_status='source_changed', reviewed_at=? WHERE source_id=?",
                       (now(), src["id"]))
            entry.update(status="changed", new_source_id=new_id)
        else:
            db.update("sources", src["id"], reviewed_at=now(), review_note=note)
            db.execute("UPDATE knowledge SET review_status='current', reviewed_at=? WHERE source_id=?",
                       (now(), src["id"]))
            entry.update(status="unchanged")
        report.append(entry)
    return report
