"""Send each '### N.M' block of a study-guide Markdown file through anki_gemini.

One Gemini run per block, in document order, exactly as if the block had been
copied to the clipboard and the Keyboard Maestro macro run (the clipboard itself is
never touched). Inline <svg> lines are dropped; figure captions are kept as text.
Blocks that succeeded are remembered in .anki_blocks_progress.json, so a rerun
continues where the last one stopped.

  anki_blocks.py NOTE.md DECK --list            # show blocks and what is done
  anki_blocks.py NOTE.md DECK --from 2.4        # process 2.4 onward (auto-apply)
  anki_blocks.py NOTE.md DECK --only 5.3 5.4    # just these blocks
"""
import argparse
import json
from pathlib import Path
import re
import sys

from ankigen import cli

PROGRESS = Path(__file__).resolve().parent / '.anki_blocks_progress.json'
HEADING = re.compile(r'^###\s+(\d+(?:\.\d+)+)\s+(.*)$')
CAPTION = re.compile(r'^\*Figure\s+\d+\.\s*(.*?)\*$')


def blocks(markdown):
    """[(id, title, text)] for every '### N.M Title' block; a block ends at the next
    heading of any level or at a '---' rule."""
    out, current = [], None
    for line in markdown.splitlines():
        m = HEADING.match(line)
        if m or line.startswith('#') or line.strip() == '---':
            if current:
                out.append(current)
            current = (m.group(1), m.group(2).strip(), [line]) if m else None
            continue
        if current is None or line.lstrip().startswith('<svg'):
            continue
        c = CAPTION.match(line.strip())
        current[2].append('Figure (described): ' + c.group(1) if c else line)
    if current:
        out.append(current)
    return [(i, t, re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip())
            for i, t, lines in out]


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


def process(text, deck, mode):
    """One anki_gemini run with `text` as the clipboard source."""
    cli.clipboard = lambda: (text, None)
    argv = sys.argv
    sys.argv = ['anki_gemini.py', deck, mode, '--quiet']
    try:
        cli.main()
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
    p.add_argument('--preview-only', action='store_true',
                   help='Save proposals for review instead of applying them')
    args = p.parse_args()

    note = args.note.expanduser().resolve()
    allb = blocks(note.read_text())
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
            print(f'{mark}  {i:>5}  {title}  ({len(text):,} chars)')
        return

    todo = [b for b in chosen if args.force or b[0] not in done]
    mode = '--preview-only' if args.preview_only else '--auto-apply'
    print(f'{len(todo)} block(s) to process into "{args.deck}" ({mode[2:]}); '
          f'{len(chosen) - len(todo)} already done.')
    for n, (i, title, text) in enumerate(todo, 1):
        print(f'\n[{n}/{len(todo)}] {i} {title}', flush=True)
        try:
            process(text, args.deck, mode)
        except Exception as exc:
            print(f'❌ {i} failed: {exc}\nStopped. Rerun the same command to continue '
                  f'from {i}.', file=sys.stderr)
            sys.exit(1)
        mark_done(str(note), args.deck, i)
    print('\nAll selected blocks processed.')


if __name__ == '__main__':
    main()
