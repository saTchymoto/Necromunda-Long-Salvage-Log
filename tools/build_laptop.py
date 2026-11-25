"""Build the laptop version: one HTML file that runs from disk with no internet or sign-in, keeping its data in that browser.
It starts from a backup (Backup button on the live site), so it opens with the campaign as it was then.
Usage: python3 tools/build_laptop.py <backup.json> [output.html]     (default: build/Long Salvage Battle Log.html)
The output holds the campaign data, so keep it out of the repository (build/ is ignored)."""
import json, os, sys
from common import ROOT, read_source, embed_images, wrap

if len(sys.argv) < 2:
    sys.exit(__doc__)
backup = json.load(open(sys.argv[1], encoding='utf-8'))
if not isinstance(backup.get('gangs'), dict) or not isinstance(backup.get('battles'), dict):
    sys.exit('That file is not a battle log backup (no gangs and battles in it).')
out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, 'build', 'Long Salvage Battle Log.html')
os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
data = {k: backup.get(k) or {} for k in ('gangs', 'battles', 'territories', 'config')}
seed = json.dumps(data, ensure_ascii=False).replace('</', '<\\/')
doc = wrap(embed_images(read_source()), body_start=f'<script type="application/json" id="seed-data">{seed}</script>\n')
open(out, 'w', encoding='utf-8').write(doc)
print(out, len(doc) // 1024, 'KB,', len(data['gangs']), 'gangs,', len(data['battles']), 'battles and post-cycles')
