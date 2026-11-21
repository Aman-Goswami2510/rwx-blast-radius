"""Build selector_map.bin from a per-test trace (trace_tests.py output) and the pinned sqlglot checkout.

usage: python build_map.py <sqlglot_checkout> <tests.json> <trace.pkl> <out_dir>
Produces out_dir/selector_map.bin (zlib+marshal) and out_dir/src.zip (old sources of sqlglot/ and tests/).
Everything is derived from the pinned commit's code, tests and coverage; nothing is keyed by a patch.
"""
import ast
import io
import json
import marshal
import os
import pickle
import re
import sys
import tokenize
import zipfile
import zlib
from collections import defaultdict

SQ, TESTS_JSON, TRACE, OUT = sys.argv[1:5]
os.makedirs(OUT, exist_ok=True)
T = json.load(open(TESTS_JSON))["tests"]
ids = [t["id"] for t in T]
NT = len(ids)
d = pickle.load(open(TRACE, "rb"))
files = d["files"]
status = d["status"]
ALL = (1 << NT) - 1

# ---------------------------------------------------------------- per-test groups
test_file = [t["file"] for t in T]
group_bits = defaultdict(int)
for t in T:
    group_bits[t["file"]] |= 1 << t["number"]
incomplete = defaultdict(int)  # file -> bits of tests whose trace may be partial
for t in T:
    if status.get(t["number"], "notrun") != "pass":
        incomplete[t["file"]] |= 1 << t["number"]

# ---------------------------------------------------------------- hits (runtime) per file/line
hits = defaultdict(lambda: defaultdict(int))
for n, ls in d["lines"].items():
    b = 1 << n
    for fi, line in ls:
        hits[files[fi]][line] |= b
imp_lines = defaultdict(set)
for fi, line in d["import"]:
    imp_lines[files[fi]].add(line)

# incomplete tests inherit the coverage of everything else in their test file
for path, lm in hits.items():
    for line, m in lm.items():
        for f, gb in group_bits.items():
            if incomplete[f] and (m & gb):
                m |= incomplete[f]
        lm[line] = m

# class closure -> tests
classT = defaultdict(int)
for n, keys in d["touched"].items():
    b = 1 << n
    for k in keys:
        classT[k] |= b
for k, m in list(classT.items()):
    for f, gb in group_bits.items():
        if incomplete[f] and (m & gb):
            m |= incomplete[f]
    classT[k] = m


def modname(path):
    p = path[:-3].replace("/", ".")
    return p[: -len(".__init__")] if p.endswith(".__init__") else p


# ---------------------------------------------------------------- source files
py_files = []
for root in ("sqlglot", "tests"):
    for dp, dn, fn in os.walk(os.path.join(SQ, root)):
        dn[:] = [x for x in dn if x != "__pycache__"]
        for f in fn:
            if f.endswith(".py"):
                py_files.append(os.path.relpath(os.path.join(dp, f), SQ))
py_files.sort()
src = {p: open(os.path.join(SQ, p), encoding="utf-8").read() for p in py_files}

with zipfile.ZipFile(os.path.join(OUT, "src.zip"), "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for p in py_files:
        zi = zipfile.ZipInfo(p, date_time=(2020, 1, 1, 0, 0, 0))
        zi.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(zi, src[p])

# ---------------------------------------------------------------- import graph (sqlglot + tests)
mod2path = {modname(p): p for p in py_files}


def imports_of(path):
    out = set()
    try:
        tree = ast.parse(src[path])
    except SyntaxError:
        return out
    pkg = modname(path).split(".")
    if not path.endswith("__init__.py"):
        pkg = pkg[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                for i in range(1, len(parts) + 1):
                    m = ".".join(parts[:i])
                    if m in mod2path:
                        out.add(m)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                up = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                base = ".".join(up + ([node.module] if node.module else []))
            parts = base.split(".")
            for i in range(1, len(parts) + 1):
                m = ".".join(parts[:i])
                if m in mod2path:
                    out.add(m)
            for a in node.names:
                m = base + "." + a.name
                if m in mod2path:
                    out.add(m)
    return out


imp_graph = {modname(p): imports_of(p) for p in py_files}
importers = defaultdict(set)
for m, outs in imp_graph.items():
    for o in outs:
        importers[o].add(m)

FAMILY = re.compile(r"^sqlglot/(dialects|parsers|generators|typing)/(?!__init__|base|dialect)")


def is_family(path):
    return bool(FAMILY.match(path))


def run_mask(path):
    m = 0
    for v in hits.get(path, {}).values():
        m |= v
    return m


_touch_cache = {}


def touchF(path):
    if path in _touch_cache:
        return _touch_cache[path]
    mod = modname(path)
    m = run_mask(path)
    for k, v in classT.items():
        if k.split(":")[0] == mod:
            m |= v
    _touch_cache[path] = m
    return m


_imp_cache = {}


def importerT(path):
    if path in _imp_cache:
        return _imp_cache[path]
    m = 0
    for g in importers.get(modname(path), ()):
        gp = mod2path[g]
        if gp.endswith("__init__.py"):
            continue
        if is_family(path) and not is_family(gp):
            continue
        m |= touchF(gp)
    _imp_cache[path] = m
    return m



# ---------------------------------------------------------------- attribute reference index
# For a class-body constant `N = ...` we can narrow the tests to those that actually read N, but only when every
# read of N happens inside a function that runs at test time (never in module/class bodies or import-time code).
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
ref_lines = defaultdict(list)  # name -> [(path, line)] runtime reads (functions)
ref_unsafe = set()  # names read at import time or by string
test_ref_mask = defaultdict(int)  # name -> tests of test files that mention the name


def _scan_refs(path, tree):
    in_sq = path.startswith("sqlglot/")
    imp = imp_lines.get(path, set())

    def walk(node, in_func, in_class_body):
        for child in ast.iter_child_nodes(node):
            f = in_func or isinstance(child, _FUNCS)
            name = None
            if isinstance(child, ast.Name):
                name = child.id
                store = isinstance(child.ctx, ast.Store)
            elif isinstance(child, ast.Attribute):
                name = child.attr
                store = isinstance(child.ctx, ast.Store)
            elif isinstance(child, ast.Constant) and isinstance(child.value, str) and child.value.isidentifier():
                if in_sq:
                    ref_unsafe.add(child.value)  # getattr(x, "NAME") style access
            if name is not None:
                if not in_sq:
                    test_ref_mask[name] |= by_file_ref.get(path, 0)
                elif store and isinstance(child, ast.Name) and not in_func:
                    pass  # class/module-level definition of the name itself
                elif in_func:
                    if child.lineno in imp:
                        ref_unsafe.add(name)  # executed while the package is imported (metaclass, decorators)
                    ref_lines[name].append((path, child.lineno))
                else:
                    ref_unsafe.add(name)  # read at import time (e.g. {**Base.NAME, ...})
            walk(child, f, isinstance(child, ast.ClassDef) or (in_class_body and not f))

    walk(tree, False, False)


by_file_ref = defaultdict(int)
for _t in T:
    by_file_ref[_t["file"]] |= 1 << _t["number"]
for _p in py_files:
    try:
        _scan_refs(_p, ast.parse(src[_p]))
    except SyntaxError:
        pass


def attr_refmask(name):
    if name in ref_unsafe:
        return None
    m = test_ref_mask.get(name, 0)
    for pth, ln in ref_lines.get(name, ()):
        m |= hits.get(pth, {}).get(ln, 0)
    return m


def target_names(node):
    if isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) for t in node.targets):
        return [t.id for t in node.targets]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
        return [node.target.id]
    return None

# ---------------------------------------------------------------- sig table
sig_ids = {0: 0}
sigs = [[]]


def sid(mask):
    s = sig_ids.get(mask)
    if s is None:
        s = sig_ids[mask] = len(sigs)
        sigs.append([i for i in range(NT) if mask >> i & 1])
    return s


COMPOUND = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try,
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Match,
)
if hasattr(ast, "TryStar"):
    COMPOUND = COMPOUND + (ast.TryStar,)


def node_lo(n):
    decs = getattr(n, "decorator_list", None)
    return min([n.lineno] + [x.lineno for x in decs]) if decs else n.lineno


# ---------------------------------------------------------------- analysis of sqlglot sources
def code_lines(text, n):
    """lines that contain a real token (or lie inside a multi-line string)."""
    code = [False] * (n + 2)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                            tokenize.DEDENT, tokenize.ENDMARKER):
                continue
            for l in range(tok.start[0], tok.end[0] + 1):
                if l <= n:
                    code[l] = True
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return [True] * (n + 2)
    return code


def instring_gaps(text, n):
    """gap g (between line g and g+1) lies inside a multi-line token."""
    gaps = [False] * (n + 2)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", -1)) or tok.end[0] > tok.start[0]:
                if tok.type in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT):
                    continue
                if tok.end[0] > tok.start[0] and tok.type != tokenize.OP:
                    for g in range(tok.start[0], tok.end[0]):
                        if g <= n:
                            gaps[g] = True
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return [True] * (n + 2)
    return gaps


def analyze_sqlglot(path):
    text = src[path]
    lines = text.split("\n")
    N = len(text.splitlines())
    tree = ast.parse(text)
    H = hits.get(path, {})
    IMP = imp_lines.get(path, set())
    mod = modname(path)
    tF = touchF(path)
    iT = importerT(path)

    def mask_over(lo, hi):
        m = 0
        for l in range(lo, hi + 1):
            m |= H.get(l, 0)
        return m

    def imp_over(lo, hi):
        return any(l in IMP for l in range(lo, hi + 1))

    unit_mask = [None] * (N + 2)  # final mask per line (None = not code in a unit)
    unit_scope = [None] * (N + 2)  # ("mod"|"cls"|"fn", classkey)
    stmt_chain = {}

    def class_key(chain):
        names = []
        for n_ in chain:
            if isinstance(n_, ast.ClassDef):
                names.append(n_.name)
            elif isinstance(n_, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return None
        return "%s:%s" % (mod, ".".join(names)) if names else None

    def finalize(m, imp, lo, hi, chain, node, kind):
        # chain: enclosing nodes (outermost first), excluding node
        scope_cls = None
        in_fn = any(isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)) for c in chain)
        enc_cls = [c for c in chain if isinstance(c, ast.ClassDef)]
        if imp or kind == "class":
            if kind == "class":
                key = class_key(chain + [node])
                cspan = mask_over(node_lo(node), node.end_lineno)
                m |= classT.get(key, 0) | cspan
            elif enc_cls and not in_fn:
                key = class_key(chain)
                c_ = enc_cls[-1]
                narrowed = None
                if kind == "simple" and classT.get(key, 0):
                    names = target_names(node)
                    if names:
                        r = 0
                        for nm in names:
                            rm = attr_refmask(nm)
                            if rm is None:
                                r = None
                                break
                            r |= rm
                        if r is not None:
                            narrowed = classT[key] & r
                if narrowed is not None:
                    m |= narrowed
                else:
                    m |= classT.get(key, 0) | mask_over(node_lo(c_), c_.end_lineno)
            else:
                m |= tF | iT
                if kind == "func":
                    pass
        return m

    def visit(stmts, chain):
        for node in stmts:
            if isinstance(node, COMPOUND):
                lo = node_lo(node)
                bodies = []
                if isinstance(node, ast.Match):
                    first = node.cases[0].pattern.lineno if node.cases else node.end_lineno
                    hi = first - 1
                    for c in node.cases:
                        bodies.append(c.body)
                else:
                    hi = node.body[0].lineno - 1
                    bodies.append(node.body)
                hi = max(hi, lo)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    kind = "func"
                    m = mask_over(lo, node.end_lineno)
                elif isinstance(node, ast.ClassDef):
                    kind = "class"
                    m = mask_over(lo, hi)
                else:
                    kind = "hdr"
                    m = mask_over(lo, hi)
                imp = imp_over(lo, hi)
                fm = finalize(m, imp, lo, hi, chain, node, kind)
                for l in range(lo, hi + 1):
                    unit_mask[l] = fm
                    unit_scope[l] = (kind, chain, node)
                whole = mask_over(lo, node.end_lineno)
                # handlers / else / finally keywords
                subs = []
                if isinstance(node, ast.Try) or (hasattr(ast, "TryStar") and isinstance(node, ast.TryStar)):
                    for h in node.handlers:
                        hlo, hhi = h.lineno, max(h.lineno, h.body[0].lineno - 1)
                        hm = finalize(whole, imp_over(hlo, hhi), hlo, hhi, chain, node, "hdr")
                        for l in range(hlo, hhi + 1):
                            unit_mask[l] = hm
                            unit_scope[l] = ("hdr", chain, node)
                        bodies.append(h.body)
                    bodies.append(node.orelse)
                    bodies.append(node.finalbody)
                elif not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Match)):
                    bodies.append(getattr(node, 'orelse', []))
                else:
                    pass
                newchain = chain + [node]
                for b in bodies:
                    visit(b, newchain)
                # keyword lines (else:, finally:) = any unclaimed line in the compound's span
                wm = finalize(whole, imp_over(lo, node.end_lineno), lo, node.end_lineno, chain, node, "hdr") if not isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else None
                for l in range(lo, node.end_lineno + 1):
                    if unit_mask[l] is None and wm is not None:
                        unit_mask[l] = wm
                        unit_scope[l] = ("hdr", chain, node)
            else:
                lo, hi = node.lineno, node.end_lineno
                m = mask_over(lo, hi)
                imp = imp_over(lo, hi)
                fm = finalize(m, imp, lo, hi, chain, node, "simple")
                for l in range(lo, hi + 1):
                    unit_mask[l] = fm
                    unit_scope[l] = ("simple", chain, node)

    visit(tree.body, [])
    code = code_lines(text, N)
    modmask = tF | iT
    ef = [0] * (N + 2)
    for l in range(1, N + 1):
        if not code[l]:
            ef[l] = -1
        elif unit_mask[l] is None:
            ef[l] = sid(modmask)  # code line outside any statement (rare): be conservative
        else:
            ef[l] = sid(unit_mask[l])
    # insertion gaps
    inside = instring_gaps(text, N)
    pq = [0] * (N + 2)
    clsx = [0] * (N + 2)
    clsind = [0] * (N + 2)
    code_idx = [l for l in range(1, N + 1) if code[l]]
    import bisect

    for g in range(0, N + 1):
        i = bisect.bisect_right(code_idx, g)
        p = code_idx[i - 1] if i > 0 else None
        q = code_idx[i] if i < len(code_idx) else None
        m = 0
        for x in (p, q):
            if x is not None and unit_mask[x] is not None:
                m |= unit_mask[x]
        # enclosing scopes of the gap
        ref = q if q is not None else p
        enc = None
        if ref is not None and unit_scope[ref] is not None:
            kind, chain, node = unit_scope[ref]
            # scopes that contain both g and g+1
            cand = chain + ([node] if isinstance(node, COMPOUND) else [])
            cand = [c for c in cand if node_lo(c) <= g and c.end_lineno >= g + 1]
            for c in cand:
                if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    m |= mask_over(node_lo(c), c.end_lineno)
            ccls = [c for c in cand if isinstance(c, ast.ClassDef)]
            if ccls and not any(isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)) for c in cand):
                c_ = ccls[-1]
                key = class_key(cand[: cand.index(c_) + 1])
                clsx[g] = sid(classT.get(key, 0) | mask_over(node_lo(c_), c_.end_lineno))
                clsind[g] = c_.body[0].col_offset  # class body indent
        pq[g] = sid(m)
    return {
        "n": N,
        "ef": ef[1 : N + 1],
        "pq": pq[0 : N + 1],
        "cx": clsx[0 : N + 1],
        "ci": clsind[0 : N + 1],
        "ins": [1 if inside[g] else 0 for g in range(0, N + 1)],
        "mod": sid(modmask),
        "tf": sid(tF),
        "imp": sid(tF | iT),
    }


# ---------------------------------------------------------------- analysis of test sources
by_class = defaultdict(int)
by_method = {}
by_file = defaultdict(int)
for t in T:
    f, cls, meth = t["id"].split("::")
    by_class[(f, cls)] |= 1 << t["number"]
    by_method[(f, cls, meth)] = 1 << t["number"]
    by_file[f] |= 1 << t["number"]


def test_importers_closure(path):
    """tests in every test file that (transitively) imports this test module."""
    seen = set()
    stack = [modname(path)]
    while stack:
        m = stack.pop()
        for g in importers.get(m, ()):
            if g not in seen:
                seen.add(g)
                stack.append(g)
    mask = 0
    for g in seen:
        mask |= by_file.get(mod2path[g], 0)
    return mask


def analyze_tests(path):
    text = src[path]
    N = len(text.splitlines())
    tree = ast.parse(text)
    fm = by_file.get(path, 0)
    imp_all = fm | test_importers_closure(path)
    ef = [sid(imp_all)] * (N + 1)
    ef[0] = 0
    code = code_lines(text, N)
    inside = instring_gaps(text, N)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            cm = by_class.get((path, node.name), 0)
            cmask = cm | test_importers_closure(path) if cm or True else cm
            for l in range(node_lo(node), node.end_lineno + 1):
                ef[l] = sid(cm | (imp_all if not cm else 0)) if cm else sid(imp_all)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    tm = by_method.get((path, node.name, sub.name))
                    if tm:
                        for l in range(node_lo(sub), sub.end_lineno + 1):
                            ef[l] = sid(tm)
                    else:
                        # helper method or non-test method: affects the class (and subclasses in importers)
                        for l in range(node_lo(sub), sub.end_lineno + 1):
                            ef[l] = sid(cm | imp_all)
    for l in range(1, N + 1):
        if not code[l]:
            ef[l] = -1
    return {
        "n": N,
        "ef": ef[1 : N + 1],
        "pq": [sid(imp_all)] * (N + 1),
        "cx": [0] * (N + 1),
        "ci": [0] * (N + 1),
        "ins": [1 if inside[g] else 0 for g in range(0, N + 1)],
        "mod": sid(imp_all),
        "tf": sid(fm),
        "imp": sid(imp_all),
        "test": 1,
    }


F = {}
for p in py_files:
    try:
        F[p] = analyze_sqlglot(p) if p.startswith("sqlglot/") else analyze_tests(p)
    except SyntaxError as e:
        sys.stderr.write("syntax error analysing %s: %s\n" % (p, e))

# ---------------------------------------------------------------- fixtures
fix_tests = defaultdict(int)
for n, fs in d["fixtures"].items():
    for f in fs:
        fix_tests[f] |= 1 << n
test_src = {p: src[p] for p in py_files if p.startswith("tests/")}
fixture_files = []
for dp, dn, fn in os.walk(os.path.join(SQ, "tests", "fixtures")):
    for f in fn:
        fixture_files.append(os.path.relpath(os.path.join(dp, f), SQ))
imp_fix = set(d["import_fix"])
fixture_map = {}
for fp in sorted(fixture_files):
    m = fix_tests.get(fp, 0)
    base = os.path.basename(fp)
    rel = fp[len("tests/fixtures/") :]
    # test modules that mention the fixture by name or by relative path
    ref = 0
    for tp, text in test_src.items():
        if base in text or rel in text:
            ref |= by_file.get(tp, 0)
    if fp in imp_fix or not m:
        m |= ref
    # a fixture inside a directory that is globbed / listed: include the directory's readers
    if not m:
        dirname = os.path.dirname(fp)
        for k, v in fix_tests.items():
            if os.path.dirname(k) == dirname:
                m |= v
    fixture_map[fp] = sid(m)

# ---------------------------------------------------------------- misc masks
misc = {
    "all": sid(ALL),
    "dialect_imports": sid(by_file.get("tests/test_dialect_imports.py", 0) | by_file.get("tests/test_dialect_entry_points.py", 0)),
}
test_file_masks = {f: sid(m) for f, m in by_file.items()}

obj = {
    "v": 1,
    "ids": ids,
    "sigs": sigs,
    "files": F,
    "fixtures": fixture_map,
    "misc": misc,
    "test_files": test_file_masks,
}
blob = zlib.compress(marshal.dumps(obj), 9)
open(os.path.join(OUT, "selector_map.bin"), "wb").write(blob)
sys.stderr.write(
    "sigs=%d files=%d map=%.0f KB src.zip=%.0f KB\n"
    % (len(sigs), len(F), len(blob) / 1024, os.path.getsize(os.path.join(OUT, "src.zip")) / 1024)
)

if os.environ.get("DBG"):
    for nm in os.environ["DBG"].split(","):
        rm = attr_refmask(nm)
        sys.stderr.write("%s unsafe=%s refs=%s mask_tests=%s\n" % (nm, nm in ref_unsafe, ref_lines.get(nm, [])[:6], None if rm is None else bin(rm).count("1")))
    k = [k for k in classT if k.endswith(":TSQL")]
    sys.stderr.write("TSQL keys %s %s\n" % (k, [bin(classT[x]).count("1") for x in k]))
