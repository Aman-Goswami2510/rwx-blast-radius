"""Smart, fast, safe test selector for Blast Radius hackathon track.
Pure standard library. Fully compliant with SCORING.md.
"""

import json
import os
import re
import sys
from pathlib import Path

DATA = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = Path(__file__).parent / "selector_db.json"

def is_comment_whitespace_only(patch_text: str) -> bool:
    """Return True if diff consists only of comments, docstrings, or blank lines."""
    has_changes = False
    for line in patch_text.splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+") or line.startswith("-"):
            has_changes = True
            content = line[1:].strip()
            if not content:
                continue
            if content.startswith("#"):
                continue
            if content.startswith('"""') or content.startswith("'''"):
                continue
            return False
    return has_changes

def partition(test_nums: list[int], tests_info: list[dict], max_k: int = 8) -> list[list[int]]:
    """Partition tests across up to max_k workers minimizing makespan + 2k."""
    if not test_nums:
        return []
    
    durations = [round(tests_info[n]["seconds"] * 1000) for n in test_nums]
    indexed = sorted(range(len(test_nums)), key=lambda i: durations[i], reverse=True)
    
    best_cost = float("inf")
    best_workers = []
    
    max_workers = min(len(test_nums), max_k)
    for k in range(1, max_workers + 1):
        loads = [0] * k
        workers = [[] for _ in range(k)]
        for i in indexed:
            lightest = loads.index(min(loads))
            workers[lightest].append(test_nums[i])
            loads[lightest] += durations[i]
        cost = max(loads) + 2000 * k
        if cost < best_cost:
            best_cost = cost
            best_workers = workers
            
    return best_workers

def select_tests_for_patch(patch_path: Path, db: dict, all_tests_count: int) -> list[int]:
    try:
        patch_text = patch_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return list(range(all_tests_count))

    # 1. Inert check: pure comments / blank lines
    if is_comment_whitespace_only(patch_text):
        return []

    # 2. Extract changed files
    changed_files = re.findall(r"diff --git a/(\S+)", patch_text)
    if not changed_files:
        return list(range(all_tests_count))

    # 3. Check if all changed files are fixtures
    fixture_map = db.get("fixture_to_tests", {})
    if all(f.startswith("tests/fixtures/") for f in changed_files):
        selected = set()
        for f in changed_files:
            if f in fixture_map:
                selected.update(fixture_map[f])
            else:
                # Fallback to all optimizer tests if fixture not mapped
                selected.update(db.get("all_optimizer_tests", []))
        return sorted(list(selected))

    # 4. Check if changes are isolated to dialect files
    # (sqlglot/dialects/<d>.py, sqlglot/generators/<d>.py, sqlglot/parsers/<d>.py, sqlglot/typing/<d>.py)
    dialect_to_tests = db.get("dialect_to_tests", {})
    dialects = set()
    is_pure_dialect = True
    
    for f in changed_files:
        m = re.search(r"(?:dialects|generators|parsers|typing)/(\w+)\.py", f)
        if m:
            dialects.add(m.group(1))
        elif f.startswith("tests/fixtures/"):
            continue
        else:
            is_pure_dialect = False
            break

    if is_pure_dialect and dialects:
        selected = set()
        for d in dialects:
            if d in dialect_to_tests:
                selected.update(dialect_to_tests[d])
        # Dialect changes frequently impact optimizer/expression validation
        selected.update(db.get("all_optimizer_tests", []))
        selected.update(db.get("all_expression_tests", []))
        selected.update(db.get("all_parser_tests_fast", []))
        return sorted(list(selected))

    # 5. Check if changes are inside sqlglot/optimizer/
    if all(f.startswith("sqlglot/optimizer/") for f in changed_files):
        selected = set(db.get("all_optimizer_tests", []))
        # Also include executor and dialect tests since optimizer optimizes queries executed
        file_to_tests = db.get("file_to_tests", {})
        selected.update(file_to_tests.get("tests/test_executor.py", []))
        selected.update(file_to_tests.get("tests/test_expressions.py", []))
        return sorted(list(selected))

    # 6. Fallback: run all tests
    return list(range(all_tests_count))

def main():
    try:
        tests_data = json.loads((DATA / "tests.json").read_text(encoding="utf-8"))["tests"]
        db = json.loads(DB_PATH.read_text(encoding="utf-8")) if DB_PATH.exists() else {}
        all_tests_count = len(tests_data)

        commits_dir = DATA / "commits"
        if not commits_dir.exists():
            return

        for patch_file in sorted(commits_dir.glob("*.patch")):
            commit_id = patch_file.stem
            try:
                selected = select_tests_for_patch(patch_file, db, all_tests_count)
                workers = partition(selected, tests_data, max_k=8)
            except Exception as e:
                # Guarantee valid output line under any failure
                sys.stderr.write(f"Error on {commit_id}: {e}\n")
                workers = partition(list(range(all_tests_count)), tests_data, max_k=8)

            print(json.dumps({"commit": commit_id, "workers": workers}))

    except Exception as e:
        sys.stderr.write(f"Fatal error in main: {e}\n")

if __name__ == "__main__":
    main()
