# PROGRESS: RWX – Blast Radius Track

## Current State Summary
- **Baseline Starter Score**: ~1614 s on remote server (or 1210.470 s unpartitioned), 0 misses.
- **Official Submission 1 (submit-1)**: 2629.304 s (7 misses × 300 s penalty + ~529 s execution).
- **Diagnosis of Official Misses**:
  - In `submit-1`, if `selector_db.json` was not loaded properly in the container environment or if a key was absent, empty sets `set()` were returned for fixtures, dialects, or optimizers, causing non-inert commits to have 0 tests selected (`workers: []`).
- **Repairs & Enhancements Made**:
  1. **Multi-Path & Invariant Loading**: `POSSIBLE_DB_PATHS` checking `__file__.parent`, `/app/selector_db.json`, and `./selector_db.json`.
  2. **Non-Inert Safety Invariant**: Enforced that non-inert patches *never* produce an empty test selection; if DB queries return empty, it safely falls back to the full suite. Even if the DB were completely deleted or missing, misses remain **0** (score: 964.607 s vs starter 1614 s).
  3. **High-Value Exclusion Optimization**: Excluded test #1490 (`tests/test_parser.py::TestParser::test_max_nodes`, 28.817s) from the fallback tier unless parser files are modified, dropping simulated cost from 392.321 s to **362.869 s** with **0 misses**.
- **Current Verified Score**: **362.869 s**, **0 misses** on all 30 sample commits!

## Next Steps
- Fine-grained hunk AST symbol mapping to further lower cost while preserving 0 misses.
- Offline synthetic mutation corpus validation across diverse unseen patch structures.
