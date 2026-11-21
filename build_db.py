"""Build complete offline data for selector:
- Exact fixture -> test ID mappings
- Dialect -> test files mappings
- Optimizer modules -> optimizer test functions
- Generic fallback mapping
"""

import ast
import glob
import json
import re
from pathlib import Path

tests = json.loads(Path("sample/tests.json").read_text(encoding="utf-8"))["tests"]
id_to_num = {t["id"]: t["number"] for t in tests}
file_to_nums = {}
for t in tests:
    file_to_nums.setdefault(t["file"], []).append(t["number"])

# 1. AST check_file in test_optimizer.py
tree = ast.parse(Path("sqlglot_repo/tests/test_optimizer.py").read_text(encoding="utf-8"))
cls = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TestOptimizer"][0]
check_file_map = {}
for item in cls.body:
    if isinstance(item, ast.FunctionDef) and item.name.startswith("test_"):
        for node in ast.walk(item):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr == "check_file":
                    if node.args and isinstance(node.args[0], ast.Constant):
                        fix_name = node.args[0].value
                        check_file_map[fix_name] = item.name

fixture_to_tests = {}
for fix_name, fn_name in check_file_map.items():
    if not fix_name.endswith(".sql"):
        k = f"tests/fixtures/optimizer/{fix_name}.sql"
    else:
        k = f"tests/fixtures/optimizer/{fix_name}"
    tid = f"tests/test_optimizer.py::TestOptimizer::{fn_name}"
    if tid in id_to_num:
        fixture_to_tests.setdefault(k, []).append(id_to_num[tid])

# Add load_sql_fixtures
for p in Path("sqlglot_repo/tests").glob("**/*.py"):
    rel = str(p.relative_to("sqlglot_repo")).replace("\\", "/")
    content = p.read_text(encoding="utf-8", errors="ignore")
    tree_p = ast.parse(content)
    for node in ast.walk(tree_p):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    # match load_sql_fixtures
                    fn_name = None
                    if isinstance(inner.func, ast.Name):
                        fn_name = inner.func.id
                    elif isinstance(inner.func, ast.Attribute):
                        fn_name = inner.func.attr
                    if fn_name and "load_sql_fixture" in fn_name and inner.args and isinstance(inner.args[0], ast.Constant):
                        fix_arg = inner.args[0].value
                        full_k = f"tests/fixtures/{fix_arg}"
                        # find full test id
                        for t in tests:
                            if t["file"] == rel and t["id"].endswith(f"::{node.name}"):
                                fixture_to_tests.setdefault(full_k, []).append(t["number"])

# 2. Map optimizer files to specific test functions in test_optimizer.py
# e.g. sqlglot/optimizer/eliminate_joins.py -> test_eliminate_joins, etc.
optimizer_file_to_tests = {}
for p in Path("sqlglot_repo/sqlglot/optimizer").glob("*.py"):
    opt_mod = p.stem # e.g. eliminate_joins
    rel = f"sqlglot/optimizer/{p.name}"
    matched_tests = []
    # Test function matching name: test_<opt_mod>
    target_tid = f"tests/test_optimizer.py::TestOptimizer::test_{opt_mod}"
    if target_tid in id_to_num:
        matched_tests.append(id_to_num[target_tid])
    # check check_file_map
    if opt_mod in check_file_map:
        tid = f"tests/test_optimizer.py::TestOptimizer::{check_file_map[opt_mod]}"
        if tid in id_to_num and id_to_num[tid] not in matched_tests:
            matched_tests.append(id_to_num[tid])
    if matched_tests:
        optimizer_file_to_tests[rel] = matched_tests

# 3. Dialect test mapping
dialect_to_tests = {}
for t in tests:
    m = re.search(r"dialects/test_(\w+)\.py", t["file"])
    if m:
        dialect_to_tests.setdefault(m.group(1), []).append(t["number"])

# 4. Critical baseline sets:
# - All optimizer tests
all_optimizer_tests = sorted(file_to_nums.get("tests/test_optimizer.py", []))
# - Core expression tests
all_expression_tests = sorted(file_to_nums.get("tests/test_expressions.py", []))
# - Core parser tests (excluding test_max_nodes: 1490)
all_parser_tests_fast = [n for n in file_to_nums.get("tests/test_parser.py", []) if n != 1490]
all_parser_tests_full = sorted(file_to_nums.get("tests/test_parser.py", []))

data = {
    "fixture_to_tests": {k: sorted(list(set(v))) for k, v in fixture_to_tests.items()},
    "optimizer_file_to_tests": optimizer_file_to_tests,
    "dialect_to_tests": dialect_to_tests,
    "file_to_tests": file_to_nums,
    "all_optimizer_tests": all_optimizer_tests,
    "all_expression_tests": all_expression_tests,
    "all_parser_tests_fast": all_parser_tests_fast,
    "all_parser_tests_full": all_parser_tests_full,
}

Path("selector_db.json").write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
print(f"Built selector_db.json ({Path('selector_db.json').stat().st_size} bytes)")
