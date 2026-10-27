"""Build the DRAFT agent task suite under eval/agent_tasks (docs/SKILL_EVOLUTION.md §4.2).

    python scripts/build_agent_tasks.py [eval_dir]

Every task is defined here, in one reviewable file, and written out as
``<split>/<id>/{task.json, workspace/, check.py, setup.py?, solve.py | reference/}``.
Checkers are deterministic and model-free; ``omnilaw-evolution agent-tasks check``
proves each one fails on the initial workspace and passes on the reference.

The suite stays ``draft`` until a human has reviewed it and runs
``omnilaw-evolution agent-tasks freeze --version <v>``. Rebuilding a frozen suite
resets it to draft.
"""
import json
import os
import shutil
import sys
import textwrap

VERSION = "2026-10-26.draft1"

DEBUG_SKILLS = ["systematic-debugging", "python-debugpy"]
CODING_SKILLS = ["test-driven-development", "spike"]
OPS_SKILLS = ["github"]

# Shared prelude of every check.py: the workspace path is argv[1].
CHECK_PRELUDE = r'''
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
'''

CHECK_MAIN = r'''
try:
    main()
except SystemExit:
    raise
except Exception as exc:
    fail("%s: %s" % (type(exc).__name__, exc))
print("ok")
'''

# Shared prelude of setup.py / solve.py scripts that drive git.
GIT_PRELUDE = r'''
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
'''

TASKS = []


def task(split, tid, category, skills, title, instruction, files=None, check="", solve=None, reference=None,
         setup=None, max_turns=20, max_seconds=300):
    TASKS.append(dict(split=split, id=tid, category=category, skills=skills, title=title,
                      instruction=textwrap.dedent(instruction).strip(), files=files or {}, check=check,
                      solve=solve, reference=reference, setup=setup, max_turns=max_turns, max_seconds=max_seconds))


def d(text):
    return textwrap.dedent(text).lstrip("\n")


# =============================================================== debugging
task("dev", "debug-paginate", "debugging", DEBUG_SKILLS, "分页函数越界",
     """
     pager.py 里的分页函数有 bug：用户反馈请求第 1 页拿到的是第 2 页的数据，总页数也算错了。
     请修复 paginate 和 page_count：page 从 1 开始；最后一页可以不满；n 为 0 时共 0 页；超出范围的页返回空列表。
     不要改函数签名。
     """,
     files={"pager.py": d('''
         def paginate(items, page, size):
             """Return the items on 1-based page `page`, `size` items per page."""
             start = page * size
             return items[start:start + size]


         def page_count(n, size):
             return n // size
         '''),
            "test_pager.py": d('''
         import unittest
         from pager import paginate


         class PagerTest(unittest.TestCase):
             def test_first_page(self):
                 self.assertEqual(paginate(list(range(10)), 1, 3), [0, 1, 2])


         if __name__ == "__main__":
             unittest.main()
         ''')},
     check=r'''
def main():
    m = load("pager")
    items = list(range(10))
    expect(m.paginate(items, 1, 3), [0, 1, 2], "page 1")
    expect(m.paginate(items, 4, 3), [9], "page 4")
    expect(m.paginate(items, 5, 3), [], "page 5")
    expect(m.page_count(10, 3), 4, "page_count(10, 3)")
    expect(m.page_count(9, 3), 3, "page_count(9, 3)")
    expect(m.page_count(0, 3), 0, "page_count(0, 3)")
''',
     reference={"pager.py": d('''
         def paginate(items, page, size):
             start = (page - 1) * size
             return items[start:start + size] if page >= 1 else []


         def page_count(n, size):
             return (n + size - 1) // size
         ''')})

task("dev", "debug-duration", "debugging", DEBUG_SKILLS, "时长解析只认一位数字",
     """
     durations.py 的 parse_duration('90m') 返回 0，而不是 5400。请修复：
     支持多位数字和 h/m/s 任意组合（如 '2h15m10s'）；输入为空、或含有无法识别的部分（如 'abc'、'10x'、'5m junk'）时抛出 ValueError。
     """,
     files={"durations.py": d('''
         import re

         UNITS = {"h": 3600, "m": 60, "s": 1}


         def parse_duration(text):
             """'1h30m' -> 5400, '45s' -> 45, '2h' -> 7200."""
             total = 0
             for value, unit in re.findall(r"(\\d)([hms])", text):
                 total += int(value) * UNITS[unit]
             return total
         ''')},
     check=r'''
def main():
    m = load("durations")
    for text, want in (("90m", 5400), ("1h30m", 5400), ("2h15m10s", 8110), ("45s", 45), ("2h", 7200)):
        expect(m.parse_duration(text), want, "parse_duration(%r)" % text)
    for bad in ("", "abc", "10x", "5m junk"):
        raises(ValueError, m.parse_duration, bad)
''',
     reference={"durations.py": d('''
         import re

         UNITS = {"h": 3600, "m": 60, "s": 1}


         def parse_duration(text):
             if not re.fullmatch(r"(\\d+[hms])+", text or ""):
                 raise ValueError("bad duration: %r" % text)
             return sum(int(v) * UNITS[u] for v, u in re.findall(r"(\\d+)([hms])", text))
         ''')})

task("dev", "debug-mutable-default", "debugging", DEBUG_SKILLS, "默认参数串数据",
     """
     tags.py 中，不传 tags 参数多次调用 add_tag，结果会互相串在一起（第二次调用返回了第一次的标签）。
     请修复。传入列表时，仍然要在原列表上追加并返回同一个列表。
     """,
     files={"tags.py": d('''
         def add_tag(tag, tags=[]):
             tags.append(tag.strip().lower())
             return tags
         ''')},
     check=r'''
def main():
    m = load("tags")
    expect(m.add_tag("A"), ["a"], "first call")
    expect(m.add_tag("B"), ["b"], "second call")
    given = ["x"]
    out = m.add_tag(" Y ", given)
    if out is not given:
        fail("must return the list that was passed in")
    expect(given, ["x", "y"], "in-place append")
''',
     reference={"tags.py": d('''
         def add_tag(tag, tags=None):
             if tags is None:
                 tags = []
             tags.append(tag.strip().lower())
             return tags
         ''')})

task("dev", "debug-csv-quotes", "debugging", DEBUG_SKILLS, "CSV 引号内逗号",
     """
     report.py 的 total_amount('data.csv') 结果不对：部分公司名里带逗号（按 CSV 规范用双引号括起来）。
     请修复 load_rows，使它能正确解析标准 CSV（包括引号内的逗号和转义的双引号）。
     """,
     files={"report.py": d('''
         def load_rows(path):
             rows = []
             with open(path, encoding="utf-8") as f:
                 header = f.readline().strip().split(",")
                 for line in f:
                     if line.strip():
                         rows.append(dict(zip(header, line.strip().split(","))))
             return rows


         def total_amount(path):
             return sum(float(r["amount"]) for r in load_rows(path))
         '''),
            "data.csv": 'name,amount\nGlobex,100.5\n"Acme, Inc.",200\n"Initech ""West""",34\nUmbrella,900\n'},
     check=r'''
def main():
    m = load("report")
    rows = m.load_rows(os.path.join(WS, "data.csv"))
    expect([r["name"] for r in rows], ["Globex", "Acme, Inc.", 'Initech "West"', "Umbrella"], "names")
    expect(m.total_amount(os.path.join(WS, "data.csv")), 1234.5, "total")
''',
     reference={"report.py": d('''
         import csv


         def load_rows(path):
             with open(path, encoding="utf-8", newline="") as f:
                 return [row for row in csv.DictReader(f) if any(row.values())]


         def total_amount(path):
             return sum(float(r["amount"]) for r in load_rows(path))
         ''')})

task("holdout", "debug-flatten", "debugging", DEBUG_SKILLS, "flatten 不递归",
     """
     utils.py 的 flatten 只展开了一层。请修复：任意深度的 list 和 tuple 都要展开，字符串当作整体，不要拆成字符。
     例如 flatten([1, [2, (3, 4)], [[5]]]) == [1, 2, 3, 4, 5]。
     """,
     files={"utils.py": d('''
         def flatten(value):
             out = []
             for item in value:
                 if type(item) == list:
                     out.extend(item)
                 else:
                     out.append(item)
             return out
         ''')},
     check=r'''
def main():
    m = load("utils")
    expect(m.flatten([1, [2, (3, 4)], [[5]]]), [1, 2, 3, 4, 5], "nested")
    expect(m.flatten(["ab", ["cd", ("ef",)]]), ["ab", "cd", "ef"], "strings")
    expect(m.flatten([]), [], "empty")
    expect(m.flatten([[[[]]], ()]), [], "empty nested")
''',
     reference={"utils.py": d('''
         def flatten(value):
             out = []
             for item in value:
                 if isinstance(item, (list, tuple)):
                     out.extend(flatten(item))
                 else:
                     out.append(item)
             return out
         ''')})

task("holdout", "debug-price-rounding", "debugging", DEBUG_SKILLS, "金额四舍五入错误",
     """
     money.py 的 format_price(2.675) 返回 '2.67'，财务要求是 '2.68'。
     请修复：保留两位小数、四舍五入（.5 一律远离零进位，-1.005 -> '-1.01'），带千位分隔符（1234.5 -> '1,234.50'）。
     参数可能是 float、int 或数字字符串。
     """,
     files={"money.py": d('''
         def format_price(amount):
             return "{:,.2f}".format(round(amount, 2))
         ''')},
     check=r'''
def main():
    m = load("money")
    for value, want in ((2.675, "2.68"), (1.005, "1.01"), (1234.5, "1,234.50"), (0.125, "0.13"),
                        (-1.005, "-1.01"), (3, "3.00"), ("1000000.555", "1,000,000.56")):
        expect(m.format_price(value), want, "format_price(%r)" % (value,))
''',
     reference={"money.py": d('''
         from decimal import Decimal, ROUND_HALF_UP


         def format_price(amount):
             value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
             return "{:,.2f}".format(value)
         ''')})

task("holdout", "debug-dict-mutation", "debugging", DEBUG_SKILLS, "遍历时删除键",
     """
     clean.py 的 remove_empty 运行时报 RuntimeError: dictionary changed size during iteration。
     请修复：递归删除值为 None、''、[] 或 {} 的键；子字典清理后若变成空的也要删除；0 和 False 保留；返回原字典对象。
     """,
     files={"clean.py": d('''
         def remove_empty(d):
             for k, v in d.items():
                 if isinstance(v, dict):
                     remove_empty(v)
                 if v in (None, "", [], {}):
                     del d[k]
             return d
         ''')},
     check=r'''
def main():
    m = load("clean")
    data = {"a": 1, "b": None, "c": {"d": "", "e": 2}, "f": {"g": []}, "h": 0, "i": False, "j": []}
    out = m.remove_empty(data)
    if out is not data:
        fail("must return the same dict")
    expect(data, {"a": 1, "c": {"e": 2}, "h": 0, "i": False}, "cleaned")
''',
     reference={"clean.py": d('''
         def remove_empty(d):
             for k in list(d):
                 v = d[k]
                 if isinstance(v, dict):
                     remove_empty(v)
                 if v is None or (isinstance(v, (str, list, dict)) and len(v) == 0):
                     del d[k]
             return d
         ''')})

task("holdout", "debug-ranking", "debugging", DEBUG_SKILLS, "并列排名顺序",
     """
     ranking.py 的 rank_players 在分数相同时顺序不稳定。要求：按分数从高到低，分数相同按名字字母升序。
     另外 top(scores, n) 应返回前 n 名，n 大于人数时返回全部。请修复。
     """,
     files={"ranking.py": d('''
         def rank_players(scores):
             return sorted(scores, key=lambda name: scores[name], reverse=True)


         def top(scores, n):
             return rank_players(scores)[:n - 1]
         ''')},
     check=r'''
def main():
    m = load("ranking")
    scores = {"bob": 10, "amy": 10, "cat": 12, "dan": 3}
    expect(m.rank_players(scores), ["cat", "amy", "bob", "dan"], "ranking")
    expect(m.top(scores, 2), ["cat", "amy"], "top 2")
    expect(m.top(scores, 9), ["cat", "amy", "bob", "dan"], "top 9")
''',
     reference={"ranking.py": d('''
         def rank_players(scores):
             return sorted(scores, key=lambda name: (-scores[name], name))


         def top(scores, n):
             return rank_players(scores)[:n]
         ''')})

# =============================================================== coding
task("dev", "code-slugify", "coding", CODING_SKILLS, "实现 slugify",
     """
     按 README.md 的规格在 slug.py 中实现 slugify(title)。
     """,
     files={"slug.py": d('''
         def slugify(title):
             raise NotImplementedError
         '''),
            "README.md": d('''
         # slugify 规格

         - 转小写；带重音的拉丁字母转为不带重音的字母（Crème -> creme）。
         - 只保留 ASCII 字母和数字；其他字符（空格、标点、下划线、中文等）连续出现时合并为一个 `-`。
         - 去掉开头和结尾的 `-`。
         - 结果为空时返回 `n-a`。
         ''')},
     check=r'''
def main():
    m = load("slug")
    for text, want in (("Hello, World!", "hello-world"), ("  Crème Brûlée  ", "creme-brulee"),
                       ("a--b__c", "a-b-c"), ("!!!", "n-a"), ("中文 Title 2", "title-2"), ("Ünïcödé", "unicode")):
        expect(m.slugify(text), want, "slugify(%r)" % text)
''',
     reference={"slug.py": d('''
         import re
         import unicodedata


         def slugify(title):
             text = unicodedata.normalize("NFKD", title)
             text = "".join(c for c in text if not unicodedata.combining(c)).lower()
             text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
             return text or "n-a"
         ''')})

task("dev", "code-lru", "coding", CODING_SKILLS, "实现 LRU 缓存",
     """
     在 lru.py 中实现 LRUCache(capacity)：get(key, default=None)、put(key, value)、len()。
     容量满时淘汰最久未被访问的键（get 和 put 都算访问）；capacity <= 0 时抛出 ValueError。
     """,
     files={"lru.py": d('''
         class LRUCache:
             def __init__(self, capacity):
                 raise NotImplementedError
         ''')},
     check=r'''
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
''',
     reference={"lru.py": d('''
         from collections import OrderedDict


         class LRUCache:
             def __init__(self, capacity):
                 if capacity <= 0:
                     raise ValueError("capacity must be positive")
                 self.capacity, self.data = capacity, OrderedDict()

             def get(self, key, default=None):
                 if key not in self.data:
                     return default
                 self.data.move_to_end(key)
                 return self.data[key]

             def put(self, key, value):
                 self.data[key] = value
                 self.data.move_to_end(key)
                 while len(self.data) > self.capacity:
                     self.data.popitem(last=False)

             def __len__(self):
                 return len(self.data)
         ''')})

task("dev", "code-roman", "coding", CODING_SKILLS, "罗马数字互转",
     """
     在 roman.py 中实现 to_roman(n) 和 from_roman(s)，范围 1..3999。
     超出范围的整数、非标准写法（如 'IIII'、'IC'、'VX'）和空字符串都抛出 ValueError。
     """,
     files={"roman.py": d('''
         def to_roman(n):
             raise NotImplementedError


         def from_roman(s):
             raise NotImplementedError
         ''')},
     check=r'''
def main():
    m = load("roman")
    for n, s in ((1, "I"), (4, "IV"), (9, "IX"), (14, "XIV"), (40, "XL"), (90, "XC"), (400, "CD"),
                 (1994, "MCMXCIV"), (3999, "MMMCMXCIX")):
        expect(m.to_roman(n), s, "to_roman(%d)" % n)
        expect(m.from_roman(s), n, "from_roman(%s)" % s)
    for n in range(1, 4000):
        if m.from_roman(m.to_roman(n)) != n:
            fail("round trip failed at %d" % n)
    for bad in (0, 4000, -1):
        raises(ValueError, m.to_roman, bad)
    for bad in ("", "IIII", "IC", "VX", "MMMM", "ABC"):
        raises(ValueError, m.from_roman, bad)
''',
     reference={"roman.py": d('''
         PAIRS = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
                  (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]


         def to_roman(n):
             if not isinstance(n, int) or not 1 <= n <= 3999:
                 raise ValueError("out of range")
             out = ""
             for value, sym in PAIRS:
                 while n >= value:
                     out, n = out + sym, n - value
             return out


         def from_roman(s):
             values = {sym: v for v, sym in PAIRS if len(sym) == 1}
             if not s or any(c not in values for c in s):
                 raise ValueError("invalid numeral")
             total = 0
             for i, c in enumerate(s):
                 v = values[c]
                 total += -v if i + 1 < len(s) and values[s[i + 1]] > v else v
             if not 1 <= total <= 3999 or to_roman(total) != s:
                 raise ValueError("non-canonical numeral")
             return total
         ''')})

task("dev", "code-intervals", "coding", CODING_SKILLS, "合并区间",
     """
     在 intervals.py 中实现 merge_intervals(intervals)：输入为 [start, end] 列表（无序），
     返回按起点排序的合并结果；首尾相接的区间（[1, 2] 和 [2, 3]）也要合并；不能修改输入；空输入返回 []。
     """,
     files={"intervals.py": d('''
         def merge_intervals(intervals):
             raise NotImplementedError
         ''')},
     check=r'''
def main():
    m = load("intervals")
    data = [[8, 10], [1, 3], [2, 6], [15, 18], [6, 7], [18, 20]]
    copy = [list(x) for x in data]
    expect(m.merge_intervals(data), [[1, 7], [8, 10], [15, 20]], "merge")
    expect(data, copy, "input unchanged")
    expect(m.merge_intervals([]), [], "empty")
    expect(m.merge_intervals([[5, 5]]), [[5, 5]], "point")
    expect(m.merge_intervals([[1, 10], [2, 3]]), [[1, 10]], "contained")
''',
     reference={"intervals.py": d('''
         def merge_intervals(intervals):
             out = []
             for start, end in sorted(intervals):
                 if out and start <= out[-1][1]:
                     out[-1][1] = max(out[-1][1], end)
                 else:
                     out.append([start, end])
             return out
         ''')})

task("holdout", "code-wrap", "coding", CODING_SKILLS, "按宽度折行",
     """
     在 wrap.py 中实现 wrap_text(text, width)，返回行列表：按单词贪心折行，每行尽量多放单词但长度不超过 width；
     连续空白视为一个分隔；单个单词比 width 长时独占一行、不拆开；空文本返回 []。
     """,
     files={"wrap.py": d('''
         def wrap_text(text, width):
             raise NotImplementedError
         ''')},
     check=r'''
def main():
    m = load("wrap")
    expect(m.wrap_text("the quick brown fox jumps", 10), ["the quick", "brown fox", "jumps"], "basic")
    expect(m.wrap_text("  a   b\n c  ", 3), ["a b", "c"], "whitespace")
    expect(m.wrap_text("supercalifragilistic is long", 8), ["supercalifragilistic", "is long"], "long word")
    expect(m.wrap_text("", 5), [], "empty")
''',
     reference={"wrap.py": d('''
         def wrap_text(text, width):
             lines, line = [], ""
             for word in text.split():
                 if not line:
                     line = word
                 elif len(line) + 1 + len(word) <= width:
                     line += " " + word
                 else:
                     lines.append(line)
                     line = word
             if line:
                 lines.append(line)
             return lines
         ''')})

task("holdout", "code-query", "coding", CODING_SKILLS, "解析查询字符串",
     """
     在 query.py 中实现 parse_query(qs)，返回 dict，每个键对应值列表（按出现顺序）。
     规则：忽略开头的 '?'；按 '&' 分割、跳过空段；没有 '=' 的键值为 ''；'+' 表示空格；支持 %XX（UTF-8）解码；空字符串返回 {}。
     """,
     files={"query.py": d('''
         def parse_query(qs):
             raise NotImplementedError
         ''')},
     check=r'''
def main():
    m = load("query")
    expect(m.parse_query("a=1&b=2&a=3&c=&d"), {"a": ["1", "3"], "b": ["2"], "c": [""], "d": [""]}, "basic")
    expect(m.parse_query("?q=hello+world&&x=%E4%B8%AD%26"), {"q": ["hello world"], "x": ["中&"]}, "decode")
    expect(m.parse_query(""), {}, "empty")
''',
     reference={"query.py": d('''
         from urllib.parse import unquote_plus


         def parse_query(qs):
             out = {}
             for part in qs.lstrip("?").split("&"):
                 if not part:
                     continue
                 key, _, value = part.partition("=")
                 out.setdefault(unquote_plus(key), []).append(unquote_plus(value))
             return out
         ''')})

task("holdout", "code-toposort", "coding", CODING_SKILLS, "拓扑排序",
     """
     在 toposort.py 中实现 topo_sort(deps)：deps 是 {节点: [前置节点, ...]}。返回所有节点（包括只作为前置出现的节点）的顺序，
     前置节点在前；多个节点同时可选时按名字升序；有环时抛出 ValueError。
     """,
     files={"toposort.py": d('''
         def topo_sort(deps):
             raise NotImplementedError
         ''')},
     check=r'''
def main():
    m = load("toposort")
    deps = {"app": ["lib", "log"], "lib": ["core"], "log": ["core"], "test": ["app"]}
    expect(m.topo_sort(deps), ["core", "lib", "log", "app", "test"], "order")
    expect(m.topo_sort({"b": [], "a": []}), ["a", "b"], "ties")
    raises(ValueError, m.topo_sort, {"a": ["b"], "b": ["c"], "c": ["a"]})
''',
     reference={"toposort.py": d('''
         def topo_sort(deps):
             nodes = set(deps) | {p for ps in deps.values() for p in ps}
             pending = {n: set(deps.get(n, [])) for n in nodes}
             out = []
             while pending:
                 ready = sorted(n for n, ps in pending.items() if not ps)
                 if not ready:
                     raise ValueError("cycle")
                 node = ready[0]
                 out.append(node)
                 del pending[node]
                 for ps in pending.values():
                     ps.discard(node)
             return out
         ''')})

task("holdout", "code-ratelimit", "coding", CODING_SKILLS, "令牌桶限流",
     """
     在 ratelimit.py 中实现 RateLimiter(rate, capacity, clock=time.monotonic)：令牌桶算法，初始为满，
     每秒连续补充 rate 个令牌，最多 capacity 个；allow() 有令牌时消耗 1 个并返回 True，否则返回 False。
     clock 参数用于测试注入时间。
     """,
     files={"ratelimit.py": d('''
         import time


         class RateLimiter:
             def __init__(self, rate, capacity, clock=time.monotonic):
                 raise NotImplementedError

             def allow(self):
                 raise NotImplementedError
         ''')},
     check=r'''
def main():
    m = load("ratelimit")
    now = [100.0]
    rl = m.RateLimiter(2, 3, clock=lambda: now[0])
    expect([rl.allow() for _ in range(4)], [True, True, True, False], "burst")
    now[0] += 0.5
    expect([rl.allow(), rl.allow()], [True, False], "after 0.5s")
    now[0] += 10
    expect([rl.allow() for _ in range(4)], [True, True, True, False], "refill capped")
''',
     reference={"ratelimit.py": d('''
         import time


         class RateLimiter:
             def __init__(self, rate, capacity, clock=time.monotonic):
                 self.rate, self.capacity, self.clock = rate, capacity, clock
                 self.tokens, self.last = float(capacity), clock()

             def allow(self):
                 now = self.clock()
                 self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                 self.last = now
                 if self.tokens >= 1:
                     self.tokens -= 1
                     return True
                 return False
         ''')})

# =============================================================== data
task("dev", "data-order-summary", "data", [], "用户订单汇总",
     """
     根据 users.csv 和 orders.csv 生成 summary.csv，表头为 name,orders,total：
     每个用户一行（没有订单的用户也要列出，orders 为 0，total 为 0.00），total 保留两位小数，
     按 total 从高到低排序，相同时按 name 升序。
     """,
     files={"users.csv": "id,name\n1,Alice\n2,Bob\n3,Carol\n4,Dave\n",
            "orders.csv": "order_id,user_id,amount\n10,1,19.99\n11,2,5\n12,1,0.01\n13,3,20\n14,2,15.5\n"},
     check=r'''
def main():
    expect(lines("summary.csv"), ["name,orders,total", "Bob,2,20.50", "Alice,2,20.00", "Carol,1,20.00",
                                  "Dave,0,0.00"], "summary.csv")
''',
     reference={"summary.csv": "name,orders,total\nBob,2,20.50\nAlice,2,20.00\nCarol,1,20.00\nDave,0,0.00\n"})

task("dev", "data-log-levels", "data", [], "日志级别计数",
     """
     app.log 每行是一条 JSON 日志（字段 level）。统计每个级别的条数写入 levels.json（对象：级别 -> 条数）。
     级别统一为大写；无法解析的行和缺少 level 的行跳过。
     """,
     files={"app.log": d('''
         {"level": "info", "msg": "start"}
         {"level": "ERROR", "msg": "boom"}
         not json at all
         {"level": "Info", "msg": "tick"}
         {"msg": "no level"}
         {"level": "warn", "msg": "slow"}
         {"level": "error", "msg": "again"}
         {"level": "INFO", "msg": "stop"}
         ''')},
     check=r'''
def main():
    expect(json.loads(read("levels.json")), {"INFO": 3, "ERROR": 2, "WARN": 1}, "levels.json")
''',
     reference={"levels.json": '{"INFO": 3, "ERROR": 2, "WARN": 1}\n'})

task("dev", "data-clean-csv", "data", [], "清洗 CSV",
     """
     清洗 messy.csv，输出 clean.csv（表头保持 name,date,city）：
     去掉每个字段首尾空白；日期统一为 YYYY-MM-DD（原始格式有 2024/03/05、05.03.2024（日.月.年）、2024-03-05、March 5, 2024）；
     清洗后完全相同的行只保留第一次出现的；保持原顺序。
     """,
     files={"messy.csv": d('''
         name,date,city
           Alice ,2024/03/05, Paris
         Bob,05.03.2024,Berlin
         Alice,2024-03-05,Paris
         Carol,"March 5, 2024",  Rome
         Bob, 2024-03-05 ,Berlin
         Dave,2024/12/31,Oslo
         ''')},
     check=r'''
def main():
    expect(lines("clean.csv"), ["name,date,city", "Alice,2024-03-05,Paris", "Bob,2024-03-05,Berlin",
                                "Carol,2024-03-05,Rome", "Dave,2024-12-31,Oslo"], "clean.csv")
''',
     reference={"clean.csv": "name,date,city\nAlice,2024-03-05,Paris\nBob,2024-03-05,Berlin\n"
                             "Carol,2024-03-05,Rome\nDave,2024-12-31,Oslo\n"})

task("dev", "data-emails", "data", [], "提取邮箱",
     """
     从 notes/ 目录下所有 .txt 文件中提取邮箱地址，转小写、去重、按字母排序，每行一个写入 emails.txt。
     注意句末标点和尖括号不属于邮箱。
     """,
     files={"notes/a.txt": "Contact bob@example.com. Or <ALICE@Example.COM>!\n",
            "notes/b.txt": "cc: carol.smith+news@mail.example.org, bob@example.com\nnot-an-email@ and @nope\n",
            "notes/c.md": "ignored@example.com\n"},
     check=r'''
def main():
    expect(lines("emails.txt"), ["alice@example.com", "bob@example.com", "carol.smith+news@mail.example.org"],
           "emails.txt")
''',
     reference={"emails.txt": "alice@example.com\nbob@example.com\ncarol.smith+news@mail.example.org\n"})

task("holdout", "data-pivot", "data", [], "销售透视表",
     """
     把 sales.csv（date,region,amount）汇总成 pivot.csv：第一列 month（YYYY-MM），其余每列是一个地区（按地区名升序），
     值为该月该地区 amount 之和，保留两位小数，没有数据填 0.00；按月份升序。
     """,
     files={"sales.csv": "date,region,amount\n2024-01-05,North,10\n2024-01-20,East,5.5\n2024-02-02,North,1.25\n"
                         "2024-01-31,North,2\n2024-03-15,West,7\n"},
     check=r'''
def main():
    expect(lines("pivot.csv"), ["month,East,North,West", "2024-01,5.50,12.00,0.00", "2024-02,0.00,1.25,0.00",
                                "2024-03,0.00,0.00,7.00"], "pivot.csv")
''',
     reference={"pivot.csv": "month,East,North,West\n2024-01,5.50,12.00,0.00\n2024-02,0.00,1.25,0.00\n"
                             "2024-03,0.00,0.00,7.00\n"})

task("holdout", "data-access-log", "data", [], "访问日志 Top 路径",
     """
     分析 access.log（常见日志格式），统计请求次数最多的 3 个路径（去掉查询字符串），
     写入 top_paths.json：[{"path": ..., "count": ...}, ...]，按次数降序，次数相同按路径升序。
     """,
     files={"access.log": d('''
         1.1.1.1 - - [01/Jan/2026:10:00:00 +0000] "GET /index.html HTTP/1.1" 200 512
         1.1.1.2 - - [01/Jan/2026:10:00:01 +0000] "GET /api/items?page=2 HTTP/1.1" 200 99
         1.1.1.3 - - [01/Jan/2026:10:00:02 +0000] "POST /api/items HTTP/1.1" 201 12
         1.1.1.1 - - [01/Jan/2026:10:00:03 +0000] "GET /about HTTP/1.1" 200 300
         1.1.1.4 - - [01/Jan/2026:10:00:04 +0000] "GET /index.html?ref=x HTTP/1.1" 200 512
         1.1.1.5 - - [01/Jan/2026:10:00:05 +0000] "GET /api/items HTTP/1.1" 304 0
         1.1.1.6 - - [01/Jan/2026:10:00:06 +0000] "GET /contact HTTP/1.1" 404 0
         1.1.1.7 - - [01/Jan/2026:10:00:07 +0000] "GET /about HTTP/1.1" 200 300
         ''')},
     check=r'''
def main():
    expect(json.loads(read("top_paths.json")), [{"path": "/api/items", "count": 3}, {"path": "/about", "count": 2},
                                                {"path": "/index.html", "count": 2}], "top_paths.json")
''',
     reference={"top_paths.json": '[{"path": "/api/items", "count": 3}, {"path": "/about", "count": 2}, '
                                  '{"path": "/index.html", "count": 2}]\n'})

task("holdout", "data-deep-merge", "data", [], "配置深度合并",
     """
     把 override.json 深度合并到 base.json 上，结果写入 merged.json：
     两边都是对象时递归合并；其他类型（包括列表）直接用 override 的值替换；override 中值为 null 的键表示删除该键。
     """,
     files={"base.json": json.dumps({"name": "svc", "server": {"host": "0.0.0.0", "port": 80, "tls": {"on": False}},
                                     "tags": ["a", "b"], "debug": True}, indent=2) + "\n",
            "override.json": json.dumps({"server": {"port": 8080, "tls": {"on": True, "cert": "x.pem"}},
                                         "tags": ["c"], "debug": None, "new": 1}, indent=2) + "\n"},
     check=r'''
def main():
    expect(json.loads(read("merged.json")), {"name": "svc", "server": {"host": "0.0.0.0", "port": 8080,
           "tls": {"on": True, "cert": "x.pem"}}, "tags": ["c"], "new": 1}, "merged.json")
''',
     reference={"merged.json": json.dumps({"name": "svc", "server": {"host": "0.0.0.0", "port": 8080,
                                           "tls": {"on": True, "cert": "x.pem"}}, "tags": ["c"], "new": 1}) + "\n"})

task("holdout", "data-grades", "data", [], "成绩平均分",
     """
     根据 grades.csv（student,subject,score）计算每个学生的平均分，写入 averages.csv（表头 student,average）：
     score 为空或 N/A 的记录不计；没有任何有效成绩的学生不输出；平均分四舍五入保留两位小数；按 student 升序。
     """,
     files={"grades.csv": "student,subject,score\nzoe,math,90\namy,math,80\namy,art,\nzoe,art,N/A\n"
                          "amy,bio,85\nmax,math,N/A\nkim,math,70\nkim,art,71\nkim,bio,71\n"},
     check=r'''
def main():
    expect(lines("averages.csv"), ["student,average", "amy,82.50", "kim,70.67", "zoe,90.00"], "averages.csv")
''',
     reference={"averages.csv": "student,average\namy,82.50\nkim,70.67\nzoe,90.00\n"})

# =============================================================== ops (shell + git)
task("dev", "ops-partial-commit", "ops", OPS_SKILLS, "只提交部分改动",
     """
     这是一个 Git 仓库。把 src/ 目录下的所有改动（包括新文件）提交为一个 commit，提交信息为 "feat: add util helpers"。
     notes.txt 的改动不要提交，保留在工作区。
     """,
     setup=GIT_PRELUDE + r'''
init()
write("src/app.py", "print('app')\n")
write("notes.txt", "draft\n")
git("add", "-A")
git("commit", "-q", "-m", "initial")
write("src/app.py", "from util import helper\nprint(helper())\n")
write("src/util.py", "def helper():\n    return 'ok'\n")
write("notes.txt", "draft\nmore notes\n")
''',
     check=r'''
def main():
    expect(git("log", "-1", "--format=%s"), "feat: add util helpers", "last commit message")
    expect(sorted(git("show", "--name-only", "--format=", "HEAD").split()), ["src/app.py", "src/util.py"], "committed files")
    expect(git("status", "--porcelain"), "M notes.txt", "remaining changes")
    expect(git("rev-list", "--count", "HEAD"), "2", "commit count")
''',
     solve=GIT_PRELUDE + r'''
git("add", "src")
git("commit", "-q", "-m", "feat: add util helpers")
''')

task("dev", "ops-todo-report", "ops", OPS_SKILLS, "TODO 清单",
     """
     找出当前目录下所有文件中的 TODO 注释（形如 "TODO: 内容"），跳过 vendor/ 目录，
     写入 todo_report.txt，每行格式为 "相对路径:行号: 内容"（内容为 "TODO:" 之后去掉首尾空白的文本），
     按路径、再按行号排序。
     """,
     files={"app/main.py": "import os\n# TODO: handle errors\nprint(1)  # TODO:  log output \n",
            "app/util.js": "// nothing here\n\n// TODO: remove debug flag\n",
            "README.md": "Docs\nTODO: write usage section\n",
            "vendor/lib.py": "# TODO: not ours\n"},
     check=r'''
def main():
    expect(lines("todo_report.txt"), ["README.md:2: write usage section", "app/main.py:2: handle errors",
                                      "app/main.py:3: log output", "app/util.js:3: remove debug flag"], "todo_report.txt")
''',
     reference={"todo_report.txt": "README.md:2: write usage section\napp/main.py:2: handle errors\n"
                                   "app/main.py:3: log output\napp/util.js:3: remove debug flag\n"})

task("dev", "ops-cherry-pick", "ops", OPS_SKILLS, "从标签建分支并摘取提交",
     """
     这是一个 Git 仓库。从标签 v1.0 创建分支 feature/greeting，并只把 dev 分支上提交信息为 "add greeting" 的那个提交
     cherry-pick 过来（dev 上的其他提交不要）。完成后停留在 feature/greeting 分支上。
     """,
     setup=GIT_PRELUDE + r'''
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
''',
     check=r'''
def main():
    expect(git("rev-parse", "--abbrev-ref", "HEAD"), "feature/greeting", "current branch")
    expect(git("log", "--format=%s"), "add greeting\nrelease 1.0", "history")
    if not os.path.exists(os.path.join(WS, "greeting.py")) or os.path.exists(os.path.join(WS, "debug.txt")):
        fail("expected greeting.py only")
''',
     solve=GIT_PRELUDE + r'''
git("checkout", "-q", "-b", "feature/greeting", "v1.0")
sha = subprocess.run(["git", "log", "dev", "--format=%H", "--grep=^add greeting$"], capture_output=True,
                     text=True, check=True).stdout.split()[0]
git("cherry-pick", sha)
''')

task("dev", "ops-rename-ext", "ops", OPS_SKILLS, "批量改扩展名",
     """
     把 photos/ 目录（含子目录）下所有扩展名为 .jpeg 的文件（不区分大小写）改名为同名的 .jpg 文件，内容不变；其他文件不要动。
     """,
     files={"photos/a.jpeg": "A", "photos/b.JPEG": "B", "photos/trip/c.jpeg": "C", "photos/trip/d.png": "D",
            "photos/e.jpg": "E", "notes.jpeg.txt": "N"},
     check=r'''
def main():
    found = sorted(os.path.relpath(os.path.join(r, f), WS) for r, _, fs in os.walk(WS) for f in fs)
    expect(found, ["notes.jpeg.txt", "photos/a.jpg", "photos/b.jpg", "photos/e.jpg", "photos/trip/c.jpg",
                   "photos/trip/d.png"], "files")
    expect(read("photos/b.jpg"), "B", "content kept")
''',
     solve=r'''
import os, sys
for root, _, files in os.walk(os.path.join(sys.argv[1], "photos")):
    for name in files:
        base, ext = os.path.splitext(name)
        if ext.lower() == ".jpeg":
            os.rename(os.path.join(root, name), os.path.join(root, base + ".jpg"))
''')

task("holdout", "ops-undo-commit", "ops", OPS_SKILLS, "撤销最后一次提交",
     """
     这是一个 Git 仓库。撤销最后一次提交（"oops: add debug output"），但保留它的改动，并让这些改动处于暂存（staged）状态。
     不要改动更早的提交。
     """,
     setup=GIT_PRELUDE + r'''
init()
write("app.py", "print('v1')\n")
git("add", "-A"); git("commit", "-q", "-m", "initial")
write("app.py", "print('v2')\n")
git("add", "-A"); git("commit", "-q", "-m", "feature: v2")
write("app.py", "print('v2')\nprint('debug')\n")
write("debug.log", "x\n")
git("add", "-A"); git("commit", "-q", "-m", "oops: add debug output")
''',
     check=r'''
def main():
    expect(git("log", "--format=%s"), "feature: v2\ninitial", "history")
    expect(sorted(git("diff", "--cached", "--name-only").split()), ["app.py", "debug.log"], "staged files")
    expect(git("diff", "--name-only"), "", "unstaged changes")
''',
     solve=GIT_PRELUDE + r'''
git("reset", "--soft", "HEAD~1")
''')

task("holdout", "ops-largest-files", "ops", OPS_SKILLS, "最大的三个文件",
     """
     找出 data/ 目录（含子目录）中最大的 3 个文件，写入 largest.txt，每行 "字节数<TAB>相对于 data/ 的路径"，
     按大小降序，大小相同按路径升序。
     """,
     files={"data/a.bin": "x" * 100, "data/b.bin": "x" * 300, "data/sub/c.bin": "x" * 300, "data/sub/d.bin": "x" * 50,
            "data/e.txt": "x" * 200, "big_outside.bin": "x" * 1000},
     check=r'''
def main():
    expect(lines("largest.txt"), ["300\tb.bin", "300\tsub/c.bin", "200\te.txt"], "largest.txt")
''',
     reference={"largest.txt": "300\tb.bin\n300\tsub/c.bin\n200\te.txt\n"})

task("holdout", "ops-merge-conflict", "ops", OPS_SKILLS, "解决合并冲突",
     """
     这是一个 Git 仓库，当前在 main 分支。把 feature 分支合并进 main。config.ini 会产生冲突：
     两个分支新增的配置项都要保留（timeout 和 retries），其余内容不变，然后完成合并提交。
     """,
     setup=GIT_PRELUDE + r'''
init()
write("config.ini", "[server]\nhost = localhost\nport = 8000\n")
git("add", "-A"); git("commit", "-q", "-m", "base config")
git("checkout", "-q", "-b", "feature")
write("config.ini", "[server]\nhost = localhost\nport = 8000\nretries = 5\n")
git("commit", "-q", "-am", "add retries")
git("checkout", "-q", "main")
write("config.ini", "[server]\nhost = localhost\nport = 8000\ntimeout = 30\n")
git("commit", "-q", "-am", "add timeout")
''',
     check=r'''
def main():
    import configparser
    expect(git("rev-parse", "--abbrev-ref", "HEAD"), "main", "branch")
    expect(len(git("log", "-1", "--format=%P").split()), 2, "merge commit parents")
    text = read("config.ini")
    if "<<<<<<<" in text or ">>>>>>>" in text or "=======" in text:
        fail("conflict markers left")
    cp = configparser.ConfigParser()
    cp.read_string(text)
    expect(dict(cp["server"]), {"host": "localhost", "port": "8000", "timeout": "30", "retries": "5"}, "server section")
    expect(git("status", "--porcelain"), "", "clean tree")
''',
     solve=GIT_PRELUDE + r'''
subprocess.run(["git", "merge", "feature"], capture_output=True, env=ENV)
write("config.ini", "[server]\nhost = localhost\nport = 8000\ntimeout = 30\nretries = 5\n")
git("add", "config.ini")
git("commit", "-q", "--no-edit")
''')

task("holdout", "ops-backup-script", "ops", OPS_SKILLS, "备份脚本",
     """
     编写可执行脚本 backup.sh：把 docs/ 目录打包为当前目录下的 backup.tar.gz（包内路径以 docs/ 开头），排除所有 .tmp 文件。
     写好后运行一次，生成 backup.tar.gz。
     """,
     files={"docs/a.md": "# A\n", "docs/sub/b.md": "# B\n", "docs/cache.tmp": "junk", "docs/sub/x.tmp": "junk",
            "other.md": "not included\n"},
     check=r'''
def main():
    import stat, tarfile
    path = os.path.join(WS, "backup.sh")
    if not os.path.isfile(path) or not os.stat(path).st_mode & stat.S_IXUSR:
        fail("backup.sh missing or not executable")
    with tarfile.open(os.path.join(WS, "backup.tar.gz")) as tar:
        names = sorted(m.name.rstrip("/").lstrip("./") for m in tar.getmembers() if m.isfile())
    expect(names, ["docs/a.md", "docs/sub/b.md"], "archive files")
''',
     solve=r'''
import os, subprocess, sys
ws = sys.argv[1]
path = os.path.join(ws, "backup.sh")
with open(path, "w") as f:
    f.write("#!/bin/sh\nset -e\ncd \"$(dirname \"$0\")\"\ntar --exclude='*.tmp' -czf backup.tar.gz docs\n")
os.chmod(path, 0o755)
subprocess.run([path], check=True, cwd=ws)
''')

# =============================================================== guard (unrelated to any candidate)
task("guard", "guard-hello", "guard", [], "Hello 脚本",
     """
     写一个 hello.py，运行时输出一行 "Hello, Omnilaw"。
     """,
     check=r'''
def main():
    out = subprocess.run([sys.executable, os.path.join(WS, "hello.py")], capture_output=True, text=True, cwd=WS)
    expect(out.stdout.strip(), "Hello, Omnilaw", "output")
''',
     reference={"hello.py": "print('Hello, Omnilaw')\n"})

task("guard", "guard-count-lines", "guard", [], "统计非空行",
     """
     统计 words.txt 中非空行（去掉空白后不为空）的数量，把数字写入 count.txt。
     """,
     files={"words.txt": "alpha\n\nbeta\n   \ngamma\ndelta\n\n"},
     check=r'''
def main():
    expect(read("count.txt").strip(), "4", "count")
''',
     reference={"count.txt": "4\n"})

task("guard", "guard-syntax", "guard", [], "修复语法错误",
     """
     calc.py 无法导入，请修复语法错误，保持 add 和 mul 的行为不变。
     """,
     files={"calc.py": "def add(a, b)\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"},
     check=r'''
def main():
    m = load("calc")
    expect((m.add(2, 3), m.mul(4, 5)), (5, 20), "add/mul")
''',
     reference={"calc.py": "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"})

task("guard", "guard-sort-names", "guard", [], "名字排序去重",
     """
     整理 names.txt：去掉每行首尾空白和空行、去重，按字母升序（区分大小写）写入 sorted.txt，每行一个。
     """,
     files={"names.txt": "carol\n alice\nbob\n\nalice \nDave\n"},
     check=r'''
def main():
    expect(lines("sorted.txt"), ["Dave", "alice", "bob", "carol"], "sorted.txt")
''',
     reference={"sorted.txt": "Dave\nalice\nbob\ncarol\n"})

task("guard", "guard-config-json", "guard", [], "按说明生成配置",
     """
     按 README.md 的说明生成 config.json。
     """,
     files={"README.md": "生成 config.json，内容为对象：name 为 \"omnilaw\"，port 为整数 8080，debug 为 false，"
                         "tags 为数组 [\"a\", \"b\"]。\n"},
     check=r'''
def main():
    expect(json.loads(read("config.json")), {"name": "omnilaw", "port": 8080, "debug": False, "tags": ["a", "b"]},
           "config.json")
''',
     reference={"config.json": '{"name": "omnilaw", "port": 8080, "debug": false, "tags": ["a", "b"]}\n'})

task("guard", "guard-palindrome", "guard", [], "回文判断",
     """
     在 pal.py 中实现 is_palindrome(s)：忽略大小写和所有非字母数字字符，判断是否回文。空字符串算回文。
     """,
     files={"pal.py": "def is_palindrome(s):\n    raise NotImplementedError\n"},
     check=r'''
def main():
    m = load("pal")
    for text, want in (("A man, a plan, a canal: Panama", True), ("race a car", False), ("", True), ("No 'x' in Nixon", True)):
        expect(m.is_palindrome(text), want, "is_palindrome(%r)" % text)
''',
     reference={"pal.py": d('''
         def is_palindrome(s):
             t = [c.lower() for c in s if c.isalnum()]
             return t == t[::-1]
         ''')})


def build(eval_dir):
    root = os.path.join(eval_dir, "agent_tasks")
    if os.path.isdir(root):
        shutil.rmtree(root)
    splits = {"dev": [], "holdout": [], "guard": []}
    for t in TASKS:
        d_ = os.path.join(root, t["split"], t["id"])
        os.makedirs(os.path.join(d_, "workspace"))
        for rel, text in t["files"].items():
            path = os.path.join(d_, "workspace", rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        with open(os.path.join(d_, "task.json"), "w", encoding="utf-8") as f:
            json.dump({k: t[k] for k in ("title", "category", "skills", "instruction", "max_turns", "max_seconds")},
                      f, ensure_ascii=False, indent=2)
            f.write("\n")
        with open(os.path.join(d_, "check.py"), "w", encoding="utf-8") as f:
            f.write(CHECK_PRELUDE + t["check"] + CHECK_MAIN)
        for name in ("setup", "solve"):
            if t[name]:
                with open(os.path.join(d_, name + ".py"), "w", encoding="utf-8") as f:
                    f.write(t[name].lstrip("\n"))
        for rel, text in (t["reference"] or {}).items():
            path = os.path.join(d_, "reference", rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        splits[t["split"]].append(t["id"])
    manifest = {"version": VERSION, "status": "draft", "splits": splits,
                "note": "Draft for human review; freeze with `omnilaw-evolution agent-tasks freeze --version <v>`."}
    with open(os.path.join(root, "MANIFEST.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return {k: len(v) for k, v in splits.items()}


if __name__ == "__main__":
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print(build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "eval")))
