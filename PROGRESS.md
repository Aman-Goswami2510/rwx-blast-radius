# Progress

* v1/v2 (earlier): heuristic selector. Official score 2577.8 s with 7 misses (the heuristic fell back to empty sets).
* v3 (this version): coverage-map selector. Sample: **176.4 s, 0 misses** (starter 1614 s). See DESIGN.md.
* Next ideas: tighten module-level selection, calibrate with more mutants, drop tests that never fail on any mutant.
