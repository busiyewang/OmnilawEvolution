# Hermes runtime source

Upstream: https://github.com/NousResearch/hermes-agent

Pinned revision: `6dca67ed4dc5d11fb08e949737f69ed1a0737a7a`

License: MIT, Copyright 2025 Nous Research. See `HERMES-LICENSE` (also included
inside the archive as `LICENSE`). Per-file upstream license notices are retained.

`hermes-core.tar.gz` contains committed Python runtime source, native tool
implementations, provider/plugin support code, locales and bundled skills from
that revision. It excludes tests, desktop/web frontends, dependency environments,
Git state, user configuration, credentials and private memories. This is actual
upstream source used by Omnilaw's worker, not a separately reimplemented agent loop.

The only upstream source adaptation is in `hermes_bootstrap.py`: standalone
Hermes updater and package-manager activation are omitted. Omnilaw installs its
own Python dependencies with `pip install -e '.[agent]'`. UTF-8 handling,
import-path hardening and connection fixes remain. `AIAgent`, the conversation
loop, tool dispatcher and tools are unchanged.

`hermes-manifest.json` records the bundle SHA-256, source revision, file count and
adaptation. The bundle is verified and extracted into an ignored runtime cache
before use. Omnilaw's integration is in `hermes_runtime.py`, `hermes_worker.py`
and `agent_bridge.py`, outside the vendored source.

To reproduce from an upstream checkout (working-tree changes are never copied):

```bash
python scripts/vendor_hermes.py /path/to/hermes-agent
```

The upstream project is not required after installation. Do not vendor a live
Hermes home or `.env`; the generated bundle must remain free of user state.
