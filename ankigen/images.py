"""Images for cards: render inline SVG with headless Chrome, load image files,
and give every image a stable file name for Anki's media folder.

An image becomes an `Image(name, data, mime)`. The name is a slug of the file stem
plus a hash of the source bytes, so rerunning the same figure reuses one media file
and a changed figure gets a new name.
"""
import base64
from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

MIME = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.gif': 'image/gif', '.webp': 'image/webp'}
MAX_BYTES = 15_000_000
MAX_IMAGES = 10
SCALE = 2.0
MAX_CSS_PX = 4000
CHROME_PATHS = ('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                '/Applications/Chromium.app/Contents/MacOS/Chromium',
                '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge')
CHROME_NAMES = ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser',
                'microsoft-edge')


@dataclass(frozen=True)
class Image:
    name: str
    data: bytes
    mime: str


def slug(text, limit=40):
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')[:limit].strip('-') or 'image'


def named(stem, source, data, mime):
    """Image whose name is `stem` plus a hash of `source` (bytes it came from)."""
    ext = next(e for e, m in MIME.items() if m == mime and e != '.jpeg')
    return Image(f'{slug(stem)}-{hashlib.sha1(source).hexdigest()[:8]}{ext}', data, mime)


def clipboard_image(data):
    return named('clipboard', data, data, 'image/png')


def find_chrome():
    """$ANKIGEN_CHROME, then the macOS app bundles, then Chrome/Chromium on PATH."""
    env = os.environ.get('ANKIGEN_CHROME')
    if env and os.access(env, os.X_OK):
        return env
    for path in CHROME_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    for name in CHROME_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def svg_size(svg):
    """(width, height) in CSS px from the root tag's width/height or viewBox, or None."""
    root = re.search(r'<svg\b[^>]*>', svg, re.I | re.S)
    if not root:
        return None
    tag = root.group(0)

    def number(attr):
        m = re.search(r'\s' + attr + r'\s*=\s*["\']\s*([\d.]+)\s*(?:px)?\s*["\']', tag)
        return float(m.group(1)) if m else None

    width, height = number('width'), number('height')
    box = re.search(r'viewBox\s*=\s*["\']\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)', tag)
    if box and float(box.group(1)) > 0:
        vw, vh = float(box.group(1)), float(box.group(2))
        width = width or (height * vw / vh if height else vw)
        height = height or width * vh / vw
    return (width, height) if width and height else None


def _fixed_size(svg, width, height):
    """The SVG with explicit pixel width/height on the root tag, no max-width."""
    def rewrite(m):
        tag = re.sub(r'\s(?:width|height)\s*=\s*(?:"[^"]*"|\'[^\']*\')', '', m.group(0))
        tag = re.sub(r'max-width\s*:[^;"\']*;?', '', tag)
        return tag[:-1].rstrip('/ ') + f' width="{width:g}" height="{height:g}">'
    return re.sub(r'<svg\b[^>]*>', rewrite, svg, count=1, flags=re.I | re.S)


def svg_to_png(svg, scale=SCALE):
    """One inline SVG string -> PNG bytes of exactly the figure, on white, at `scale`x."""
    size = svg_size(svg)
    if not size:
        raise ValueError('The SVG has neither width and height nor a viewBox, so its size is unknown.')
    width, height = size
    if not 0 < width <= MAX_CSS_PX or not 0 < height <= MAX_CSS_PX:
        raise ValueError(f'SVG size {width:g}x{height:g} is outside 1..{MAX_CSS_PX} px.')
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError('Rendering SVG figures needs Google Chrome (or Chromium/Edge); none '
                           'found. Install it or set ANKIGEN_CHROME to the binary, or pass '
                           '--no-images.')
    from PIL import Image as Pillow
    with tempfile.TemporaryDirectory() as tmp:
        page, out = Path(tmp) / 'figure.html', Path(tmp) / 'figure.png'
        page.write_text('<!doctype html><html><head><meta charset="utf-8"></head>'
                        '<body style="margin:0;background:#fff">'
                        + _fixed_size(svg, width, height) + '</body></html>', encoding='utf-8')
        command = [chrome, '--headless', '--disable-gpu', '--hide-scrollbars', '--no-first-run',
                   '--disable-extensions', f'--user-data-dir={Path(tmp) / "profile"}',
                   f'--force-device-scale-factor={scale:g}',
                   # The window is larger than the figure; it is cropped to the figure below.
                   f'--window-size={math.ceil(width) + 40},{math.ceil(height) + 160}',
                   '--virtual-time-budget=2000', f'--screenshot={out}', page.as_uri()]
        result = subprocess.run(command, capture_output=True, text=True, timeout=90)
        if not out.exists():
            raise RuntimeError('Headless Chrome produced no screenshot: ' + result.stderr[-500:])
        with Pillow.open(out) as shot:
            crop = shot.convert('RGB').crop((0, 0, round(width * scale), round(height * scale)))
        crop.save(out, 'PNG')
        return out.read_bytes()


def load(path):
    """Image from a file: PNG/JPEG/GIF/WebP as is, SVG rendered to PNG."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise ValueError(f'Image file not found: {path}')
    suffix = path.suffix.lower()
    if suffix != '.svg' and suffix not in MIME:
        raise ValueError(f'Unsupported image type {path.suffix or "(none)"} for {path.name}; '
                         'use PNG, JPEG, GIF, WebP or SVG.')
    source = path.read_bytes()
    if len(source) > MAX_BYTES:
        raise ValueError(f'{path.name} exceeds the 15 MB image guardrail.')
    if suffix == '.svg':
        return named(path.stem, source, svg_to_png(source.decode('utf-8')), 'image/png')
    return named(path.stem, source, source, MIME[suffix])


def load_all(paths):
    if len(paths) > MAX_IMAGES:
        raise ValueError(f'At most {MAX_IMAGES} images per run; got {len(paths)}.')
    found = []
    for path in paths:
        image = load(path)
        if all(image.name != other.name for other in found):
            found.append(image)
    return found


def media_entry(image):
    return dict(mime=image.mime, data=base64.b64encode(image.data).decode('ascii'))
