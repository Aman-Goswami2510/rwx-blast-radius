import re, json

tests = json.load(open('sample/tests.json'))['tests']
id_to_num = {t['id']: t['number'] for t in tests}

lines = open('sqlglot_repo/tests/test_optimizer.py').readlines()
current_func = None
check_file_map = {}

for line in lines:
    m = re.match(r'\s*def\s+(test_\w+)', line)
    if m:
        current_func = m.group(1)
    m2 = re.search(r'self\.check_file\((["\'])([^"\']+)\1', line)
    if m2 and current_func:
        check_file_map[m2.group(2)] = current_func

print(f"Found {len(check_file_map)} check_file mappings:")
for k, v in sorted(check_file_map.items()):
    tid = f"tests/test_optimizer.py::TestOptimizer::{v}"
    print(f"optimizer/{k}.sql -> {tid} (number: {id_to_num.get(tid)})")
