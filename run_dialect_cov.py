"""Compute module/file-level coverage for test files."""

import glob
import json
import os
import subprocess
import time
from pathlib import Path
import sqlite3
from coverage.numbits import numbits_to_nums

tests = json.loads(Path("sample/tests.json").read_text(encoding="utf-8"))["tests"]
file_to_test_nums = {}
for t in tests:
    file_to_test_nums.setdefault(t["file"], []).append(t["number"])

# Group tests by dialect file vs other
dialect_files = [f for f in file_to_test_nums if "tests/dialects/test_" in f and f != "tests/dialects/test_dialect.py"]
other_files = [f for f in file_to_test_nums if f not in dialect_files]

print(f"Dialect test files: {len(dialect_files)}, other test files: {len(other_files)}")

# Measure coverage for each dialect test file
# We run pytest on each dialect test file with coverage
cov_map = {} # source_file -> set of test_files

for idx, tf in enumerate(dialect_files):
    cov_db = f".cov_{idx}.db"
    cmd = [
        "python", "-m", "coverage", "run",
        f"--data-file={cov_db}",
        "--source=sqlglot_repo/sqlglot",
        "-m", "pytest",
        f"sqlglot_repo/{tf}",
        "-q"
    ]
    t0 = time.time()
    res = subprocess.run(cmd, capture_output=True, text=True)
    dt = time.time() - t0
    
    if os.path.exists(cov_db):
        con = sqlite3.connect(cov_db)
        cur = con.cursor()
        cur.execute("SELECT id, path FROM file")
        fmap = dict(cur.fetchall())
        cur.execute("SELECT DISTINCT file_id FROM line_bits")
        touched_files = [fmap[fid].replace("\\", "/").split("sqlglot_repo/")[-1] for (fid,) in cur.fetchall() if fid in fmap]
        con.close()
        try:
            os.remove(cov_db)
        except:
            pass
        for src in touched_files:
            cov_map.setdefault(src, set()).add(tf)
        print(f"[{idx+1}/{len(dialect_files)}] {tf} ({dt:.2f}s) touched {len(touched_files)} files")
    else:
        print(f"FAILED {tf}: {res.stderr[:200]}")

# Save dialect coverage
Path("dialect_cov.json").write_text(json.dumps({k: sorted(list(v)) for k, v in cov_map.items()}, indent=2))
print("Saved dialect_cov.json")
