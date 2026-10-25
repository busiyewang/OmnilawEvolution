"""Execution isolation for candidate code.

Source isolation (a separate git checkout) does not stop a process from
touching the host, so candidate code is also run under an OS sandbox:

* macOS: ``sandbox-exec`` profile — no network, no reads of protected paths,
  writes only to the run's scratch directory.
* Linux: ``bwrap`` if installed — read-only bind of /, tmpfs scratch, protected
  paths masked, network unshared.
* Otherwise ``none``: plain subprocess with a scrubbed environment. The
  isolation level is recorded with every evaluation so reports can say so.
"""
from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import time

# OMNILAW_MODEL_DIR is a path, not a secret: the semantic runtime reads the pinned model from it.
SAFE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "HOME", "TMPDIR", "OMNILAW_MODEL_DIR")


class SandboxTimeout(Exception):
    pass


class SandboxStopped(Exception):
    pass


def detect_mode(requested="auto"):
    if requested != "auto":
        return requested
    if platform.system() == "Darwin" and shutil.which("sandbox-exec"):
        return "sandbox-exec"
    if platform.system() == "Linux" and shutil.which("bwrap"):
        return "bwrap"
    return "none"


def _sb_quote(path):
    return '"%s"' % os.path.realpath(path).replace("\\", "\\\\").replace('"', '\\"')


def darwin_profile(deny_read, allow_write, network=False):
    lines = ["(version 1)", "(allow default)"]
    if not network:
        lines.append("(deny network*)")
    for p in deny_read:
        lines.append("(deny file-read* file-write* (subpath %s))" % _sb_quote(p))
    allowed = " ".join("(subpath %s)" % _sb_quote(p) for p in allow_write)
    lines.append('(deny file-write* (require-not (require-any %s (literal "/dev/null") '
                 '(literal "/dev/dtracehelper") (regex #"^/dev/tty"))))' % allowed)
    return "\n".join(lines) + "\n"


def build_command(cmd, mode, deny_read, allow_write, network, scratch):
    if mode == "sandbox-exec":
        profile = os.path.join(scratch, "sandbox.sb")
        with open(profile, "w") as f:
            f.write(darwin_profile(deny_read, allow_write, network))
        return ["sandbox-exec", "-f", profile] + list(cmd)
    if mode == "bwrap":
        args = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--die-with-parent"]
        if not network:
            args.append("--unshare-net")
        for p in deny_read:
            if os.path.isdir(p):
                args += ["--tmpfs", os.path.realpath(p)]
        for p in allow_write:
            args += ["--bind", os.path.realpath(p), os.path.realpath(p)]
        return args + list(cmd)
    return list(cmd)


def run(cmd, *, scratch, deny_read=(), allow_write=(), network=False, timeout=60, cwd=None,
        mode="auto", stop_check=None, env_extra=None, poll=0.2):
    """Run ``cmd`` isolated. Returns dict(returncode, stdout, stderr, seconds, isolation).
    Raises SandboxTimeout / SandboxStopped after killing the process group."""
    os.makedirs(scratch, exist_ok=True)
    mode = detect_mode(mode)
    full = build_command(cmd, mode, list(deny_read), [scratch] + list(allow_write), network, scratch)
    env = {k: os.environ[k] for k in SAFE_ENV_KEYS if k in os.environ}
    env["TMPDIR"] = scratch
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(env_extra or {})
    out_path, err_path = os.path.join(scratch, "stdout.txt"), os.path.join(scratch, "stderr.txt")
    start = time.time()
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        proc = subprocess.Popen(full, stdout=out, stderr=err, stdin=subprocess.DEVNULL, cwd=cwd, env=env,
                                start_new_session=True)
        try:
            while True:
                rc = proc.poll()
                if rc is not None:
                    break
                if time.time() - start > timeout:
                    _kill(proc)
                    raise SandboxTimeout("process exceeded %ss" % timeout)
                if stop_check and stop_check():
                    _kill(proc)
                    raise SandboxStopped("stop requested")
                time.sleep(poll)
        except BaseException:
            _kill(proc)
            raise
    with open(out_path, encoding="utf-8", errors="replace") as f:
        stdout = f.read()
    with open(err_path, encoding="utf-8", errors="replace") as f:
        stderr = f.read()
    return {"returncode": rc, "stdout": stdout, "stderr": stderr, "seconds": round(time.time() - start, 3),
            "isolation": mode}


def _kill(proc):
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    proc.wait()
