# Design and compliance: RWX – Blast Radius

Pinned sqlglot commit: `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`.

## What the selector does
`selector.py` (pure standard library, ~0.15 s CPU for 30 commits) reads each patch and picks the tests that can see the change.

1. **Coverage map (built offline from the pinned commit).** `tools/trace_tests.py` runs the whole test suite once under `sys.monitoring` and records, per test: the sqlglot source lines it executes, which sqlglot classes it instantiates (with their MRO), and which fixture files it opens. Process pools are run in-process while tracing so the work done in worker processes is also seen. `tools/build_map.py` turns that into `selector_map.bin`: for every source line of every file, the set of tests that exercise it.
2. **Statement-level units.** A changed line is expanded to its whole statement (or compound-statement header), so a multi-line edit is attributed correctly. Functions use the union of the tests that run any line of the function.
3. **Import-time code.** Lines that execute while the package is imported (module constants, class bodies, decorators) are not attributed to a single test. They are mapped to the tests that use the module / class, and to the tests that use modules importing it.
3b. **Class-level constants.** For a changed class-body assignment `NAME = ...` the tests are narrowed to those that instantiate that class (or a subclass) **and** execute a line that reads `NAME`. This is applied only when every read of `NAME` in the code base is inside a function that runs at test time; any read at import time (module/class bodies, metaclass hooks, `{**Base.NAME}`) or by string (`getattr`) keeps the wide rule from 3.
4. **Insertions.** A pure insertion is anchored to the tests of its neighbouring statements, the enclosing function, and (for module level or class-body inserts) the module or class users.
5. **Inert changes.** The patched file is rebuilt in memory and parsed; if its syntax tree equals the original after normalising comparisons that mean the same thing (`x is None` / `x == None`, `x is Enum.MEMBER` / `x == Enum.MEMBER`), nothing that executes changed and nothing is selected (cost 0). Comments and formatting never appear in a syntax tree. Otherwise, a change that only adds, removes or edits blank lines or `#` comments and cannot alter tokenisation (not inside a multi-line string, not after a line continuation, not an encoding cookie) also selects nothing.
6. **Syntax / import breakage.** The patched file is rebuilt in memory from the pinned source and compiled. A file that no longer compiles selects every test that imports it (all tests for core modules).
7. **Fixtures.** A changed `tests/fixtures/...` file selects the tests that opened it while tracing (plus tests that reference it by name).
8. **Test files.** A changed test file selects the tests of that file (and test modules that import it).
9. **Anything unknown** (unparsable patch, unknown Python file, build or config files, `conftest`, `pyproject`) selects all tests. Any exception for a commit also selects all tests. The map missing also selects all tests. A valid line is always printed.
10. **Worker split.** For k = 1..8 workers the tests are spread by longest-processing-time and a local search, and the k with the smallest `makespan + 2 s * k` is chosen.

## Evaluation
* Sample: 116.5 s, 0 misses (starter 1614 s; the best possible with perfect knowledge of failing tests is 94.4 s).
* `tools/mutate.py` injects operator flips, constant edits, deletions, inserted statements, renamed definitions, comment insertions and syntax errors into the pinned code, runs the real tests to see which fail, and checks that the selector chose every failing test. Misses found this way led to the rule that a patched `sqlglot/` module that no longer compiles selects every test.

## Compliance statement
* There is **no table keyed by a patch** (hash, content, or kind of edit) holding precomputed answers. The map is indexed by *source line* of the pinned commit and contains only coverage, class-usage, fixture-access and import information derived from the code and tests themselves.
* `src.zip` is a copy of the pinned commit's `sqlglot/` and `tests/` Python sources, used only to rebuild a patched file in memory and compile it. It is the same content the pinned commit contains.
* The mutation harness (`tools/mutate.py`) is evaluation only. Its output is not shipped and is not used at run time.
* Build for the image needs no network: everything needed is committed (map ~0.4 MB, sources ~1.3 MB).

## Rebuilding the map
```
git clone https://github.com/tobymao/sqlglot && git -C sqlglot checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa
python tools/trace_tests.py sqlglot sample/tests.json trace.pkl      # needs pandas, numpy (duckdb/pytz are stubbed if absent)
python tools/build_map.py sqlglot sample/tests.json trace.pkl .       # writes selector_map.bin and src.zip
```

## Known limits
* Tests that need `duckdb` were traced with a stub. Their sqlglot-side lines are still recorded, but anything only reachable after a real duckdb result is unseen; those tests still run for every line they were seen to execute.
* Coverage can under-approximate behaviour that depends on data rather than on lines. Changes in the module-level of core modules therefore select widely on purpose.
