import json
import re
from pathlib import Path

tests = json.load(open('sample/tests.json'))['tests']
answers = json.load(open('sample/answers.json'))['commits']

def partition(test_nums, max_k=8):
    if not test_nums:
        return []
    durations = [round(tests[n]['seconds']*1000) for n in test_nums]
    indexed = sorted(range(len(test_nums)), key=lambda i: durations[i], reverse=True)
    best_cost = float('inf')
    best_workers = []
    for k in range(1, min(len(test_nums), max_k) + 1):
        loads = [0] * k
        w = [[] for _ in range(k)]
        for i in indexed:
            l = loads.index(min(loads))
            w[l].append(test_nums[i])
            loads[l] += durations[i]
        cost = max(loads) + 2000 * k
        if cost < best_cost:
            best_cost = cost
            best_workers = w
    return best_workers

all_tests = list(range(len(tests)))
opt_tests = [t['number'] for t in tests if 'test_optimizer' in t['file']]

test_by_file = {}
for t in tests:
    test_by_file.setdefault(t['file'], []).append(t['number'])

sim_cost = 0
misses = 0
for cid, cinfo in sorted(answers.items()):
    patch = open(f'sample/commits/{cid}.patch').read()
    files = re.findall(r'diff --git a/(\S+)', patch)
    
    # check if only comments/whitespace
    is_pure_comment = True
    for line in patch.splitlines():
        if line.startswith('+') and not line.startswith('+++'):
            clean = line[1:].strip()
            if clean and not clean.startswith('#') and not clean.startswith('"""') and not clean.startswith("'''"):
                is_pure_comment = False
                break
        elif line.startswith('-') and not line.startswith('---'):
            clean = line[1:].strip()
            if clean and not clean.startswith('#') and not clean.startswith('"""') and not clean.startswith("'''"):
                is_pure_comment = False
                break
    
    if is_pure_comment:
        selected = []
    elif all('fixtures/optimizer/' in f for f in files):
        selected = opt_tests
    else:
        dialects = set()
        for f in files:
            m = re.search(r'dialects/(\w+)\.py|generators/(\w+)\.py|parsers/(\w+)\.py|typing/(\w+)\.py', f)
            if m:
                dialects.add(m.group(1) or m.group(2) or m.group(3) or m.group(4))
            elif 'fixtures/optimizer/' in f:
                pass
            else:
                dialects = None
                break
        if dialects is not None and len(dialects) > 0:
            s = set(opt_tests)
            for d in dialects:
                fpath = f'tests/dialects/test_{d}.py'
                if fpath in test_by_file:
                    s.update(test_by_file[fpath])
            if 'tests/test_parser.py' in test_by_file:
                s.update(test_by_file['tests/test_parser.py'])
            if 'tests/test_expressions.py' in test_by_file:
                s.update(test_by_file['tests/test_expressions.py'])
            selected = sorted(list(s))
        else:
            selected = all_tests
            
    for f in cinfo['failing']:
        if f not in selected:
            print('MISS in commit', cid, 'test', f)
            misses += 1
            break
            
    w = partition(selected)
    if not w:
        c = 0
    else:
        loads = [sum(round(tests[n]['seconds']*1000) for n in wk) for wk in w]
        c = max(loads) + 2000 * len(w)
    sim_cost += c

print('Total sim cost (sec):', sim_cost / 1000.0, 'misses:', misses)
