"""Shared pieces for the build scripts: where things live, building the images into the page, and the page wrapper."""
import base64, datetime, os, re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'src', 'long-salvage-log.html')
IMAGES = os.path.join(ROOT, 'src', 'images')
# Every image the page loads by file name. The hosted and laptop builds put them inside the page; the Claude copy serves them beside it.
IMAGE_FILES = ['map-base.jpg', 'map-overlay-v3.png', 'map-tread.png', 'logo-fangs.png', 'logo-kyn.png', 'logo-delaque.png', 'logo-gsc.png']


def read_source(stamp=True):
    page = open(SRC, encoding='utf-8').read()
    if stamp:  # the version date shown at the bottom of the page
        page = re.sub(r'const APP_BUILD = "[0-9-]+";', 'const APP_BUILD = "%s";' % datetime.date.today().isoformat(), page, count=1)
    return page


def data_uri(fn):
    mime = 'image/jpeg' if fn.endswith('.jpg') else 'image/png'
    return 'data:%s;base64,%s' % (mime, base64.b64encode(open(os.path.join(IMAGES, fn), 'rb').read()).decode())


def embed_images(page):
    for fn in IMAGE_FILES:
        if fn == 'map-base.jpg':
            old = 'const MAP_IMG_SRC = "map-base.jpg";'
            assert old in page, old
            page = page.replace(old, 'const MAP_IMG_SRC = "%s";' % data_uri(fn), 1)
        else:
            assert '"' + fn + '"' in page, fn
            page = page.replace('"' + fn + '"', '"' + data_uri(fn) + '"', 1)
    return page


def wrap(page, head_extra='', body_start=''):
    """A full HTML document around the page: its <title>, <meta> tags and styles go in <head>, the rest in <body>."""
    cut = page.index('</style>') + len('</style>')
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
            '<style>:root{color-scheme:light}body{margin:0;font:14px system-ui,sans-serif}img{max-width:100%}[hidden]{display:none!important}</style>\n'
            + head_extra + page[:cut] + '\n</head>\n<body>\n' + body_start + page[cut:] + '\n</body>\n</html>\n')
