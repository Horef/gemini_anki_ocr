from contextlib import redirect_stderr
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import numpy as np
from google.genai import types

from ankigen import anki_connect, index, journal, retrieval

CONCEPTS = (('neuro', 'nerve'), ('glucose', 'sugar'))


def vector(item):
    v = np.zeros(retrieval.DIMENSION, dtype=np.float32)
    text = item.casefold() if isinstance(item, str) else 'neuro image'
    for axis, words in enumerate(CONCEPTS):
        v[axis] = float(any(w in text for w in words))
    v[len(CONCEPTS)] = 0.1
    return v.tolist()


class FakeGemini:
    types = types

    def __init__(self, fail=False, max_batch=None):
        self.fail, self.max_batch, self.calls = fail, max_batch, []

    def embed(self, items, model, dimension):
        self.calls.append(list(items))
        if self.fail or (self.max_batch and len(items) > self.max_batch):
            raise RuntimeError('embedding down')
        return [vector(i) for i in items]


def raw(nid, question, answer, model='ScientificBasic'):
    fields = ({'Question': question, 'Answer': answer} if model == 'ScientificBasic' else
              {'Front': question, 'Front question': 'What is it?', 'Back': answer,
               'Back question': 'What is this called?'})
    return dict(noteId=nid, modelName=model, tags=[],
                fields={k: {'value': v} for k, v in fields.items()})


class FakeAnki:
    def __init__(self, notes, deck=None, mod_time=True):
        self.notes = {n['noteId']: n for n in notes}
        self.mods = {nid: 1 for nid in self.notes}
        self.deck = set(self.notes) if deck is None else set(deck)
        self.mod_time, self.fetched = mod_time, []

    def __call__(self, action, **params):
        if action == 'findNotes':
            ids = self.deck if 'deck:' in params['query'] else self.notes
            return sorted(i for i in ids if i in self.notes)
        if action == 'notesModTime':
            if not self.mod_time:
                raise RuntimeError('notesModTime: unsupported action')
            return [{'noteId': i, 'mod': self.mods[i]} for i in params['notes']]
        if action == 'notesInfo':
            self.fetched.extend(params['notes'])
            return [self.notes.get(i, {}) for i in params['notes']]
        raise AssertionError(action)


def args(**overrides):
    return NS(**{**dict(chunks=24, matches_per_query=5, context_notes=30, embed_notes=5000,
                        min_similarity=0.5), **overrides})


class TextTests(unittest.TestCase):
    def test_chunks_are_sentence_aligned_and_bounded(self):
        text = ' '.join(f'Sentence number {i} explains a fact.' for i in range(200))
        out = retrieval.chunks(text, 5)
        self.assertLessEqual(len(out), 5)
        self.assertTrue(all(c.endswith('.') for c in out))
        self.assertEqual(' '.join(out), text)
        self.assertEqual(retrieval.chunks('  \n ', 5), [])
        self.assertEqual(retrieval.chunks('Short. Text.', 5), ['Short. Text.'])

    def test_overlong_sentence_is_split_for_the_embedding_limit(self):
        out = retrieval.chunks('x' * (retrieval.MAX_DOCUMENT_CHARS * 2 + 5), 24)
        self.assertTrue(all(len(c) <= retrieval.MAX_DOCUMENT_CHARS for c in out))

    def test_document_uses_meaningful_fields_only(self):
        doc = retrieval.document('ScientificTwoSided', {
            'Front': '<b>Axon</b>', 'Front question': 'What is it?',
            'Back': 'Projection of a neuron', 'Back question': 'Name?'})
        self.assertEqual(doc, 'title: Axon | text: Projection of a neuron')

    def test_bm25_prefers_rare_matching_terms_deterministically(self):
        bm25 = retrieval.BM25({1: retrieval.tokens('the cell membrane'),
                               2: retrieval.tokens('the mitochondria of the cell'),
                               3: retrieval.tokens('unrelated words')})
        self.assertEqual(bm25.rank('mitochondria cell', 5), [2, 1])
        self.assertEqual(bm25.rank('nothing matches', 5), [])

    def test_fusion_rewards_agreement_and_breaks_ties_by_key(self):
        self.assertEqual(retrieval.fuse([[3, 1], [1, 2]]), [1, 3, 2])
        self.assertEqual(retrieval.fuse([[5], [4]]), [4, 5])


class IndexTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'nested' / 'index.sqlite3'

    def test_file_is_private_and_corruption_rebuilds(self):
        idx = index.Index(self.path); idx.close()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.path.write_bytes(b'not a database' * 100)
        with redirect_stderr(io.StringIO()) as err:
            idx = index.Index(self.path)
        self.assertEqual(idx.mods(), {})
        idx.close()
        self.assertIn('rebuilding', err.getvalue())
        self.assertEqual(len(list(self.path.parent.glob('*.corrupt-*'))), 1)

    def test_vectors_are_normalised_and_pruned(self):
        idx = index.Index(self.path)
        idx.store_vectors([('a', [3.0, 4.0]), ('b', [1.0, 0.0])])
        self.assertTrue(np.allclose(idx.vectors(['a'])['a'], [0.6, 0.8]))
        self.assertEqual(idx.prune_vectors({'a'}), 1)
        self.assertEqual(set(idx.vectors(['a', 'b'])), {'a'})
        with self.assertRaises(ValueError):
            idx.store_vectors([('z', [0.0, 0.0])])
        idx.close()


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        state = patch.object(journal, 'STATE', self.root / 'runs'); state.start()
        self.addCleanup(state.stop)
        self.anki = FakeAnki([raw(1, 'What is a nerve cell?', 'A cell transmitting impulses'),
                              raw(2, 'What is blood sugar?', 'Circulating carbohydrate'),
                              raw(3, 'Axon', 'Projection', 'ScientificTwoSided'),
                              raw(9, 'Other deck', 'Nerve fact')], deck=[1, 2, 3])
        api = patch.object(anki_connect, 'request', side_effect=self.anki); api.start()
        self.addCleanup(api.stop)

    def retrieve(self, gemini, text='Neurons transmit signals.', image=None, **kw):
        idx = index.Index(self.root / 'index.sqlite3')
        with redirect_stderr(io.StringIO()) as err:
            result = retrieval.retrieve('D', text, image, gemini, args(**kw), idx)
        return result, err.getvalue()

    def test_semantic_match_without_shared_words_and_deck_scope(self):
        (notes, queued), _ = self.retrieve(FakeGemini())
        ids = [n['note_id'] for n in notes]
        self.assertEqual(ids[0], 1)
        self.assertNotIn(9, ids)
        self.assertEqual(queued, [])
        self.assertEqual(notes[0]['fields']['Answer'], 'A cell transmitting impulses')

    def test_unchanged_notes_are_neither_refetched_nor_reembedded(self):
        first = FakeGemini(); self.retrieve(first)
        self.assertEqual(sum(len(c) for c in first.calls), 3 + 1)
        self.anki.fetched.clear()
        second = FakeGemini(); self.retrieve(second)
        self.assertEqual(second.calls, [[retrieval.query_text('Neurons transmit signals.')]])
        self.assertEqual(sorted(self.anki.fetched), [1])  # only the final fresh snapshot

    def test_changed_and_deleted_notes_are_synchronised(self):
        self.retrieve(FakeGemini())
        self.anki.notes[2] = raw(2, 'What is blood glucose?', 'Changed')
        self.anki.mods[2] = 2
        del self.anki.notes[3]
        gemini = FakeGemini(); self.retrieve(gemini)
        self.assertEqual(len(gemini.calls[0]), 1)
        idx = index.Index(self.root / 'index.sqlite3')
        self.assertEqual(set(idx.mods()), {1, 2, 9})
        self.assertIn('glucose', idx.notes([2])[2]['fields']['Question'])
        idx.close()

    def test_without_notes_mod_time_content_digest_still_avoids_reembedding(self):
        self.anki.mod_time = False
        self.retrieve(FakeGemini())
        gemini = FakeGemini(); self.retrieve(gemini)
        self.assertEqual(len(gemini.calls), 1)

    def test_embedding_failure_falls_back_to_keywords(self):
        (notes, _), err = self.retrieve(FakeGemini(fail=True), text='What is blood sugar?')
        self.assertEqual(notes[0]['note_id'], 2)
        self.assertIn('keyword ranking only', err)

    def test_image_only_without_semantic_search_stops(self):
        with self.assertRaisesRegex(RuntimeError, 'Nothing was generated'):
            self.retrieve(FakeGemini(fail=True), text='', image=b'png')

    def test_image_only_uses_multimodal_query(self):
        (notes, _), _ = self.retrieve(FakeGemini(), text='', image=b'png')
        self.assertEqual(notes[0]['note_id'], 1)

    def test_single_input_gateway_is_probed_then_used_singly(self):
        gemini = FakeGemini(max_batch=1)
        (notes, _), _ = self.retrieve(gemini)
        self.assertEqual(notes[0]['note_id'], 1)
        self.assertEqual([len(c) for c in gemini.calls], [3, 1, 1, 1, 1])

    def test_embedding_cap_leaves_rest_to_keywords(self):
        (notes, _), err = self.retrieve(FakeGemini(), embed_notes=1)
        self.assertIn('covers 1/3', err)
        self.assertTrue(notes)

    def test_queued_proposals_are_ranked_but_not_returned_as_notes(self):
        plan = {'deck': 'D', 'notes': [], 'status': {}, 'limits': {'actions': 10},
                'version': 2, 'actions': [{'action': 'create', 'model': 'ScientificBasic',
                'fields': {'Question': 'How do neurons signal?', 'Answer': 'Spikes'},
                'reason': 'r', 'evidence': 'e'}]}
        journal.store_generation(plan)
        (notes, queued), _ = self.retrieve(FakeGemini())
        self.assertTrue(all(n['note_id'] > 0 for n in notes))
        self.assertEqual(queued, [{'model': 'ScientificBasic', 'fields': plan['actions'][0]['fields']}])

    def test_media_only_notes_are_not_embedded_or_ranked(self):
        self.anki.notes[4] = raw(4, '<img src="a.png">', '<img src="b.png">')
        self.anki.mods[4] = 1; self.anki.deck.add(4)
        gemini = FakeGemini()
        (notes, _), _ = self.retrieve(gemini)
        self.assertNotIn(4, [n['note_id'] for n in notes])
        self.assertEqual(len(gemini.calls[0]), 3)

    def test_anki_failure_is_not_treated_as_no_matches(self):
        with patch.object(anki_connect, 'request', side_effect=RuntimeError('Anki closed')), \
                self.assertRaises(RuntimeError):
            self.retrieve(FakeGemini())

    def test_build_index_embeds_everything(self):
        gemini = FakeGemini()
        with redirect_stderr(io.StringIO()), patch('sys.stdout', io.StringIO()) as out:
            retrieval.build_index(gemini, args(), index.Index(self.root / 'index.sqlite3'))
        self.assertEqual(sum(len(c) for c in gemini.calls), 4)
        self.assertIn('4 notes, 4 refreshed, 4 embedded, 0 still waiting', out.getvalue())


if __name__ == '__main__':
    unittest.main()
