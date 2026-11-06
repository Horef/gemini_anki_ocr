import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import anki_blocks

NOTE = '''# Lecture 1 – Title

**How to use these notes.** Not a block.

---

# Part 1 – Basics

---

### 1.1 First idea

Text with $x^2$.

<svg viewBox="0 0 10 10"><rect width="100%" height="100%" fill="#fff"/></svg>

*Figure 1. The caption says what the figure shows.*

Deeper: link

---

### 1.10 Tenth idea

More text.

# Self-test

1. Question?
'''


class BlocksTests(unittest.TestCase):
    def test_splits_blocks_drops_svg_keeps_caption(self):
        b = anki_blocks.blocks(NOTE)
        self.assertEqual([i for i, _, _ in b], ['1.1', '1.10'])
        first = b[0][2]
        self.assertTrue(first.startswith('### 1.1 First idea'))
        self.assertNotIn('<svg', first)
        self.assertIn('Figure (described): The caption says what the figure shows.', first)
        self.assertIn('Deeper: link', first)
        self.assertNotIn('---', first)

    def test_block_ends_at_next_heading(self):
        last = anki_blocks.blocks(NOTE)[1][2]
        self.assertEqual(last, '### 1.10 Tenth idea\n\nMore text.')

    def test_ids_sort_numerically(self):
        self.assertLess(anki_blocks.key('1.2'), anki_blocks.key('1.10'))


GUIDE = '''### 2.1 With figures

Intro.

<svg viewBox="0 0 10 10"><rect width="10" height="10"/></svg>

*Figure 1. Squares.*

![Orientation map](figs/map%201.png)

![remote](https://example.com/x.png)

<svg width="20" height="10">
<circle r="3"/>
</svg>

### 2.2 No figures

Plain.
'''


class FigureTests(unittest.TestCase):
    def test_parse_collects_figures_in_order_and_keeps_text_clean(self):
        (i, _, text, figures), (_, _, plain_text, none) = anki_blocks.parse(GUIDE)
        self.assertEqual(i, '2.1')
        self.assertEqual([k for k, _ in figures], ['svg', 'file', 'svg'])
        self.assertEqual(figures[1], ('file', 'figs/map 1.png'))
        self.assertTrue(figures[2][1].endswith('</svg>') and '<circle' in figures[2][1])
        self.assertNotIn('<svg', text)
        self.assertNotIn('<circle', text)
        self.assertIn('Figure (described): Squares.', text)
        self.assertIn('[Figure: Orientation map]', text)
        self.assertEqual((plain_text, none), ('### 2.2 No figures\n\nPlain.', []))

    def test_blocks_keeps_its_three_item_shape(self):
        self.assertEqual([len(b) for b in anki_blocks.blocks(GUIDE)], [3, 3])

    def test_figure_files_write_svg_and_resolve_links(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'figs').mkdir()
            (Path(d) / 'figs' / 'map 1.png').write_bytes(b'x')
            scratch = Path(d) / 'scratch'; scratch.mkdir()
            figures = anki_blocks.parse(GUIDE)[0][3] + [('file', 'missing.png')]
            with patch('sys.stderr', new_callable=io.StringIO) as err:
                paths = anki_blocks.figure_files(figures, scratch, d, 'My Guide', '2.1')
            self.assertEqual([p.name for p in paths],
                             ['my-guide-2-1-fig1.svg', 'map 1.png', 'my-guide-2-1-fig3.svg'])
            self.assertIn('<rect', paths[0].read_text())
            self.assertIn('missing.png', err.getvalue())

    def test_process_passes_each_figure_as_an_image_option(self):
        seen = []
        with patch.object(anki_blocks.cli, 'main', side_effect=lambda: seen.append(list(
                anki_blocks.sys.argv)) or {'skipped_duplicates': []}):
            result = anki_blocks.process('t', 'Deck', '--auto-apply', [Path('a.svg'), Path('b.png')])
        self.assertEqual(seen[0], ['anki_gemini.py', 'Deck', '--auto-apply', '--quiet',
                                   '--image', 'a.svg', '--image', 'b.png'])
        self.assertEqual(result, {'skipped_duplicates': []})

    def run_main(self, extra):
        with tempfile.TemporaryDirectory() as d:
            note = Path(d) / 'guide.md'
            note.write_text(GUIDE)
            (Path(d) / 'figs').mkdir()
            (Path(d) / 'figs' / 'map 1.png').write_bytes(b'x')
            sent = []
            def fake_process(text, deck, mode, image_paths=()):
                sent.append([Path(p).name for p in image_paths])
            with patch.object(anki_blocks, 'PROGRESS', Path(d) / 'p.json'), \
                 patch.object(anki_blocks, 'process', side_effect=fake_process), \
                 patch('sys.argv', ['anki_blocks.py', str(note), 'Deck'] + extra), \
                 patch('sys.stdout', new_callable=io.StringIO) as out:
                anki_blocks.main()
            return sent, out.getvalue()

    def test_blocks_send_their_figures_unless_no_images(self):
        sent, out = self.run_main([])
        self.assertEqual(sent, [['guide-2-1-fig1.svg', 'map 1.png', 'guide-2-1-fig3.svg'], []])
        self.assertIn('(3 figure(s))', out)
        self.assertEqual(self.run_main(['--no-images'])[0], [[], []])

    def test_list_shows_figure_counts(self):
        _, out = self.run_main(['--list'])
        self.assertIn('3 figures', out)


class ResumeTests(unittest.TestCase):
    def run_main(self, progress, outcomes):
        note = progress.parent / 'guide.md'
        note.write_text('### 1.1 A\n\nText a\n\n### 1.2 B\n\nText b\n')
        sent = []
        def fake_process(text, deck, mode, image_paths=()):
            sent.append(text.split('\n')[0])
            return outcomes.pop(0)
        argv = ['anki_blocks.py', str(note), 'Deck']
        with patch.object(anki_blocks, 'PROGRESS', progress), \
             patch.object(anki_blocks, 'process', side_effect=fake_process), \
             patch('sys.argv', argv), patch('sys.stdout', new_callable=io.StringIO) as out:
            anki_blocks.main()
        return sent, out.getvalue(), note

    def test_block_with_skipped_duplicates_is_done_and_counted(self):
        with tempfile.TemporaryDirectory() as d:
            progress = Path(d) / 'progress.json'
            sent, out, note = self.run_main(progress, [{'skipped_duplicates': ['Card X']}, None])
            self.assertEqual(sent, ['### 1.1 A', '### 1.2 B'])
            self.assertIn('1.1: 1 duplicate card(s) skipped', out)
            self.assertIn('1 duplicate card(s) skipped in total', out)
            self.assertEqual(json.loads(progress.read_text())[f'{note.resolve()}::Deck'],
                             ['1.1', '1.2'])
            # Resuming sends nothing again.
            sent, out, _ = self.run_main(progress, [])
            self.assertEqual(sent, [])


if __name__ == '__main__':
    unittest.main()
