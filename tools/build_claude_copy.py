"""Build the Claude copy (the artifact on claude.ai, which keeps its data in Claude's own database): index.html plus the images
beside it, ready to publish as the artifact's files.
Usage: python3 tools/build_claude_copy.py [output folder]     (default: build/claude-copy)"""
import os, shutil, sys
from common import ROOT, IMAGES, IMAGE_FILES, read_source

out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'build', 'claude-copy')
os.makedirs(out_dir, exist_ok=True)
# The page wrapper claude.ai expects; the page itself (title, styles, markup, script) goes in the body unchanged.
head = ('<!doctype html><html><head><meta charset=utf8>'
        '<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover"><style></style></head><body>')
doc = head + read_source() + '</body></html>'
open(os.path.join(out_dir, 'index.html'), 'w', encoding='utf-8').write(doc)
for fn in IMAGE_FILES:
    shutil.copy(os.path.join(IMAGES, fn), os.path.join(out_dir, fn))
print(os.path.join(out_dir, 'index.html'), len(doc) // 1024, 'KB, plus', len(IMAGE_FILES), 'images')
