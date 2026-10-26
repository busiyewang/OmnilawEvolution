#!/usr/bin/env python3
"""Rebuild the pinned Hermes source bundle from a local upstream Git checkout.

Only committed runtime sources are included; never user config, credentials,
memories, virtualenvs, build outputs, or the upstream working tree.
"""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

REVISION = "6dca67ed4dc5d11fb08e949737f69ed1a0737a7a"
DIRECTORIES = {"agent", "tools", "hermes_cli", "hermes_platform", "gateway", "cron",
               "pm", "providers", "plugins", "skills", "locales", "assets", "acp_adapter", "tui_gateway"}
ROOT_FILES = {"LICENSE", "pyproject.toml", "SOUL.md"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    args = parser.parse_args()
    target = Path(__file__).resolve().parents[1] / "src/omnilaw_evolution/_vendor"
    target.mkdir(exist_ok=True)
    raw = subprocess.check_output(["git", "-C", str(args.checkout), "archive", REVISION])
    files = {}
    with tarfile.open(fileobj=io.BytesIO(raw)) as source:
        for member in source:
            p = Path(member.name)
            if not member.isfile() or not (p.parts[0] in DIRECTORIES or
                    len(p.parts) == 1 and (p.suffix == ".py" or p.name in ROOT_FILES)):
                continue
            data = source.extractfile(member).read()
            if p.name == "hermes_bootstrap.py":
                # Omnilaw owns installation. Keep UTF-8, path hardening and socket
                # fixes; never let an embedded library update/re-exec the host.
                data = data.split(b"\n_settle_interrupted_update()\n", 1)[0]
                data += (b"\n# Omnilaw embedding: dependencies are installed by pip, not Hermes PM.\n"
                         b"install_happy_eyeballs_socket_connect()\nexport_scratch_tmp_env()\n")
            files[member.name] = data
    with (target / "hermes-core.tar.gz").open("wb") as stream:
        with gzip.GzipFile(filename="", fileobj=stream, mode="wb", mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w") as out:
                for name, data in sorted(files.items()):
                    info = tarfile.TarInfo(name)
                    info.size, info.mode = len(data), 0o644
                    out.addfile(info, io.BytesIO(data))
    (target / "HERMES-LICENSE").write_bytes(files["LICENSE"])
    manifest = {"upstream": "https://github.com/NousResearch/hermes-agent", "commit": REVISION,
                "sha256": hashlib.sha256((target / "hermes-core.tar.gz").read_bytes()).hexdigest(),
                "files": len(files), "uncompressed_bytes": sum(map(len, files.values())),
                "patches": ["hermes_bootstrap.py: omit standalone updater/PM activation; use Omnilaw's Python environment"]}
    (target / "hermes-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
