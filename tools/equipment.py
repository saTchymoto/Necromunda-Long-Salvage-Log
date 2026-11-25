"""The Equipment Lists & Trading Post database lives inside the page (between the EQUIPMENT-DB markers) so the page works on its own.
data/equipment.json is the readable copy to edit.
  python3 tools/equipment.py extract   page  -> data/equipment.json
  python3 tools/equipment.py apply     data/equipment.json -> page (then build as usual)
  python3 tools/equipment.py check     do the two match?"""
import json, os, re, sys
from common import ROOT, SRC

JSON_PATH = os.path.join(ROOT, 'data', 'equipment.json')
BLOCK = re.compile(r'(/\* EQUIPMENT-DB:BEGIN[^\n]*\*/\n)const EQUIPMENT = (.*?);\n(/\* EQUIPMENT-DB:END \*/)', re.S)


def page_data(page):
    m = BLOCK.search(page)
    if not m:
        sys.exit('EQUIPMENT-DB markers not found in ' + SRC)
    return m, json.loads(m.group(2))


cmd = sys.argv[1] if len(sys.argv) > 1 else ''
page = open(SRC, encoding='utf-8').read()
m, data = page_data(page)
if cmd == 'extract':
    os.makedirs(os.path.dirname(JSON_PATH), exist_ok=True)
    json.dump(data, open(JSON_PATH, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    open(JSON_PATH, 'a').write('\n')
    print('wrote', JSON_PATH, len(data.get('items', [])), 'items')
elif cmd == 'apply':
    new = json.load(open(JSON_PATH, encoding='utf-8'))
    text = json.dumps(new, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    page = page[:m.start()] + m.group(1) + 'const EQUIPMENT = ' + text + ';\n' + m.group(3) + page[m.end():]
    open(SRC, 'w', encoding='utf-8').write(page)
    print('updated the page from', JSON_PATH)
elif cmd == 'check':
    same = json.load(open(JSON_PATH, encoding='utf-8')) == data
    print('data/equipment.json and the page match' if same else 'data/equipment.json and the page DIFFER: run extract or apply')
    sys.exit(0 if same else 1)
else:
    sys.exit(__doc__)
