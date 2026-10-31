import unittest

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


if __name__ == '__main__':
    unittest.main()
