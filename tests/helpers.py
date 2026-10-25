import os
import shutil
import tempfile

from omnilaw_evolution import sources, versions
from omnilaw_evolution.budget import Budget
from omnilaw_evolution.config import load_config, write_config
from omnilaw_evolution.db import DB
from omnilaw_evolution.demo import build_instance
from omnilaw_evolution.llm import MockLLM
from omnilaw_evolution.planner import propose

GOAL = "reduce duplicates, keep new information"


class Instance:
    """A throwaway demo instance: git repo copy + config + state."""

    def __init__(self, budget=None, extra=None):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="omnilaw-test-"))
        self.root = os.path.join(self.tmp, "instance")
        self.base_sha = build_instance(self.root)
        self.cfg_path = os.path.join(self.tmp, "omnilaw.json")
        data = {"project_root": "instance", "state_dir": "state", "experiments_dir": "experiments"}
        if budget:
            data["budget"] = budget
        data.update(extra or {})
        write_config(self.cfg_path, data)
        self.cfg = load_config(self.cfg_path)
        self.db = DB(self.cfg.db_path)

    def init(self):
        return versions.init_releases(self.db, self.cfg)

    def ingest(self):
        return sources.ingest_snapshot(self.db, self.cfg,
                                       os.path.join(self.root, "fixtures", "sources", "manifest.json"))

    def propose(self, budget=None):
        return propose(self.db, self.cfg, MockLLM(), budget or Budget(self.cfg["budget"]), GOAL)

    def ready(self):
        """init + ingest + propose; returns (good_proposal_id, contrast_proposal_id)."""
        self.init()
        self.ingest()
        ids, _, _ = self.propose()
        return ids[0], ids[1]

    @property
    def discovery_manifest(self):
        return os.path.join(self.root, "fixtures", "discovery", "manifest.json")

    def close(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
