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


class ResumeTests(unittest.TestCase):
    def run_main(self, progress, outcomes):
        note = progress.parent / 'guide.md'
        note.write_text('### 1.1 A\n\nText a\n\n### 1.2 B\n\nText b\n')
        sent = []
        def fake_process(text, deck, mode):
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
