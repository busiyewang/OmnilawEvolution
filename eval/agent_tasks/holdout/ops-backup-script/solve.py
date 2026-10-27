import os, subprocess, sys
ws = sys.argv[1]
path = os.path.join(ws, "backup.sh")
with open(path, "w") as f:
    f.write("#!/bin/sh\nset -e\ncd \"$(dirname \"$0\")\"\ntar --exclude='*.tmp' -czf backup.tar.gz docs\n")
os.chmod(path, 0o755)
subprocess.run([path], check=True, cwd=ws)
