"""Record which sqlglot lines execute while each module is imported in a fresh interpreter.

usage: python import_trace.py <sqlglot_checkout> <out.pkl>
out.pkl = {"base": set((file, line)) executed by `import sqlglot`,
           "mods": {module: set((file, line)) executed by `import module` (including `import sqlglot`)}}
"""
import json
import os
import pickle
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

SQ = os.path.abspath(sys.argv[1])
OUT = sys.argv[2]

CODE = r"""
import sys, os, json
sq = sys.argv[1]; mod = sys.argv[2]
SQG = os.path.join(sq, "sqlglot") + os.sep
mon = sys.monitoring
mon.use_tool_id(4, "imptrace")
E = mon.events
hits = set()
ids = {}
def on_start(code, off):
    fn = code.co_filename
    if fn.startswith(SQG):
        ids[code] = os.path.relpath(fn, sq)
        mon.set_local_events(4, code, E.LINE)
    return mon.DISABLE
def on_line(code, line):
    hits.add((ids[code], line))
    return mon.DISABLE
mon.register_callback(4, E.PY_START, on_start)
mon.register_callback(4, E.LINE, on_line)
mon.set_events(4, E.PY_START)
sys.path.insert(0, sq)
try:
    __import__(mod)
except Exception as e:
    sys.stderr.write("fail %s %r\n" % (mod, e))
mon.set_events(4, 0)
print(json.dumps(sorted(hits)))
"""


def modname(path):
    p = path[:-3].replace("/", ".")
    return p[: -len(".__init__")] if p.endswith(".__init__") else p


mods = []
for dp, dn, fn in os.walk(os.path.join(SQ, "sqlglot")):
    dn[:] = [d for d in dn if d != "__pycache__"]
    for f in fn:
        if f.endswith(".py") and f != "__main__.py":
            mods.append(modname(os.path.relpath(os.path.join(dp, f), SQ)))
mods = sorted(set(mods))


def run(m):
    r = subprocess.run([sys.executable, "-c", CODE, SQ, m], capture_output=True, text=True, cwd=SQ, timeout=120)
    try:
        return m, {(a, b) for a, b in json.loads(r.stdout)}
    except Exception:
        return m, set()


with ThreadPoolExecutor(2) as ex:
    res = dict(ex.map(run, mods))
base = res.get("sqlglot", set())
pickle.dump({"base": base, "mods": res}, open(OUT, "wb"))
sys.stderr.write("modules=%d base_lines=%d\n" % (len(res), len(base)))
