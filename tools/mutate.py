"""Evaluation-only mutation harness (never shipped as an answer table).

usage: python mutate.py <sq_copy> <tests.json> <selector_dir> <baseline_fail.json> <seed> <count> <out.jsonl> <kinds,...>
For each mutant: edit the checkout, take `git diff` as the patch, run the real test suite to learn which tests fail,
run selector.py on the patch, and record whether every failing test was selected.
"""
import json
import os
import pickle
import random
import re
import subprocess
import sys

SQ, TESTS, SELDIR, BASE, SEED, COUNT, OUT = sys.argv[1:8]
KINDS = sys.argv[8].split(",") if len(sys.argv) > 8 else None
COUNT = int(COUNT)
rnd = random.Random(int(SEED))
HERE = os.path.dirname(os.path.abspath(__file__))
base_fail = set(json.load(open(BASE)))
T = json.load(open(TESTS))["tests"]
dur = {t["number"]: round(t["seconds"] * 1000) for t in T}

pyfiles = []
for dp, dn, fn in os.walk(os.path.join(SQ, "sqlglot")):
    dn[:] = [d for d in dn if d != "__pycache__"]
    for f in fn:
        if f.endswith(".py"):
            pyfiles.append(os.path.relpath(os.path.join(dp, f), SQ))
pyfiles.sort()

FLIPS = [
    (r"\bis None\b", "is not None"), (r"\bis not None\b", "is None"), (r" == ", " != "), (r" != ", " == "),
    (r" and ", " or "), (r" or ", " and "), (r"\bTrue\b", "False"), (r"\bFalse\b", "True"),
    (r" < ", " <= "), (r" > ", " >= "), (r" \+ 1\b", " + 2"), (r"\bnot ", ""), (r"\bin ", "not in "),
]


def pick_line(kind, path, lines):
    idx = [i for i, l in enumerate(lines) if l.strip() and not l.strip().startswith(("#", '"""', "'''"))]
    rnd.shuffle(idx)
    for i in idx:
        l = lines[i]
        if kind == "flip":
            if l.lstrip().startswith(("import ", "from ", "def ", "class ", "@")):
                continue
            for pat, rep in rnd.sample(FLIPS, len(FLIPS)):
                if re.search(pat, l):
                    return i, [re.sub(pat, rep, l, count=1)]
        elif kind == "const":
            m = re.search(r"(?<![\w.])(\d+)(?![\w.])", l)
            if m and not l.lstrip().startswith(("import ", "from ", "#")):
                return i, [l[: m.start()] + str(int(m.group(1)) + 1) + l[m.end():]]
            m = re.search(r'"([A-Za-z_]{3,})"', l)
            if m and not l.lstrip().startswith(("import ", "from ")):
                return i, [l.replace(m.group(0), '"' + m.group(1) + 'X"', 1)]
        elif kind == "delete":
            if l.rstrip().endswith((":", ",", "(", "[", "{", "\\")) or l.lstrip().startswith(("def ", "class ", "@", "else", "elif", "except", "finally", "return", "import", "from", ")", "]", "}")):
                continue
            if len(l) - len(l.lstrip()) < 8:
                continue
            return i, []
        elif kind == "comment":
            return i, [l, " " * (len(l) - len(l.lstrip())) + "# harmless note"]
        elif kind == "module":
            if len(l) == len(l.lstrip()) and re.match(r"[A-Z_]{4,} = ", l):
                return i, [l + "  # x"] if False else [re.sub(r"(\w+)$", r"\1", l)]
        elif kind == "insert":
            if l.rstrip().endswith(":") or (len(l) - len(l.lstrip())) < 8:
                continue
            ind = " " * (len(l) - len(l.lstrip()))
            return i, [l, ind + "pass"] if False else [l, ind + "_ = 0"]
        elif kind == "syntax":
            if l.rstrip().endswith(":"):
                return i, [l.rstrip()[:-1]]
        elif kind == "ret":
            if l.lstrip().startswith("return ") and (len(l) - len(l.lstrip())) >= 8:
                return i, [l.replace("return ", "return None  # ", 1)]
    return None


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


kinds = KINDS or ["flip", "const", "delete", "comment", "insert", "ret", "syntax"]
results = open(OUT, "a")
done = 0
attempts = 0
while done < COUNT and attempts < COUNT * 20:
    attempts += 1
    kind = kinds[done % len(kinds)]
    path = rnd.choice(pyfiles)
    if "__init__" in path and kind != "syntax":
        continue
    full = os.path.join(SQ, path)
    text = open(full).read()
    lines = text.split("\n")
    pk = pick_line(kind, path, lines)
    if not pk:
        continue
    i, new = pk
    mutated = lines[:i] + new + lines[i + 1:]
    open(full, "w").write("\n".join(mutated))
    patch = sh(["git", "diff", "--no-color"], cwd=SQ).stdout
    # run real tests
    pk_out = os.path.join(SQ, "..", "mut_%s_%d.pkl" % (SEED, done))
    r = sh([sys.executable, os.path.join(HERE, "trace_tests.py"), SQ, TESTS, pk_out, "--notrace"], timeout=900)
    failing = set()
    if os.path.exists(pk_out):
        st = pickle.load(open(pk_out, "rb"))["status"]
        failing = {n for n, s in st.items() if s != "pass"} - base_fail
        os.remove(pk_out)
    sh(["git", "checkout", "--", "."], cwd=SQ)
    sh(["git", "clean", "-fdq", "-e", "__pycache__"], cwd=SQ)
    # run selector
    d = os.path.join(SQ, "..", "mutdata_%s" % SEED)
    os.makedirs(os.path.join(d, "commits"), exist_ok=True)
    open(os.path.join(d, "commits", "m.patch"), "w").write(patch)
    if not os.path.exists(os.path.join(d, "tests.json")):
        os.symlink(os.path.abspath(TESTS), os.path.join(d, "tests.json"))
    env = dict(os.environ, DATA_DIR=d)
    s = sh([sys.executable, os.path.join(SELDIR, "selector.py")], env=env)
    sel = set()
    try:
        for w in json.loads(s.stdout.strip().splitlines()[0])["workers"]:
            sel |= set(w)
    except Exception:
        sel = set(range(len(T)))
    missed = sorted(failing - sel)
    rec = {"kind": kind, "file": path, "line": i + 1, "failing": len(failing), "selected": len(sel),
           "sel_cost_ms": sum(dur[n] for n in sel), "need_cost_ms": sum(dur[n] for n in failing),
           "missed": missed, "missed_ids": [T[n]["id"] for n in missed][:5],
           "patch": patch[:600]}
    results.write(json.dumps(rec) + "\n")
    results.flush()
    done += 1
