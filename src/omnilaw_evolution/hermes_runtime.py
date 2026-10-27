"""Manage a private, persistent Hermes worker for a local Omnilaw session."""
from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import queue
import signal
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time

from . import skills
from .agent_bridge import AgentBridge

VENDOR = Path(__file__).with_name("_vendor")
TOOLSETS = ["terminal", "file", "web", "browser", "todo", "memory", "skills", "session_search",
            "clarify", "code_execution", "delegation", "omnilaw"]


def prepare_runtime(cfg):
    if sys.version_info[:2] != (3, 14):
        raise RuntimeError("Hermes 引擎需要 Python 3.14；基础命令仍可使用。")
    missing = [name for name in ("openai", "rich", "dotenv", "yaml", "httpx", "browser_harness")
               if importlib.util.find_spec(name) is None]
    if missing:
        raise RuntimeError("请先安装智能体依赖：python -m pip install -e '.[agent]'；缺少 " + ", ".join(missing))
    manifest = json.loads((VENDOR / "hermes-manifest.json").read_text())
    bundle = VENDOR / "hermes-core.tar.gz"
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != manifest["sha256"]:
        raise RuntimeError("Hermes 源码包校验失败，请重新安装 Omnilaw。")
    base = Path(cfg.subdir("runtime"))
    dest = base / manifest["sha256"][:16]
    with (base / "extract.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not dest.exists():
            with tempfile.TemporaryDirectory(dir=base) as tmp:
                extracted = Path(tmp) / "core"
                extracted.mkdir()
                with tarfile.open(bundle) as archive:
                    # The generated bundle contains only regular source files.
                    for member in archive:
                        target = (extracted / member.name).resolve()
                        if not member.isfile() or not target.is_relative_to(extracted.resolve()):
                            raise RuntimeError("Invalid member in Hermes source bundle")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(archive.extractfile(member).read())
                os.replace(extracted, dest)
    return dest


def worker_environment(home, cwd, bridge):
    # Do not inherit provider keys or the user's Hermes profile/plugin config.
    keep = {"HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ",
            "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update(HERMES_HOME=str(home), TERMINAL_CWD=str(cwd), TERMINAL_ENV="local",
               HERMES_INTERACTIVE="1", HERMES_SINGLE_QUERY_SESSION="0",
               OPENAI_BASE_URL=bridge.url + "/v1", OPENAI_API_KEY=bridge.token,
               NO_PROXY="localhost,127.0.0.1,::1", PYTHONUNBUFFERED="1", PYTHONUTF8="1")
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", os.defpath)
    return env


def hermes_settings(cfg, bridge_url, cwd, max_turns, write_approval):
    """Upstream config.yaml for one Hermes home. ``write_approval`` stages every
    skill write under ``<HERMES_HOME>/pending/skills`` instead of saving it."""
    return {"model": {"provider": "custom", "default": cfg["model"]["model"], "base_url": bridge_url + "/v1",
                      "context_length": int(cfg["model"].get("context_tokens", 1048576))},
            "terminal": {"backend": "local", "cwd": cwd, "timeout": 60, "persistent_shell": True},
            "compression": {"enabled": True, "threshold": 0.65},
            "web": {"search_backend": "ddgs", "extract_backend": "omnilaw-local"},
            "browser": {"executable_path": browser_executable(cfg), "headed": False,
                        "use_real_profile": False, "user_data_dir": ""},
            "tools": {"tool_search": {"enabled": "off"}},
            "auxiliary": {"title_generation": {"model_upgrade_enabled": False}},
            "memory": {"memory_enabled": True, "user_profile_enabled": True},
            "skills": {"write_approval": bool(write_approval)},
            "agent": {"max_turns": int(max_turns)}}


def browser_executable(cfg):
    configured = cfg["chat"].get("browser_executable")
    if configured:
        return os.path.expanduser(configured)
    candidates = [shutil.which(name) for name in ("chromium", "chromium-browser", "google-chrome")]
    if sys.platform == "darwin":
        candidates += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                       "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    return next((p for p in candidates if p and os.path.isfile(p)), "")


class HermesRuntime:
    def __init__(self, cfg, user, channel, session, command_schema, on_event=None, on_request=None):
        self.cfg, self.on_event, self.on_request = cfg, on_event, on_request
        self.process = self.bridge = self.log = self.reader = None
        self.events = queue.Queue()
        self.tools = []
        self.last_messages = []
        source = prepare_runtime(cfg)
        identity = skills.identity(user, channel)
        session_id = hashlib.sha256(json.dumps([user, channel, session]).encode()).hexdigest()[:32]
        shared = Path(cfg.subdir("agent", identity))
        home = shared / "sessions" / session_id
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ("memories", "skills"):
            folder = shared / name
            folder.mkdir(exist_ok=True, mode=0o700)
            if not (home / name).exists():
                (home / name).symlink_to(folder, target_is_directory=True)
        # Seed upstream skills once, preserving subsequent user/agent edits.
        with (shared / "seed.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            marker = shared / ".bundled-skills-seeded"
            if not marker.exists():
                for path in (source / "skills").rglob("*"):
                    if path.is_file() and path.name != "AGENTS.md":
                        target = shared / "skills" / path.relative_to(source / "skills")
                        if not target.exists():
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(path, target)
                marker.write_text(source.name + "\n")
        if skills.enabled(cfg):
            # Staged writes are reviewed against this history (skills.py).
            skills.SkillRepo(cfg, identity).init()
        if not (home / "state.db").is_symlink():
            (home / "state.db").symlink_to(shared / "sessions.db")
        try:
            self.bridge = AgentBridge(cfg, user, channel, command_schema, session=session)
            settings = hermes_settings(cfg, self.bridge.url, cfg["project_root"],
                                       int(cfg["chat"].get("max_iterations", 30)), skills.enabled(cfg))
            config_path = home / "config.yaml"
            config_path.write_text(json.dumps(settings, indent=2))  # JSON is valid YAML
            config_path.chmod(0o600)
            (home / "SOUL.md").write_text("你是 Omnilaw，运行在用户本机的个人智能体。默认用中文回答。\n")
            self.log_path = home / "worker.log"
            self.log = self.log_path.open("a")
            self.log_path.chmod(0o600)
            self.process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("hermes_worker.py"))],
                cwd=cfg["project_root"], env=worker_environment(home, cfg["project_root"], self.bridge),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                text=True, encoding="utf-8", bufsize=1, start_new_session=True)
            self.reader = threading.Thread(target=self._read, daemon=True)
            self.reader.start()
            self._send({"source": str(source), "shared": str(shared), "session_id": "omnilaw-" + session_id,
                "model": cfg["model"]["model"], "base_url": self.bridge.url + "/v1", "token": self.bridge.token,
                "project_root": cfg["project_root"], "state_dir": cfg["state_dir"],
                "parent_pid": os.getpid(), "toolsets": TOOLSETS,
                "max_iterations": int(cfg["chat"].get("max_iterations", 30)),
                "max_seconds": int(cfg["chat"].get("max_seconds", 900)),
                "max_tokens": int(cfg["model"].get("max_tokens", 4096)),
                "command_schema": command_schema})
            ready = self._receive("ready", timeout=120)
            self.tools = ready["tools"]
        except BaseException:
            self.close()
            raise

    def _send(self, data):
        self.process.stdin.write(json.dumps(data, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _read(self):
        for line in self.process.stdout:
            try:
                self.events.put(json.loads(line))
            except ValueError:
                # Native libraries sometimes write directly to fd 1. Keep them
                # out of the protocol and show the diagnostic log on failure.
                self.log.write(line)
                self.log.flush()
        self.events.put({"type": "failed", "message": "智能体进程已退出；日志：" + str(self.log_path)})

    def _receive(self, expected, timeout):
        deadline, last_notice = time.monotonic() + timeout, time.monotonic()
        while time.monotonic() < deadline:
            try:
                event = self.events.get(timeout=1)
            except queue.Empty:
                if self.on_event and time.monotonic() - last_notice > 20:
                    self.on_event("智能体仍在执行…（Ctrl-C 可中断）")
                    last_notice = time.monotonic()
                continue
            kind = event.get("type")
            if kind == expected:
                return event
            if kind == "failed":
                raise RuntimeError(event["message"])
            if kind == "request":
                answer = self.on_request(event) if self.on_request else "deny"
                self._send({"answer": answer})
            elif kind == "event" and self.on_event:
                self.on_event(event["message"])
                last_notice = time.monotonic()
        raise RuntimeError("智能体执行超时；已停止本轮。")

    def run(self, text, history):
        self._send({"text": text, "history": history})
        try:
            return self._receive("result", int(self.cfg["chat"].get("max_seconds", 900)) + 30)
        except KeyboardInterrupt:
            self.process.send_signal(signal.SIGINT)
            try:
                return self._receive("result", 10)
            except (KeyboardInterrupt, RuntimeError):
                self.close()
                raise KeyboardInterrupt
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.bridge:
            self.last_messages = self.bridge.last_messages
        if self.process:
            if self.process.poll() is None:
                try:
                    self._send({"close": True})
                    self.process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(self.process.pid, signal.SIGKILL)
                        self.process.wait(timeout=3)
            self.process.stdin.close()
            if self.reader:
                self.reader.join(timeout=2)
            self.process.stdout.close()
            self.process = None
        if self.bridge:
            self.bridge.close()
            self.bridge = None
        if self.log:
            self.log.close()
            self.log = None
