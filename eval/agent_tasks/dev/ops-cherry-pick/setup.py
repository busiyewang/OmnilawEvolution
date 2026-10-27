import os, subprocess, sys
WS = sys.argv[1]
os.chdir(WS)
ENV = dict(os.environ, GIT_AUTHOR_DATE="2026-01-01T00:00:00", GIT_COMMITTER_DATE="2026-01-01T00:00:00")

def git(*args):
    subprocess.run(["git"] + list(args), check=True, capture_output=True, env=ENV)

def write(rel, text):
    os.makedirs(os.path.dirname(rel) or ".", exist_ok=True)
    with open(rel, "w", encoding="utf-8") as f:
        f.write(text)

def init():
    git("init", "-q", "-b", "main")
    git("config", "user.name", "Dev")
    git("config", "user.email", "dev@example.com")

init()
write("README.md", "v1\n")
git("add", "-A"); git("commit", "-q", "-m", "release 1.0")
git("tag", "v1.0")
write("CHANGELOG.md", "after 1.0\n")
git("add", "-A"); git("commit", "-q", "-m", "post-release changes")
git("checkout", "-q", "-b", "dev")
write("debug.txt", "debugging\n")
git("add", "-A"); git("commit", "-q", "-m", "wip: debug output")
write("greeting.py", "print('hello')\n")
git("add", "-A"); git("commit", "-q", "-m", "add greeting")
git("checkout", "-q", "main")
