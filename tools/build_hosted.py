"""Build the live version (GitHub Pages): one index.html with the images built in, which signs in with Google and keeps
its data in the campaign's Firebase (Firestore) database, plus version.txt so open copies can tell a new version is out.
Usage: python3 tools/build_hosted.py [output folder]     (default: the repository root, where GitHub Pages serves from)"""
import datetime, json, os, sys
from common import ROOT, read_source, embed_images, wrap

FB_CONFIG = {  # public by design: access is decided by the database's security rules, not by this key
    "apiKey": "AIzaSyBzlJ3VWoea5h9DXxR9F1MjSQWxu5GafVA",
    "authDomain": "necromunda-long-salvage.firebaseapp.com",
    "projectId": "necromunda-long-salvage",
    "storageBucket": "necromunda-long-salvage.firebasestorage.app",
    "messagingSenderId": "130016889877",
    "appId": "1:130016889877:web:fa20c31d89bb5757cd121c",
}
FIREBASE_VERSION = "10.12.2"  # compat build: the same calls as the Claude copy's database

out_dir = sys.argv[1] if len(sys.argv) > 1 else ROOT
os.makedirs(out_dir, exist_ok=True)
page = embed_images(read_source())
build = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d%H%M%S')
sdk = ''.join(f'<script src="https://www.gstatic.com/firebasejs/{FIREBASE_VERSION}/firebase-{m}-compat.js"></script>\n' for m in ('app', 'auth', 'firestore'))
settings = f'<script>window.FB_CONFIG = {json.dumps(FB_CONFIG)}; window.HOSTED_BUILD = "{build}";</script>\n'
doc = wrap(page, head_extra=sdk + settings)
open(os.path.join(out_dir, 'index.html'), 'w', encoding='utf-8').write(doc)
open(os.path.join(out_dir, 'version.txt'), 'w').write(build + '\n')
print(os.path.join(out_dir, 'index.html'), len(doc) // 1024, 'KB, build', build)
