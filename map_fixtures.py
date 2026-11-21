import re, json
from pathlib import Path

tests = json.load(open('sample/tests.json'))['tests']
id_to_num = {t['id']: t['number'] for t in tests}

fixture_to_test = {}
current_func = None

for line in open('sqlglot_repo/tests/test_optimizer.py'):
    m_func = re.match(r'\s*def\s+(test_\w+)\s*\(', line)
    if m_func:
        current_func = m_func.group(1)
    m_fix = re.search(r'load_sql_fixtures?\([\'"]([^\'"]+)[\'"]', line)
    if m_fix and current_func:
        fix_path = 'tests/fixtures/' + m_fix.group(1)
        test_id = 'tests/test_optimizer.py::TestOptimizer::' + current_func
        if test_id in id_to_num:
            fixture_to_test.setdefault(fix_path, set()).add(id_to_num[test_id])

print(f"Mapped {len(fixture_to_test)} fixtures to tests:")
for k, v in sorted(fixture_to_test.items()):
    print(k, "->", v)
