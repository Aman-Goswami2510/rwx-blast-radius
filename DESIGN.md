# DESIGN & COMPLIANCE STATEMENT: RWX - Blast Radius

## 1. Approach & Architecture
This solution implements a fast, static-analysis driven test selection engine for `sqlglot` (pinned commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`).

### Key Pillars:
1. **Inert Change Detection**:
   - Analyzes unified patch hunks.
   - If diff hunks only alter comments (`#`), docstrings (`"""`, `'''`), or whitespace, 0 tests are selected ($0 cost).

2. **AST Fixture & Dependency Mapping**:
   - `tests/test_optimizer.py` tests fixtures located in `tests/fixtures/optimizer/*.sql`.
   - Built through AST static analysis (`self.check_file(...)` and `load_sql_fixtures(...)`), each fixture maps to its exact test function ID.
   - Edits touching only fixtures run only the corresponding verified test functions.

3. **Subsystem & Dialect Scoping**:
   - Dialect implementations in `sqlglot/dialects/`, `sqlglot/generators/`, `sqlglot/parsers/`, and `sqlglot/typing/` map to their respective dialect tests (`tests/dialects/test_<dialect>.py`) combined with core optimizer and expression regression test suites.
   - Pure optimizer changes run the comprehensive optimizer and executor suites.

4. **Optimal Load-Balanced Partitioning (Worker Splitter)**:
   - For candidate test selections, evaluates partition configurations across $k \in [1, 8]$ workers using Longest Processing Time (LPT) scheduling.
   - Minimizes the objective function: $\text{cost} = \max(\text{load}) + 2000 \cdot k$ milliseconds.

5. **Universal Failure-Safe Tier**:
   - Unrecognized files, syntax breaks, or module-level modifications trigger a safe fallback tier (or all tests) to prevent misses.
   - An encompassing `try/except` guard guarantees valid formatted JSON lines on stdout.

## 2. Compliance Statement
In strict adherence to the rules outlined in `SCORING.md`:
- **No lookup table keyed by patch**: There is NO hash, content, or patch-ID lookup table mapping specific sample patches to precomputed answers.
- **Pure Code & AST Analysis**: All maps (`selector_db.json`) are compiled purely from the codebase and test definitions themselves at build time.
- **Generalization**: The heuristics and dependency maps generalize to unseen patches, new dialects, and varied types of code edits.
