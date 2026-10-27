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

git("checkout", "-q", "-b", "feature/greeting", "v1.0")
sha = subprocess.run(["git", "log", "dev", "--format=%H", "--grep=^add greeting$"], capture_output=True,
                     text=True, check=True).stdout.split()[0]
git("cherry-pick", sha)
