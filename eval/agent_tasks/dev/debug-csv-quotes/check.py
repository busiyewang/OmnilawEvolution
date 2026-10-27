
import importlib.util, json, os, subprocess, sys
WS = os.path.realpath(sys.argv[1])

def fail(msg):
    print("FAIL: " + str(msg))
    sys.exit(1)

def load(name):
    path = os.path.join(WS, name + ".py")
    if not os.path.exists(path):
        fail("missing %s.py" % name)
    sys.path.insert(0, WS)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def read(rel):
    path = os.path.join(WS, rel)
    if not os.path.isfile(path):
        fail("missing " + rel)
    with open(path, encoding="utf-8") as f:
        return f.read()

def lines(rel):
    return [l.rstrip("\r") for l in read(rel).rstrip("\n").split("\n")]

def git(*args):
    res = subprocess.run(["git"] + list(args), cwd=WS, capture_output=True, text=True)
    return res.stdout.strip()

def expect(actual, expected, what):
    if actual != expected:
        fail("%s: expected %r, got %r" % (what, expected, actual))

def raises(exc, fn, *args):
    try:
        fn(*args)
    except exc:
        return
    except Exception as other:
        fail("%s%r raised %s, expected %s" % (getattr(fn, "__name__", fn), args, type(other).__name__, exc.__name__))
    fail("%s%r did not raise %s" % (getattr(fn, "__name__", fn), args, exc.__name__))

def main():
    m = load("report")
    rows = m.load_rows(os.path.join(WS, "data.csv"))
    expect([r["name"] for r in rows], ["Globex", "Acme, Inc.", 'Initech "West"', "Umbrella"], "names")
    expect(m.total_amount(os.path.join(WS, "data.csv")), 1234.5, "total")

try:
    main()
except SystemExit:
    raise
except Exception as exc:
    fail("%s: %s" % (type(exc).__name__, exc))
print("ok")
