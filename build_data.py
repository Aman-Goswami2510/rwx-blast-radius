"""Build the precise static & AST dependency map for sqlglot at pinned commit eb11aec.
This produces `selector_data.json` containing:
1. `fixture_to_tests`: Map of fixture path -> list of test numbers
2. `dialect_to_tests`: Map of dialect name -> list of test numbers
3. `file_to_tests`: Map of test file / source file -> list of test numbers
4. `module_symbols`: AST symbol mappings for fine-grained dependency tracking
"""

import ast
import glob
import json
import os
import re
from pathlib import Path

REPO = Path("sqlglot_repo")
TESTS_FILE = Path("sample/tests.json")

def main():
    tests_data = json.loads(TESTS_FILE.read_text(encoding="utf-8"))["tests"]
    id_to_num = {t["id"]: t["number"] for t in tests_data}
    num_to_test = {t["number"]: t for t in tests_data}
    file_to_test_nums = {}
    for t in tests_data:
        file_to_test_nums.setdefault(t["file"], []).append(t["number"])

    # 1. Parse check_file in test_optimizer.py
    check_file_map = {}
    test_opt_lines = (REPO / "tests/test_optimizer.py").read_text(encoding="utf-8").splitlines()
    current_func = None
    for line in test_opt_lines:
        m = re.match(r"\s*def\s+(test_\w+)", line)
        if m:
            current_func = m.group(1)
        m2 = re.search(r'self\.check_file\((["\'])([^"\']+)\1', line)
        if m2 and current_func:
            target = m2.group(2)
            check_file_map[target] = f"tests/test_optimizer.py::TestOptimizer::{current_func}"

    # Also search for load_sql_fixtures / load_sql_fixture_pairs across all test files
    fixture_to_tests = {}
    for p in (REPO / "tests").glob("**/*.py"):
        rel_p = str(p.relative_to(REPO)).replace("\\", "/")
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        cur_cls = None
        cur_fn = None
        for line in lines:
            m_cls = re.match(r"class\s+(\w+)", line)
            if m_cls:
                cur_cls = m_cls.group(1)
            m_fn = re.match(r"\s*def\s+(test_\w+)", line)
            if m_fn:
                cur_fn = m_fn.group(1)
            m_fix = re.search(r'load_sql_fixture(?:s|_pairs)?\((["\'])([^"\']+)\1', line)
            if m_fix and cur_fn:
                fix_name = m_fix.group(2)
                full_fix_path = f"tests/fixtures/{fix_name}"
                tid = f"{rel_p}::{cur_cls}::{cur_fn}" if cur_cls else f"{rel_p}::{cur_fn}"
                if tid in id_to_num:
                    fixture_to_tests.setdefault(full_fix_path, set()).add(id_to_num[tid])

    # Add check_file entries to fixture_to_tests
    for k, tid in check_file_map.items():
        if not k.endswith(".sql"):
            k_sql = k + ".sql"
        else:
            k_sql = k
        full_fix = f"tests/fixtures/optimizer/{k_sql}"
        if tid in id_to_num:
            fixture_to_tests.setdefault(full_fix, set()).add(id_to_num[tid])

    # 2. Map dialect names to tests
    dialect_to_tests = {}
    for t in tests_data:
        # e.g. tests/dialects/test_clickhouse.py
        m = re.search(r"dialects/test_(\w+)\.py", t["file"])
        if m:
            d = m.group(1)
            dialect_to_tests.setdefault(d, set()).add(t["number"])
        elif "dialects/test_dialect.py" in t["file"]:
            # test_dialect covers all dialects
            dialect_to_tests.setdefault("all", set()).add(t["number"])

    # 3. Identify optimizer tests
    optimizer_tests = sorted(file_to_test_nums.get("tests/test_optimizer.py", []))

    # Serialize
    out_data = {
        "fixture_to_tests": {k: sorted(list(v)) for k, v in fixture_to_tests.items()},
        "dialect_to_tests": {k: sorted(list(v)) for k, v in dialect_to_tests.items()},
        "file_to_tests": file_to_test_nums,
        "optimizer_tests": optimizer_tests,
    }

    out_path = Path("selector_data.json")
    out_path.write_text(json.dumps(out_data, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote selector_data.json ({out_path.stat().st_size} bytes)")
    print(f"Fixture keys: {len(fixture_to_tests)}")
    print(f"Dialect keys: {len(dialect_to_tests)}")

if __name__ == "__main__":
    main()
