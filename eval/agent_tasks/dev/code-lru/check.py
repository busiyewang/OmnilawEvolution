
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
    m = load("lru")
    raises(ValueError, m.LRUCache, 0)
    c = m.LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    expect(c.get("a"), 1, "get a")
    c.put("c", 3)
    expect(c.get("b"), None, "b evicted")
    expect(c.get("b", "x"), "x", "default")
    c.put("a", 10)
    c.put("d", 4)
    expect((c.get("a"), c.get("c"), c.get("d")), (10, None, 4), "after updates")
    expect(len(c), 2, "len")

try:
    main()
except SystemExit:
    raise
except Exception as exc:
    fail("%s: %s" % (type(exc).__name__, exc))
print("ok")
