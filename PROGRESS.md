# PROGRESS: RWX – Blast Radius Track

## Current State Summary
- **Baseline Starter Score**: 1210.470 s (or ~1614 s on remote server depending on worker configuration), 0 misses.
- **Current Milestone (Step A/B Safe Baseline)**: **392.321 s**, **0 misses** on all 30 sample commits!
- **Format Verification**: Validated strictly against `score.py` rules (JSON shape, valid test IDs, <= 8 workers, exact match).
- **Execution Speed**: Pure Python stdlib selector executes all 30 sample commits in **~0.05 seconds** CPU time.

## Components Implemented & Verified
1. **Fixture Dependency Map**:
   - Analyzed `TestOptimizer` via AST (`check_file` and `load_sql_fixtures`).
   - Mapped all optimizer fixtures to specific test methods (100% precision on sample fixture commits: 2vdj, 6vvp, egys, f4bn, gbhk, mthh, nwnx, st8f).
2. **Inert Change Detection**:
   - Safe parsing of comments, whitespace, and docstrings.
   - Tested on comments/whitespace commits (9u5k, bap8, f4xa) -> 0 tests selected, 0 penalty, 0 cost.
3. **Dialect & Subsystem Isolation**:
   - Changes confined to specific dialects run that dialect's tests plus optimizer/expression validation.
4. **Optimal Worker Splitter (Makespan Minimization)**:
   - Dynamic worker partition (k = 1..8) using LPT load balancing to minimize `max(worker_load) + 2000 * k`.
5. **Robust Fallback**:
   - Universal `try/except` guard per commit guaranteeing valid output. Unrecognized changes fall back safely to all tests.
   - Ready for Docker execution (world-readable, read-only disk compatible, nobody user).

## Next Steps
- Implement Step C & D: Fine-grained hunk AST symbol analysis (function/method level).
- Synthesize mutation patches to test recall generalization on unseen commits.
- Lower score towards target ~140 s while strictly guaranteeing 0 misses.
