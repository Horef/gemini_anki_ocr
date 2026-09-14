import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from io import StringIO
import anki_gemini as app
from anki_review import readable, details


def proposal():
    return {'deck': 'D', 'notes': [], 'status': {}, 'limits': {'actions': 100},
            'actions': [{'action': 'create', 'model': 'ScientificBasic',
                         'fields': {'Question': 'Why X?', 'Answer': 'Because Y.'},
                         'reason': 'New fact', 'evidence': 'Source Y'}]}


def generation(question='Why X?', deck='D'):
    plan = proposal()
    plan.update(version=2, deck=deck, usage=[{'model': 'fake'}],
                runs=[{'action_count': 1}], created_at='now', updated_at='now')
    plan['actions'][0]['fields']['Question'] = question
    return plan


class JournalAggregationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = patch.object(app, 'STATE', self.root); self.state.start()
        self.addCleanup(self.state.stop)

    def test_generations_before_review_share_one_journal(self):
        path, indices = app.store_generation(generation())
        same_path, new_indices = app.store_generation(generation('Why Z?'))

        self.assertEqual(same_path, path)
        self.assertEqual(indices, [0])
        self.assertEqual(new_indices, [1])
        self.assertEqual(len(list(self.root.glob('*.json'))), 1)
        saved = json.loads(path.read_text())
        self.assertEqual([a['fields']['Question'] for a in saved['actions']],
                         ['Why X?', 'Why Z?'])
        self.assertEqual(len(saved['runs']), 2)
        self.assertIn('Why X?', path.with_suffix('.txt').read_text())
        self.assertIn('Why Z?', path.with_suffix('.txt').read_text())

    def test_review_closes_journal_and_next_generation_starts_another(self):
        first, _ = app.store_generation(generation())
        with patch('anki_review.review_dialog', return_value=('discard', [])):
            app.review_latest()
        second, _ = app.store_generation(generation('Why Z?'))

        self.assertNotEqual(second, first)
        self.assertTrue((self.root / 'archive' / first.name).exists())
        self.assertEqual(list(self.root.glob('*.json')), [second])

    def test_different_decks_keep_separate_journals(self):
        first, _ = app.store_generation(generation())
        second, _ = app.store_generation(generation('Why Z?', 'Other'))
        self.assertNotEqual(first, second)
        self.assertEqual(len(list(self.root.glob('*.json'))), 2)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = patch.object(app, 'STATE', self.root); self.state.start()
        self.addCleanup(self.state.stop)
        self.path = self.root / '20260905T100000-test.json'
        app.save(self.path, proposal())
        self.path.with_suffix('.txt').write_text('Preview')

    def test_latest_uses_creation_name_not_apply_mtime_and_filters_deck(self):
        newer = self.root / '20260905T110000-test.json'
        p = proposal(); p['deck'] = 'Other'; app.save(newer, p)
        os.utime(self.path, (9999999999, 9999999999))
        self.assertEqual(app.queued_plans()[0][0], newer)
        self.assertEqual(app.queued_plans('D')[0][0], self.path)

    def test_review_summary_identifies_cards_and_caps_long_batches(self):
        p = proposal()
        p['actions'] *= 7
        output = StringIO()
        with patch('sys.stdout', output):
            app.print_review_summary(p)
        text = output.getvalue()
        self.assertEqual(text.count('• create: Why X?'), 5)
        self.assertIn('• …and 2 more', text)

    def test_later_keeps_everything(self):
        with patch('anki_review.review_dialog', return_value=('later', [])), patch.object(app, 'anki') as api:
            app.review_latest()
        api.assert_not_called()
        self.assertTrue(self.path.exists())
        self.assertTrue(self.path.with_suffix('.txt').exists())
        self.assertFalse(self.path.with_suffix('.lock').exists())

    def test_discard_archives_without_touching_anki(self):
        with patch('anki_review.review_dialog', return_value=('discard', [])), patch.object(app, 'anki') as api:
            app.review_latest()
        api.assert_not_called()
        archived = self.root / 'archive' / self.path.name
        self.assertEqual(json.loads(archived.read_text())['status'], {'0': 'dismissed'})
        self.assertFalse(self.path.exists())
        self.assertTrue(archived.with_suffix('.txt').exists())
        self.assertEqual(app.queued_plans(), [])

    def test_apply_selected_marks_remainder_dismissed(self):
        p = proposal(); p['actions'].append({**p['actions'][0], 'fields': {'Question': 'Why Z?', 'Answer': 'Y'}})
        app.save(self.path, p)
        def apply(path, kinds, indices):
            self.assertEqual(indices, [0])
            self.assertTrue(path.with_suffix('.lock').exists())
            p = json.loads(path.read_text()); p['status']['0'] = 'done'; app.save(path, p)
        with patch('anki_review.review_dialog', return_value=('apply', [0])), patch.object(app, '_apply_locked', side_effect=apply):
            app.review_latest()
        p = json.loads((self.root / 'archive' / self.path.name).read_text())
        self.assertEqual(p['status'], {'0': 'done', '1': 'dismissed'})

    def test_apply_failure_keeps_batch_and_does_not_dismiss(self):
        with patch('anki_review.review_dialog', return_value=('apply', [0])), \
             patch.object(app, '_apply_locked', side_effect=RuntimeError('Stale note')), self.assertRaises(RuntimeError):
            app.review_latest()
        self.assertTrue(self.path.exists())
        self.assertEqual(json.loads(self.path.read_text())['status'], {})

    def test_uncertain_write_cannot_be_discarded_or_retried(self):
        p = proposal(); p['status']['0'] = 'pending'; app.save(self.path, p)
        with patch('anki_review.review_dialog') as dialog, self.assertRaises(RuntimeError):
            app.review_latest()
        dialog.assert_not_called()
        self.assertTrue(self.path.exists())

    def test_review_holds_lock_while_user_decides(self):
        def dialog(*args):
            with self.assertRaises(FileExistsError):
                app.apply_plan(self.path, {'create'})
            return 'later', []
        with patch('anki_review.review_dialog', side_effect=dialog):
            app.review_latest()

    def test_done_runs_not_selected(self):
        p = proposal(); p['status']['0'] = 'done'; app.save(self.path, p)
        self.assertEqual(app.queued_plans(), [])
        self.assertTrue(app.archive_if_finished(self.path))

    def test_review_flags_stay_queued_in_automatic_mode(self):
        p = proposal(); p['actions'] = [{'action': 'review', 'reason': 'Conflict', 'evidence': 'Two claims'}]
        app.save(self.path, p)
        self.assertFalse(app.archive_if_finished(self.path))

    def test_empty_queue_does_not_launch_window_or_gemini(self):
        with patch('anki_review.review_dialog') as dialog, patch.object(app, 'Gemini') as gemini:
            app.review_latest('No such deck')
        dialog.assert_not_called(); gemini.assert_not_called()

    def test_archived_dismissed_actions_cannot_be_applied(self):
        with patch('anki_review.review_dialog', return_value=('discard', [])):
            app.review_latest()
        with patch.object(app, 'anki') as api:
            app.apply_plan(self.root / 'archive' / self.path.name, {'create'})
        api.assert_not_called()

    def test_conflicting_modes_fail_before_external_calls(self):
        with patch.object(app.sys, 'argv', ['anki_gemini.py', '--auto-apply', '--preview-only']), \
             patch.object(app, 'anki') as api, self.assertRaises(ValueError):
            app.main()
        api.assert_not_called()

    def test_review_command_does_not_read_clipboard(self):
        with patch.object(app.sys, 'argv', ['anki_gemini.py', '--review-latest']), \
             patch.object(app, 'review_latest') as review, patch.object(app, 'clipboard') as clip:
            app.main()
        review.assert_called_once_with(None); clip.assert_not_called()

    def test_readable_preserves_media_reference_and_line_breaks(self):
        self.assertEqual(readable('<div>A</div><div>B</div>'), 'A\nB')
        self.assertIn('x.png', readable('<img src="x.png">'))
        self.assertIn('Source evidence', details(proposal(), 0))


class AutomaticPipelineTests(unittest.TestCase):
    def run_mode(self, mode, actions):
        from unittest.mock import Mock
        g = Mock(); g.count.return_value = 10
        g.generate.side_effect = [['X'], {'actions': actions}]; g.usage = []
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        def api(action, **params):
            if action == 'deckNames':
                return ['D']
            if action == 'modelFieldNames':
                return app.FIELDS[params['modelName']]
            raise AssertionError(action)
        seen = []
        def apply(path, kinds, only=None):
            seen.append(kinds)
            plan = json.loads(path.read_text())
            for index, a in enumerate(plan['actions']):
                if a['action'] in kinds and (only is None or index in only):
                    plan['status'][str(index)] = 'done'
            app.save(path, plan)
        argv = ['anki_gemini.py', 'D', '--quiet'] + ([mode] if mode else [])
        with patch.object(app.sys, 'argv', argv), patch.object(app, 'STATE', root), \
             patch.object(app, 'Gemini', return_value=g), patch.object(app, 'anki', side_effect=api), \
             patch.object(app, 'clipboard', return_value=('X', None)), \
             patch.object(app, 'retrieve', return_value=[]), patch.object(app, 'apply_plan', side_effect=apply):
            app.main()
        self.assertEqual(g.generate.call_count, 2)
        return root, seen

    def test_auto_applies_both_kinds_and_archives_completed_batch(self):
        root, seen = self.run_mode('--auto-apply', proposal()['actions'])
        self.assertEqual(seen, [{'create', 'update'}])
        self.assertEqual(list(root.glob('*.json')), [])
        self.assertEqual(len(list((root / 'archive').glob('*.json'))), 1)

    def test_auto_leaves_conflicts_in_queue(self):
        actions = proposal()['actions'] + [{'action': 'review', 'reason': 'Conflict', 'evidence': 'X'}]
        root, seen = self.run_mode('--auto-apply', actions)
        self.assertEqual(len(list(root.glob('*.json'))), 1)
        self.assertFalse((root / 'archive').exists())

    def test_preview_makes_no_writes_and_normal_mode_is_compatible(self):
        root, seen = self.run_mode('--preview-only', proposal()['actions'])
        self.assertFalse(seen)
        self.assertEqual(len(list(root.glob('*.json'))), 1)
        root, seen = self.run_mode(None, proposal()['actions'])
        self.assertEqual(seen, [{'create'}])

    def test_automatic_run_does_not_apply_older_actions_in_shared_journal(self):
        from unittest.mock import Mock
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        with patch.object(app, 'STATE', root):
            path, _ = app.store_generation(generation())
        newer = generation('Why Z?')['actions'][0]
        g = Mock(); g.count.return_value = 10
        g.generate.side_effect = [['Z'], {'actions': [newer]}]; g.usage = []

        def api(action, **params):
            if action == 'deckNames':
                return ['D']
            if action == 'modelFieldNames':
                return app.FIELDS[params['modelName']]
            raise AssertionError(action)

        applied = []
        def apply(saved_path, kinds, only=None):
            applied.append(only)
            plan = json.loads(saved_path.read_text())
            plan['status'][str(only[0])] = 'done'
            app.save(saved_path, plan)

        with patch.object(app.sys, 'argv', ['anki_gemini.py', 'D', '--auto-apply', '--quiet']), \
             patch.object(app, 'STATE', root), patch.object(app, 'Gemini', return_value=g), \
             patch.object(app, 'anki', side_effect=api), \
             patch.object(app, 'clipboard', return_value=('Z', None)), \
             patch.object(app, 'retrieve', return_value=[]), \
             patch.object(app, 'apply_plan', side_effect=apply):
            app.main()

        self.assertEqual(applied, [[1]])
        saved = json.loads(path.read_text())
        self.assertNotIn('0', saved['status'])
        self.assertEqual(saved['status']['1'], 'done')


if __name__ == '__main__':
    unittest.main()
