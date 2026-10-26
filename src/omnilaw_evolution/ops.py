"""Operations: credentials file, pinned model download, readiness check, and
the background service (launchd on macOS, a systemd user unit on Linux).

Credentials live in an env file outside the repository (default
``~/.config/omnilaw-evolution/env``, mode 600). Only variable *names* are ever
printed; values never reach logs, reports or the state DB.
"""
from __future__ import annotations

import hashlib
import os
import platform
import plistlib
import shutil
import stat
import subprocess
import sys
import time
import urllib.request

ENV_FILE = os.path.join(os.path.expanduser("~"), ".config", "omnilaw-evolution", "env")
LABEL = "com.omnilaw.evolution"


# ------------------------------------------------------------ credentials
def env_file_path():
    return os.environ.get("OMNILAW_ENV_FILE") or ENV_FILE


def load_env_file(path=None):
    """Set variables from ``KEY=VALUE`` lines that are not already in the
    environment. Refuses a file readable by group or others. Returns the names set."""
    path = path or env_file_path()
    if not os.path.isfile(path):
        return []
    mode = os.stat(path).st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise RuntimeError("%s is readable by other users; run: chmod 600 %s" % (path, path))
    names = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("export "):
                key = key[7:].strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
                names.append(key)
    return names


def env_template(cfg):
    fc, nc = cfg["chat"]["feishu"], cfg["notify"]
    names = [cfg["model"].get("api_key_env") or "ANTHROPIC_API_KEY", fc["app_id_env"], fc["app_secret_env"],
             fc["verification_token_env"], nc["webhook_url_env"], nc["webhook_secret_env"]]
    return ("# omnilaw-evolution credentials (chmod 600). Fill in what you use; leave the rest empty.\n"
            + "".join("%s=\n" % n for n in names))


def init_env_file(cfg, path=None):
    path = path or env_file_path()
    if os.path.exists(path):
        return path, False
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(env_template(cfg))
    return path, True


# ------------------------------------------------------------ model files
def fetch_model(progress=None):
    """Download the pinned semantic model (revision + sha256 from
    ``collector/semantic.py``). Existing files with the right hash are kept."""
    from .collector import semantic

    m = semantic.MODEL
    out = []
    for rel, sha in m["files"].items():
        dest = os.path.join(semantic.model_dir(), rel)
        if os.path.isfile(dest) and _sha256(dest) == sha:
            out.append({"file": rel, "status": "present"})
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        url = "https://huggingface.co/%s/resolve/%s/%s" % (m["repo"], m["revision"], rel)
        tmp = dest + ".part"
        h = hashlib.sha256()
        req = urllib.request.Request(url, headers={"User-Agent": "omnilaw-evolution"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
            for chunk in iter(lambda: resp.read(1 << 20), b""):
                f.write(chunk)
                h.update(chunk)
                if progress:
                    progress(rel, f.tell())
        if h.hexdigest() != sha:
            os.remove(tmp)
            raise RuntimeError("hash mismatch for %s: got %s" % (rel, h.hexdigest()))
        os.replace(tmp, dest)
        out.append({"file": rel, "status": "downloaded", "bytes": os.path.getsize(dest)})
    return out


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------ readiness
def doctor(cfg):
    """What is usable right now, and what still needs a human. Names only."""
    from .collector import semantic
    from .costs import price
    from .knowledge import semantic_status

    def has(module):
        try:
            __import__(module)
            return True
        except ImportError:
            return False

    fc, nc = cfg["chat"]["feishu"], cfg["notify"]
    key_env = cfg["model"].get("api_key_env") or "ANTHROPIC_API_KEY"
    ok_sem, why_sem = semantic_status(cfg)
    checks = {
        "python": sys.version.split()[0],
        "interpreter": sys.executable,
        "env_file": {"path": env_file_path(), "exists": os.path.isfile(env_file_path())},
        "semantic": {"enabled_in_config": bool(cfg.get("semantic", {}).get("enabled")),
                     "runtime": semantic.available(), "usable": ok_sem, "reason": why_sem},
        "model": {"provider": cfg["model"]["provider"], "model": cfg["model"].get("model"),
                  "sdk_installed": True if cfg["model"]["provider"] == "deepseek" else has("anthropic"),
                  "transport": "stdlib-http" if cfg["model"]["provider"] == "deepseek" else cfg["model"]["provider"],
                  "credential_env": key_env,
                  "credential_set": bool(os.environ.get(key_env)),
                  "price_per_mtok": price(cfg), "max_daily_cost_usd": cfg["budget"].get("max_daily_cost_usd")},
        "executor": {"kind": cfg["executor"]["kind"], "claude_cli": shutil.which(cfg["executor"]["claude_cli"])},
        "chat_agent": {"engine": cfg["chat"].get("engine", "basic"),
                       "python_supported": sys.version_info[:2] == (3, 14),
                       "dependencies_installed": all(has(m) for m in ("openai", "rich", "dotenv", "yaml", "browser_harness")),
                       "source": "bundled Hermes 6dca67e", "local_cli_only": True},
        "feishu": {"sdk_installed": has("lark_oapi"),
                   "app_credentials_set": all(os.environ.get(fc[k]) for k in ("app_id_env", "app_secret_env")),
                   "verification_token_set": bool(os.environ.get(fc["verification_token_env"])),
                   "webhook_set": bool(os.environ.get(nc["webhook_url_env"])),
                   "allowed_users": len(cfg["chat"].get("allowed_users") or []),
                   "admins": len(cfg["chat"].get("admins") or []), "notify_kind": nc["kind"]},
        "background": {"enabled": bool(cfg.get("background")), "daily_budget": cfg.get("daily_budget"),
                       "service": service_status()},
    }
    todo = []
    if cfg["chat"].get("engine") == "hermes" and not (
            checks["chat_agent"]["python_supported"] and checks["chat_agent"]["dependencies_installed"]):
        todo.append("chat_agent: Python 3.14 and python -m pip install -e '.[agent]'")
    if not ok_sem:
        todo.append("semantic: " + why_sem)
    if cfg["model"]["provider"] != "mock":
        if not checks["model"]["credential_set"]:
            todo.append("model: set %s in the env file" % key_env)
        if price(cfg) is None:
            todo.append("model: set model.price_per_mtok {input, output}")
        if not cfg["budget"].get("max_daily_cost_usd"):
            todo.append("model: set budget.max_daily_cost_usd (0 refuses every paid call)")
    if not checks["feishu"]["app_credentials_set"]:
        todo.append("feishu: set %s and %s in the env file" % (fc["app_id_env"], fc["app_secret_env"]))
    if not checks["feishu"]["admins"]:
        todo.append("feishu: put your open_id in chat.allowed_users and chat.admins")
    checks["todo"] = todo
    return checks


# ------------------------------------------------------------ service
def _plist_path():
    return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", LABEL + ".plist")


def _unit_path():
    return os.path.join(os.path.expanduser("~"), ".config", "systemd", "user", "omnilaw-evolution.service")


def service_definition(cfg, config_path, label=LABEL, args=("daemon",)):
    log_dir = cfg.subdir("logs")
    program = [sys.executable, "-m", "omnilaw_evolution", "--config", os.path.abspath(config_path)] + list(args)
    return {
        "Label": label,
        "ProgramArguments": program,
        "WorkingDirectory": cfg["project_root"],
        "EnvironmentVariables": {"OMNILAW_ENV_FILE": env_file_path(),
                                 "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        "RunAtLoad": True,
        # Restart only after a crash; a refusal (exit code != 0 at start) is
        # throttled by launchd, a clean SIGTERM exit stays stopped.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 300,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "StandardOutPath": os.path.join(log_dir, label + ".log"),
        "StandardErrorPath": os.path.join(log_dir, label + ".log"),
    }


def install_service(cfg, config_path, services=("daemon",)):
    """Install and start the background service(s). Refuses when the daemon
    itself would refuse (background off or no daily budget)."""
    from .worker import check_daemon_config

    check_daemon_config(cfg)
    if not config_path or not os.path.isfile(config_path):
        raise RuntimeError("install needs an explicit --config file")
    if platform.system() != "Darwin":
        raise RuntimeError("automatic install is implemented for macOS launchd; on Linux copy "
                           "docs/deploy/omnilaw-evolution.service.example to %s" % _unit_path())
    installed = []
    for name in services:
        label = LABEL if name == "daemon" else LABEL + "." + name
        args = ("daemon",) if name == "daemon" else ("serve-feishu", "--mode", "ws")
        path = os.path.join(os.path.dirname(_plist_path()), label + ".plist")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            plistlib.dump(service_definition(cfg, config_path, label, args), f)
        domain = "gui/%d" % os.getuid()
        _bootout(domain, label)
        for attempt in range(3):
            res = subprocess.run(["launchctl", "bootstrap", domain, path], capture_output=True, text=True)
            if res.returncode == 0:
                break
            time.sleep(2)
        else:
            raise RuntimeError("launchctl bootstrap failed: %s" % res.stderr.strip())
        installed.append({"label": label, "plist": path})
    return installed


def _bootout(domain, label, wait=20):
    """Stop a loaded service and wait until launchd has really unloaded it
    (bootout returns before the job is gone; bootstrapping too early fails)."""
    target = "%s/%s" % (domain, label)
    subprocess.run(["launchctl", "bootout", target], capture_output=True)
    deadline = time.time() + wait
    while time.time() < deadline:
        if subprocess.run(["launchctl", "print", target], capture_output=True).returncode != 0:
            return
        time.sleep(0.5)


def uninstall_service():
    removed = []
    folder = os.path.dirname(_plist_path())
    if not os.path.isdir(folder):
        return removed
    for name in sorted(os.listdir(folder)):
        if name.startswith(LABEL) and name.endswith(".plist"):
            label = name[:-len(".plist")]
            _bootout("gui/%d" % os.getuid(), label)
            os.remove(os.path.join(folder, name))
            removed.append(label)
    return removed


def service_status():
    if platform.system() != "Darwin":
        return {"installed": os.path.exists(_unit_path()), "manager": "systemd (manual)"}
    folder = os.path.dirname(_plist_path())
    out = {}
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if name.startswith(LABEL) and name.endswith(".plist"):
            label = name[:-len(".plist")]
            res = subprocess.run(["launchctl", "print", "gui/%d/%s" % (os.getuid(), label)],
                                 capture_output=True, text=True)
            state = "not loaded"
            for line in res.stdout.splitlines():
                line = line.strip()
                if line.startswith("state ="):
                    state = line.split("=", 1)[1].strip()
                elif line.startswith("pid ="):
                    state += " (pid %s)" % line.split("=", 1)[1].strip()
            out[label] = state
    return out or {"installed": False}
