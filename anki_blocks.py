"""Send each '### N.M' block of a study-guide Markdown file through anki_gemini.

One Gemini run per block, in document order, exactly as if the block had been
copied to the clipboard and the Keyboard Maestro macro run (the clipboard itself is
never touched). Figures in a block reach Gemini and the cards: each inline <svg> is
rendered to PNG with headless Chrome and each Markdown image link ![alt](path) is
read from disk (paths are relative to the note); Gemini sees them and may put them on
the cards it writes, and only the images a card uses are uploaded to Anki's media
folder. Figure captions stay as text. --no-images turns this off (figures are then
dropped as before). Blocks that succeeded are remembered in .anki_blocks_progress.json, so a rerun
continues where the last one stopped.

  anki_blocks.py NOTE.md DECK --list            # show blocks and what is done
  anki_blocks.py NOTE.md DECK --from 2.4        # process 2.4 onward (auto-apply)
  anki_blocks.py NOTE.md DECK --only 5.3 5.4    # just these blocks
  anki_blocks.py NOTE.md DECK --no-images       # text only
"""
import argparse
import json
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import unquote

from ankigen import cli
from ankigen.images import slug

PROGRESS = Path(__file__).resolve().parent / '.anki_blocks_progress.json'
HEADING = re.compile(r'^###\s+(\d+(?:\.\d+)+)\s+(.*)$')
CAPTION = re.compile(r'^\*Figure\s+\d+\.\s*(.*?)\*$')
IMAGE_LINK = re.compile(r'!\[([^\]]*)\]\(\s*(<[^>]+>|[^)\s]+)(?:\s+"[^"]*")?\s*\)')
REMOTE = re.compile(r'^(?:[a-z][a-z0-9+.-]*:|//)', re.I)


def parse(markdown):
    """[(id, title, text, figures)] for every '### N.M Title' block; a block ends at the
    next heading of any level or at a '---' rule. `figures` lists, in document order,
    ('svg', svg_text) for inline <svg> lines (which may span lines) and ('file', target)
    for local Markdown image links; neither stays in `text` (a link leaves '[Figure:
    alt]'), but captions do."""
    out, current, svg = [], None, None
    for line in markdown.splitlines():
        if svg is not None:
            svg.append(line)
            if '</svg>' in line:
                current[3].append(('svg', '\n'.join(svg))); svg = None
            continue
        m = HEADING.match(line)
        if m or line.startswith('#') or line.strip() == '---':
            if current:
                out.append(current)
            current = (m.group(1), m.group(2).strip(), [line], []) if m else None
            continue
        if current is None:
            continue
        if line.lstrip().startswith('<svg'):
            if '</svg>' in line:
                current[3].append(('svg', line.strip()))
            else:
                svg = [line.strip()]
            continue
        c = CAPTION.match(line.strip())
        if c:
            line = 'Figure (described): ' + c.group(1)

        def link(match):
            target = match.group(2).strip('<>')
            if not REMOTE.match(target):
                current[3].append(('file', unquote(target)))
            return f'[Figure: {match.group(1)}]' if match.group(1) else ''
        current[2].append(IMAGE_LINK.sub(link, line))
    if current:
        out.append(current)
    return [(i, t, re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip(), figures)
            for i, t, lines, figures in out]


def blocks(markdown):
    """[(id, title, text)]; see parse for the block rules."""
    return [(i, t, text) for i, t, text, _ in parse(markdown)]


def figure_files(figures, folder, base, stem, block_id):
    """Paths of a block's figures, ready for --image: inline SVGs are written into
    `folder`, linked files are resolved against `base` (missing ones are reported)."""
    paths = []
    for k, (kind, value) in enumerate(figures, 1):
        if kind == 'svg':
            path = Path(folder) / f'{slug(stem, 24)}-{block_id.replace(".", "-")}-fig{k}.svg'
            path.write_text(value, encoding='utf-8')
        else:
            path = (Path(base) / value).expanduser()
            if not path.is_file():
                print(f'⚠️ {block_id}: image not found, skipped: {value}', file=sys.stderr)
                continue
        paths.append(path)
    return paths


def key(i):
    return tuple(int(p) for p in i.split('.'))


def load_progress():
    return json.loads(PROGRESS.read_text()) if PROGRESS.exists() else {}


def mark_done(note, deck, block_id):
    data = load_progress()
    done = data.setdefault(f'{note}::{deck}', [])
    if block_id not in done:
        done.append(block_id)
    PROGRESS.write_text(json.dumps(data, indent=1, ensure_ascii=False))


def process(text, deck, mode, image_paths=()):
    """One anki_gemini run with `text` as the clipboard source. Returns the run summary
    ({'skipped_duplicates': [titles]}), or None when the run reports nothing."""
    cli.clipboard = lambda: (text, None)
    argv = sys.argv
    sys.argv = ['anki_gemini.py', deck, mode, '--quiet']
    for path in image_paths:
        sys.argv += ['--image', str(path)]
    try:
        return cli.main()
    finally:
        sys.argv = argv


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('note', type=Path)
    p.add_argument('deck')
    p.add_argument('--from', dest='start', help='First block id, e.g. 2.4')
    p.add_argument('--to', dest='end', help='Last block id, inclusive')
    p.add_argument('--only', nargs='+', help='Only these block ids')
    p.add_argument('--list', action='store_true', help='List blocks and progress; no Gemini calls')
    p.add_argument('--force', action='store_true', help='Also redo blocks marked done')
    p.add_argument('--no-images', action='store_true',
                   help='Text only: do not render or attach the figures of a block')
    p.add_argument('--preview-only', action='store_true',
                   help='Save proposals for review instead of applying them')
    args = p.parse_args()

    note = args.note.expanduser().resolve()
    parsed = parse(note.read_text())
    allb = [(i, t, x) for i, t, x, _ in parsed]
    figures = {i: f for i, _, _, f in parsed}
    if not allb:
        sys.exit('No "### N.M Title" blocks found.')
    known = {i for i, _, _ in allb}
    for i in [args.start, args.end] + (args.only or []):
        if i and i not in known:
            sys.exit(f'Unknown block id: {i}')
    done = set(load_progress().get(f'{note}::{args.deck}', []))
    chosen = [b for b in allb
              if (not args.only or b[0] in args.only)
              and (not args.start or key(b[0]) >= key(args.start))
              and (not args.end or key(b[0]) <= key(args.end))]

    if args.list:
        for i, title, text in allb:
            mark = 'done' if i in done else ('todo' if (i, title, text) in chosen else '    ')
            n = len(figures[i])
            print(f'{mark}  {i:>5}  {title}  ({len(text):,} chars'
                  + (f', {n} figure{"s" * (n != 1)}' if n else '') + ')')
        return

    todo = [b for b in chosen if args.force or b[0] not in done]
    mode = '--preview-only' if args.preview_only else '--auto-apply'
    print(f'{len(todo)} block(s) to process into "{args.deck}" ({mode[2:]}); '
          f'{len(chosen) - len(todo)} already done.')
    skipped_total = 0
    scratch = tempfile.TemporaryDirectory()
    for n, (i, title, text) in enumerate(todo, 1):
        files = [] if args.no_images else figure_files(
            figures[i], scratch.name, note.parent, note.stem, i)
        print(f'\n[{n}/{len(todo)}] {i} {title}'
              + (f' ({len(files)} figure(s))' if files else ''), flush=True)
        try:
            summary = process(text, args.deck, mode, files)
        except Exception as exc:
            print(f'❌ {i} failed: {exc}\nStopped. Rerun the same command to continue '
                  f'from {i}.', file=sys.stderr)
            sys.exit(1)
        # A block whose cards were partly skipped as duplicates is still finished:
        # the rest of it is in Anki, so resuming must not send it again.
        skipped = len((summary or {}).get('skipped_duplicates', []))
        skipped_total += skipped
        if skipped:
            print(f'{i}: {skipped} duplicate card(s) skipped; block counted as done.')
        mark_done(str(note), args.deck, i)
    scratch.cleanup()
    print('\nAll selected blocks processed.'
          + (f' {skipped_total} duplicate card(s) skipped in total.' if skipped_total else ''))


if __name__ == '__main__':
    main()
