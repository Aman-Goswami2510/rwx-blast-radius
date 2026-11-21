"""Blast Radius selector: coverage-map driven test selection for sqlglot (pinned commit).

Pure standard library. Reads /data/tests.json and /data/commits/*.patch, prints one JSON line per commit.

How it works (see DESIGN.md):
  * selector_map.bin is built at development time from the pinned commit itself (per-test line coverage,
    classes each test instantiates, fixture files each test opens, import graph).  For every source line it
    stores the set of tests that exercise it.  It holds no information about any patch.
  * For a patch we map every changed line to the tests that exercise it, anchor pure insertions to their
    neighbours, treat import-time lines through class/module usage, rebuild the patched file in memory and
    compile it (a file that no longer compiles breaks everything that imports it), then split the chosen
    tests over at most 8 workers so that makespan + 2 s per worker is minimal.
"""
import ast
import io
import json
import marshal
import os
import re
import sys
import tokenize
import zipfile
import zlib

DATA = os.environ.get("DATA_DIR", "/data")
HERE = os.path.dirname(os.path.abspath(__file__))
ALL = "ALL"

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

INERT_EXT = (".md", ".rst", ".txt", ".png", ".jpg", ".svg", ".gif", ".html", ".css", ".js", ".rs", ".lock", ".typed")
INERT_PREFIX = ("docs/", "posts/", "pdoc/", "benchmarks/", "sqlglotrs/", ".github/", "sqlglot-integration-tests/",
                "sqlglotc/")
INERT_NAMES = ("LICENSE", "CHANGELOG.md", "README.md", "CONTRIBUTING.md", "AGENTS.md", "CLAUDE.md", "Makefile",
               "MANIFEST.in", ".gitignore", ".gitattributes", ".pre-commit-config.yaml", ".editorconfig",
               ".readthedocs.yml", "sqlglot.png")
ALL_NAMES = ("pyproject.toml", "setup.py", "setup.cfg", "conftest.py", "pytest.ini", "tox.ini")


def load_map():
    blob = open(os.path.join(HERE, "selector_map.bin"), "rb").read()
    return marshal.loads(zlib.decompress(blob))


def parse_patch(text):
    files = []
    cur = None
    hunk = None
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            cur = {"old": m.group(1) if m else None, "new": m.group(2) if m else None, "hunks": [],
                   "new_file": False, "deleted": False, "rename": False, "binary": False, "garbled": m is None}
            files.append(cur)
            hunk = None
        elif cur is None:
            continue
        elif hunk is None and line.startswith("new file mode"):
            cur["new_file"] = True
        elif hunk is None and line.startswith("deleted file mode"):
            cur["deleted"] = True
        elif hunk is None and line.startswith("rename from "):
            cur["rename"] = True
            cur["old"] = line[len("rename from "):]
        elif hunk is None and line.startswith("rename to "):
            cur["new"] = line[len("rename to "):]
        elif hunk is None and (line.startswith("Binary files") or line.startswith("GIT binary patch")):
            cur["binary"] = True
        elif line.startswith("@@"):
            m = HUNK.match(line)
            if not m:
                cur["garbled"] = True
                hunk = None
                continue
            a, b = int(m.group(1)), m.group(2)
            hunk = {"start": a, "count": 1 if b is None else int(b), "lines": []}
            cur["hunks"].append(hunk)
        elif hunk is not None:
            if line[:1] in (" ", "+", "-", "\\"):
                hunk["lines"].append(line)
            elif line == "":
                hunk["lines"].append(" ")  # blank context line whose trailing space was stripped
            elif line.startswith("diff ") or line.startswith("index "):
                hunk = None
    return files


def groups_of(hunk):
    """Yield change groups: (removed old line numbers, added [(gap, text)])."""
    ol = hunk["start"] if hunk["count"] > 0 else hunk["start"] + 1
    rem, add = [], []
    out = []
    for ln in hunk["lines"]:
        c = ln[:1]
        if c == " ":
            if rem or add:
                out.append((rem, add))
                rem, add = [], []
            ol += 1
        elif c == "-":
            rem.append(ol)
            ol += 1
        elif c == "+":
            add.append((ol - 1, ln[1:]))
    if rem or add:
        out.append((rem, add))
    return out


def apply_hunks(old_text, hunks):
    old = old_text.split("\n")
    if old and old[-1] == "":
        old.pop()
    new = []
    pos = 0
    for h in hunks:
        start = (h["start"] - 1) if h["count"] > 0 else h["start"]
        new.extend(old[pos:start])
        pos = start
        for ln in h["lines"]:
            c = ln[:1]
            if c == " ":
                new.append(old[pos] if pos < len(old) else ln[1:])
                pos += 1
            elif c == "-":
                pos += 1
            elif c == "+":
                new.append(ln[1:])
    new.extend(old[pos:])
    return "\n".join(new) + "\n"


def literal_only(adds):
    """True when the added text contains no names (apart from True/False/None and the assignment target), i.e. it
    cannot raise while a module is imported."""
    text = "\n".join(t for _, t in adds)
    toks = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            toks.append(tok)
    except Exception:
        pass
    line_first = True
    for i, tok in enumerate(toks):
        if tok.type in (tokenize.INDENT, tokenize.DEDENT, tokenize.COMMENT):
            continue
        if tok.type in (tokenize.NEWLINE, tokenize.NL):
            line_first = True
            continue
        if tok.type == tokenize.NAME:
            if tok.string in ("True", "False", "None"):
                pass
            elif line_first and i + 1 < len(toks) and toks[i + 1].string in ("=", ":"):
                pass
            else:
                return False
        elif tok.type not in (tokenize.STRING, tokenize.NUMBER, tokenize.OP, tokenize.ENDMARKER):
            return False
        line_first = False
    return True


class _Norm(ast.NodeTransformer):
    """Normalise comparisons that mean the same thing: `x is None` == `x == None`, `x is Enum.MEMBER` == `x == Enum.MEMBER`."""

    @staticmethod
    def _singleton(n):
        if isinstance(n, ast.Constant):
            return n.value is None
        if isinstance(n, ast.Attribute):
            return n.attr.isupper() and isinstance(n.value, (ast.Name, ast.Attribute))
        return False

    def visit_Compare(self, node):
        self.generic_visit(node)
        ops = []
        for op, comp in zip(node.ops, node.comparators):
            if isinstance(op, ast.Is) and self._singleton(comp):
                op = ast.Eq()
            elif isinstance(op, ast.IsNot) and self._singleton(comp):
                op = ast.NotEq()
            ops.append(op)
        node.ops = ops
        return node


def semantically_equal(old_text, new_text):
    """True when both sources parse to the same tree after normalising equivalent comparisons.
    Comments, blank lines and formatting never appear in the tree, so they are equal by construction."""
    try:
        a = ast.dump(_Norm().visit(ast.parse(old_text)), include_attributes=False)
        b = ast.dump(_Norm().visit(ast.parse(new_text)), include_attributes=False)
    except Exception:
        return False
    return a == b


class Selector:
    def __init__(self):
        self.M = load_map()
        self.sigs = self.M["sigs"]
        self.zf = None
        self.sigcache = {}

    def sig(self, s):
        r = self.sigcache.get(s)
        if r is None:
            r = self.sigcache[s] = frozenset(self.sigs[s])
        return r

    def old_source(self, path):
        if self.zf is None:
            self.zf = zipfile.ZipFile(os.path.join(HERE, "src.zip"))
        return self.zf.read(path).decode("utf-8")

    # ------------------------------------------------------------------ python files
    def python_file(self, path, fd, out):
        info = self.M["files"].get(path)
        if info is None:
            return False
        ef, pq, cx, ci, ins = info["ef"], info["pq"], info["cx"], info["ci"], info["ins"]
        efw = info.get("efw")
        n = info["n"]
        if fd["new_file"]:
            return False
        if not fd["deleted"] and not fd["rename"] and fd["hunks"] and not any(h["start"] <= 2 for h in fd["hunks"]):
            try:
                if semantically_equal(self.old_source(path), apply_hunks(self.old_source(path), fd["hunks"])):
                    return True  # nothing that executes has changed (comments / formatting / equivalent comparisons)
            except Exception:
                pass
        if fd["deleted"] or fd["rename"] and fd["new"] != fd["old"]:
            out.add(self.sig(info["imp"]))
            out.add(self.sig(self.M["misc"]["dialect_imports"]))
            if fd["deleted"]:
                return True
        nontrivial = False
        for h in fd["hunks"]:
            for rem, add in groups_of(h):
                code_rem = [l for l in rem if 1 <= l <= n and ef[l - 1] != -1]
                for l in rem:
                    if l < 1 or l > n:
                        out.add(self.sig(info["imp"]))
                        nontrivial = True
                lit = None
                for l in code_rem:
                    s_ = ef[l - 1]
                    w_ = efw[l - 1] if efw else -2
                    if w_ != -2:
                        if lit is None:
                            lit = literal_only(add)
                        if not lit:
                            s_ = w_
                    out.add(self.sig(s_))
                    nontrivial = True
                code_add = [(g, t) for g, t in add if t.strip() and not t.lstrip().startswith("#")]
                if not rem:
                    # pure insertion
                    for g, t in add:
                        g = max(0, min(g, n))
                        is_code = bool(t.strip()) and not t.lstrip().startswith("#")
                        if not is_code:
                            # blank / comment: harmless unless it can change tokenisation
                            prev = ""
                            if g >= 1:
                                prev = self.prev_line(path, g)
                            if ins[g] or (g < 2 and "coding" in t) or prev.rstrip().endswith("\\"):
                                out.add(self.sig(pq[g]))
                                nontrivial = True
                            continue
                        indent = len(t) - len(t.lstrip())
                        out.add(self.sig(pq[g]))
                        if indent == 0:
                            out.add(self.sig(info["mod"]))
                        if cx[g] and indent <= ci[g]:
                            out.add(self.sig(cx[g]))
                        nontrivial = True
                elif not code_rem and code_add:
                    # a comment/blank line replaced by code: treat like an insertion
                    for g, t in code_add:
                        g = max(0, min(g, n))
                        indent = len(t) - len(t.lstrip())
                        out.add(self.sig(pq[g]))
                        if indent == 0:
                            out.add(self.sig(info["mod"]))
                        if cx[g] and indent <= ci[g]:
                            out.add(self.sig(cx[g]))
                        nontrivial = True
                elif rem and add and not code_rem:
                    # comment replaced by comment: only tokenisation hazards matter
                    for g, t in add:
                        g = max(0, min(g, n))
                        if ins[g]:
                            out.add(self.sig(pq[g]))
                            nontrivial = True
        if nontrivial or True:
            self.syntax_check(path, fd, info, out)
        return True

    def prev_line(self, path, g):
        try:
            return self.old_source(path).split("\n")[g - 1]
        except Exception:
            return "\\"

    def syntax_check(self, path, fd, info, out):
        """A patched module that no longer compiles breaks every test that imports it."""
        has_removed = any(ln[:1] == "-" for h in fd["hunks"] for ln in h["lines"])
        has_added_code = any(
            ln[:1] == "+" and ln[1:].strip() and not ln[1:].lstrip().startswith("#")
            for h in fd["hunks"] for ln in h["lines"])
        if not (has_removed or has_added_code):
            return
        try:
            new = apply_hunks(self.old_source(path), fd["hunks"])
            compile(new, path, "exec", dont_inherit=True)
        except SyntaxError:
            out.add(self.sig(info["imp"]))
            out.add(self.sig(self.M["misc"]["dialect_imports"]))
            if path.startswith("sqlglot/"):
                # a package module that no longer compiles breaks the dialect registry and everything using it;
                # running every test costs ~45 s (bounded by the longest test), a miss costs 300 s
                out.add(ALL)
        except Exception:
            out.add(ALL)

    # ------------------------------------------------------------------ one commit
    def select(self, text):
        out = set()
        for fd in parse_patch(text):
            if fd["garbled"]:
                out.add(ALL)
                continue
            for path in {p for p in (fd["old"], fd["new"]) if p}:
                self.one_path(path, fd, out)
            if ALL in out:
                return ALL
        return out

    def one_path(self, path, fd, out):
        base = os.path.basename(path)
        if path.startswith("tests/fixtures/"):
            s = self.M["fixtures"].get(path)
            if s is not None:
                out.add(self.sig(s))
            return
        if path.endswith(".py") and path.startswith(("sqlglot/", "tests/")):
            if path == fd["new"] and fd["new_file"]:
                out.add(self.sig(self.M["misc"]["dialect_imports"]))
                return
            if path == fd["old"] or path == fd["new"]:
                handled = self.python_file(path, fd, out) if path == fd["old"] or not fd["rename"] else True
                if not handled:
                    # unknown python file: be safe unless it is a brand new module
                    if not fd["new_file"]:
                        out.add(ALL)
            return
        if base in ALL_NAMES:
            out.add(ALL)
            return
        if base in INERT_NAMES or path.startswith(INERT_PREFIX) or path.endswith(INERT_EXT):
            if path.startswith("sqlglotc/") or path.startswith("tests/"):
                out.add(self.sig(self.M["test_files"].get("tests/test_sqlglotc.py", self.M["misc"]["all"])))
            return
        out.add(ALL)


def partition(nums, durs, max_k=8):
    """Split tests over <= max_k workers minimising makespan + 2000 ms per worker (integer ms)."""
    if not nums:
        return []
    items = sorted(nums, key=lambda n: (-durs[n], n))
    best = None
    for k in range(1, min(max_k, len(items)) + 1):
        loads = [0] * k
        work = [[] for _ in range(k)]
        for n in items:
            i = loads.index(min(loads))
            work[i].append(n)
            loads[i] += durs[n]
        # local improvement: move or swap items from the busiest worker
        for _ in range(200):
            hi = loads.index(max(loads))
            improved = False
            for n in list(work[hi]):
                d = durs[n]
                for j in range(k):
                    if j == hi:
                        continue
                    if loads[j] + d < loads[hi]:
                        work[hi].remove(n)
                        work[j].append(n)
                        loads[hi] -= d
                        loads[j] += d
                        improved = True
                        break
                    for m in list(work[j]):
                        dm = durs[m]
                        if dm < d and loads[j] + d - dm < loads[hi] and loads[hi] - d + dm < loads[hi]:
                            work[hi].remove(n)
                            work[j].remove(m)
                            work[hi].append(m)
                            work[j].append(n)
                            loads[hi] += dm - d
                            loads[j] += d - dm
                            improved = True
                            break
                    if improved:
                        break
                if improved:
                    break
            if not improved:
                break
        cost = max(loads) + 2000 * k
        if best is None or cost < best[0]:
            best = (cost, [sorted(w) for w in work if w])
    return best[1]


def main():
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    durs = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    all_nums = sorted(durs)
    commits = sorted(os.listdir(os.path.join(DATA, "commits")))
    sel = None
    id2num = {}
    try:
        sel = Selector()
        id_index = {i: k for k, i in enumerate(sel.M["ids"])}
        idx2num = {}
        for t in tests:
            k = id_index.get(t["id"])
            if k is not None:
                idx2num[k] = t["number"]
        unknown_nums = [t["number"] for t in tests if t["id"] not in id_index]
    except Exception as e:  # map missing/corrupt: safe fallback
        sys.stderr.write("selector map unavailable: %r\n" % (e,))
        sel = None
    for fn in commits:
        if not fn.endswith(".patch"):
            continue
        cid = fn[:-6]
        try:
            if sel is None:
                chosen = all_nums
            else:
                text = open(os.path.join(DATA, "commits", fn), encoding="utf-8", errors="replace").read()
                res = sel.select(text)
                if res == ALL or ALL in res:
                    chosen = all_nums
                else:
                    mask = set()
                    for s in res:
                        mask |= s
                    chosen = sorted({idx2num[k] for k in mask if k in idx2num} | set(unknown_nums if mask else []))
        except Exception as e:
            sys.stderr.write("error on %s: %r\n" % (cid, e))
            chosen = all_nums
        print(json.dumps({"commit": cid, "workers": partition(chosen, durs)}))


if __name__ == "__main__":
    main()
