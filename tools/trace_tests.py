"""Trace per-test line coverage of sqlglot with sys.monitoring (no coverage.py / pytest needed).

usage: python trace_tests.py <sqlglot_checkout> <tests.json> <out.pkl> [--only substr] [--ids file]
Runs every test in tests.json through unittest, recording for each test:
  - lines executed in sqlglot/ (runtime context)
  - files of the Dialect classes (and their parser/generator/tokenizer) instantiated
  - fixture files opened
  - pass/fail status in this environment
Lines executed while importing modules (before any test) go in the IMPORT set.
"""
import io
import json
import os
import pickle
import sys
import time
import types
import unittest

SQ = os.path.abspath(sys.argv[1])
TESTS = json.load(open(sys.argv[2]))["tests"]
OUT = sys.argv[3]
ONLY = None
IDS = None
if "--only" in sys.argv:
    ONLY = sys.argv[sys.argv.index("--only") + 1]
if "--ids" in sys.argv:
    IDS = set(json.load(open(sys.argv[sys.argv.index("--ids") + 1])))

os.chdir(SQ)
sys.path.insert(0, SQ)


# ---- stubs for packages that are not installable here --------------------------------
class _Boom(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def f(*a, **k):
            raise RuntimeError("stubbed module %s.%s" % (self.__name__, name))

        return f


try:
    import pytz  # noqa
except ImportError:
    import datetime as _dt
    import zoneinfo as _zi

    _p = types.ModuleType("pytz")
    _p.__version__ = "2025.2"
    _p.utc = _dt.timezone.utc
    _p.UTC = _dt.timezone.utc
    _p.timezone = lambda n: _zi.ZoneInfo(n)
    _p.common_timezones = sorted(_zi.available_timezones())
    _p.all_timezones = _p.common_timezones
    _p.exceptions = types.SimpleNamespace(UnknownTimeZoneError=Exception, AmbiguousTimeError=Exception, NonExistentTimeError=Exception)
    sys.modules["pytz"] = _p
try:
    import duckdb  # noqa
except ImportError:
    from unittest import mock as _mock

    _d = _mock.MagicMock(name="duckdb")
    _d.__version__ = "0.0"
    sys.modules["duckdb"] = _d

# ---- run process pools in-process so their work is traced (and round-trips through pickle like spawn) ----
import concurrent.futures as _cf
import concurrent.futures.process as _cfp
import pickle as _pickle


class _SyncPool:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        self._init = (initializer, initargs)
        self._done = False

    def _ensure(self):
        if not self._done:
            self._done = True
            if self._init[0]:
                self._init[0](*self._init[1])

    def submit(self, fn, *a, **k):
        self._ensure()
        fut = _cf.Future()
        try:
            # emulate spawn: arguments and results cross a pickle boundary (exercises Expr.__reduce__ -> serde)
            fn2, a2, k2 = _pickle.loads(_pickle.dumps((fn, a, k)))
            res = _pickle.loads(_pickle.dumps(fn2(*a2, **k2)))
            fut.set_result(res)
        except BaseException as e:  # noqa
            fut.set_exception(e)
        return fut

    def map(self, fn, *its, **kw):
        return iter([self.submit(fn, *a).result() for a in zip(*its)])

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_cf.ProcessPoolExecutor = _SyncPool
_cfp.ProcessPoolExecutor = _SyncPool

SQG = os.path.join(SQ, "sqlglot") + os.sep
FIXT = os.path.join(SQ, "tests", "fixtures") + os.sep

mon = sys.monitoring
TOOL = 3
mon.use_tool_id(TOOL, "tracer")
E = mon.events

current = None  # set receiving line hits
file_ids = {}
file_names = []


def fid(fn):
    i = file_ids.get(fn)
    if i is None:
        i = file_ids[fn] = len(file_names)
        file_names.append(fn)
    return i


code_fid = {}


def on_start(code, off):
    fn = code.co_filename
    if fn.startswith(SQG):
        code_fid[code] = fid(os.path.relpath(fn, SQ))
        mon.set_local_events(TOOL, code, E.LINE)
    return mon.DISABLE


def on_line(code, line):
    if current is not None:
        current.add((code_fid[code], line))
    return mon.DISABLE


NOTRACE = "--notrace" in sys.argv
if not NOTRACE:
    mon.register_callback(TOOL, E.PY_START, on_start)
    mon.register_callback(TOOL, E.LINE, on_line)
    mon.set_events(TOOL, E.PY_START)

# ---- audit hook for fixture reads ------------------------------------------------------
cur_fix = None


def audit(event, args):
    if event == "open" and cur_fix is not None:
        p = args[0]
        if isinstance(p, (str, bytes, os.PathLike)):
            try:
                p = os.path.abspath(os.fsdecode(p))
            except Exception:
                return
            if p.startswith(FIXT):
                cur_fix.add(os.path.relpath(p, SQ))


sys.addaudithook(audit)

# ---- import phase -----------------------------------------------------------------------
IMPORT = set()
IMPORT_FIX = set()
current = IMPORT
cur_fix = IMPORT_FIX
import importlib
import pkgutil

import sqlglot

for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
    if info.name.endswith("__main__"):
        continue
    try:
        importlib.import_module(info.name)
    except Exception as e:
        sys.stderr.write("import fail %s %r\n" % (info.name, e))

touched = None  # set of instantiated types for the running test


def _wrap(cls):
    orig = cls.__init__

    def init(self, *a, **k):
        t_ = touched
        if t_ is not None:
            t_.add(type(self))
        return orig(self, *a, **k)

    init.__wrapped__ = orig
    cls.__init__ = init


import sqlglot.dialects.dialect as D
import sqlglot.expressions.core as _core
import sqlglot.generator as _gen
import sqlglot.parser as _par
import sqlglot.tokens as _tok

_wrap(_core.Expr)
_wrap(D.Dialect)
_wrap(_par.Parser)
_wrap(_gen.Generator)
_wrap(_tok.Tokenizer)


def closure(types_):
    out = set()
    for ty in types_:
        for c in ty.__mro__:
            m = getattr(c, "__module__", "")
            if m.startswith("sqlglot"):
                out.add("%s:%s" % (m, c.__qualname__))
    return out


# ---- run tests ---------------------------------------------------------------------------
by_class = {}
order = []
for t in TESTS:
    if ONLY and ONLY not in t["id"]:
        continue
    if IDS is not None and t["id"] not in IDS:
        continue
    f, cls, meth = t["id"].split("::")
    key = (f, cls)
    if key not in by_class:
        by_class[key] = []
        order.append(key)
    by_class[key].append((t["number"], meth))

result_lines = {}
result_touched = {}
result_fix = {}
status = {}


class R(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.cur = None
        self.between = set()
        self.between_fix = set()
        self.between_touched = set()

    def startTest(self, test):
        global current, cur_fix, touched
        super().startTest(test)
        if not NOTRACE:
            mon.restart_events()
        n = self.numbers[test._testMethodName]
        self.cur = n
        result_lines[n] = set()
        result_touched[n] = set()
        result_fix[n] = set()
        status[n] = "pass"
        current = result_lines[n]
        cur_fix = result_fix[n]
        touched = result_touched[n]

    def stopTest(self, test):
        global current, cur_fix, touched
        super().stopTest(test)
        current = self.between
        cur_fix = self.between_fix
        touched = self.between_touched

    def addError(self, test, err):
        super().addError(test, err)
        if self.cur in status:
            status[self.cur] = "error:" + repr(err[1])[:200]

    def addFailure(self, test, err):
        super().addFailure(test, err)
        status[self.cur] = "fail:" + repr(err[1])[:200]

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        status[self.cur] = "skip"


t0 = time.time()
for key in order:
    f, cls, _ = key[0], key[1], None
    modname = f[:-3].replace("/", ".")
    try:
        mod = importlib.import_module(modname)
        klass = getattr(mod, cls)
    except Exception as e:
        for n, m in by_class[key]:
            status[n] = "importerror:" + repr(e)[:200]
            result_lines[n] = set()
            result_touched[n] = set()
            result_fix[n] = set()
        sys.stderr.write("cannot import %s: %r\n" % (modname, e))
        continue
    r = R()
    r.numbers = {m: n for n, m in by_class[key]}
    suite = unittest.TestSuite()
    for n, m in by_class[key]:
        suite.addTest(klass(m))
    # between-test hits (setUpClass etc.) are recorded into r.between
    current = r.between
    cur_fix = r.between_fix
    touched = r.between_touched
    if not NOTRACE:
        mon.restart_events()
    ts = time.time()
    stdout, stderr = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = io.StringIO()
    try:
        suite.run(r)
    finally:
        sys.stdout, sys.stderr = stdout, stderr
    for n, m in by_class[key]:
        if n not in result_lines:
            result_lines[n] = set()
            result_touched[n] = set()
            result_fix[n] = set()
            status[n] = status.get(n, "notrun")
        result_lines[n] |= r.between
        result_fix[n] |= r.between_fix
        result_touched[n] |= r.between_touched
    sys.stderr.write("%-60s %4d tests %6.1fs\n" % (f + "::" + cls, len(by_class[key]), time.time() - ts))
    sys.stderr.flush()
    if not NOTRACE:
        mon.restart_events()
current = None
cur_fix = None
touched = None

pickle.dump(
    {
        "files": file_names,
        "import": IMPORT,
        "import_fix": IMPORT_FIX,
        "lines": result_lines,
        "touched": {n: closure(v) for n, v in result_touched.items()},
        "fixtures": result_fix,
        "status": status,
    },
    open(OUT, "wb"),
)
sys.stderr.write("done in %.0fs\n" % (time.time() - t0))
