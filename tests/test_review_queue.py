import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from io import StringIO

from ankigen import anki_connect, cli, journal
from ankigen.review_window import readable, details

DIALOG = 'ankigen.review_window.review_dialog'


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


class StateMixin:
    def use_temp_state(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        state = patch.object(journal, 'STATE', self.root); state.start()
        self.addCleanup(state.stop)


class JournalAggregationTests(StateMixin, unittest.TestCase):
    def setUp(self):
        self.use_temp_state()

    def test_generations_before_review_share_one_journal(self):
        path, indices = journal.store_generation(generation())
        same_path, new_indices = journal.store_generation(generation('Why Z?'))

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
        first, _ = journal.store_generation(generation())
        with patch(DIALOG, return_value=('discard', [])):
            journal.review_latest()
        second, _ = journal.store_generation(generation('Why Z?'))

        self.assertNotEqual(second, first)
        self.assertTrue((self.root / 'archive' / first.name).exists())
        self.assertEqual(list(self.root.glob('*.json')), [second])

    def test_different_decks_keep_separate_journals(self):
        first, _ = journal.store_generation(generation())
        second, _ = journal.store_generation(generation('Why Z?', 'Other'))
        self.assertNotEqual(first, second)
        self.assertEqual(len(list(self.root.glob('*.json'))), 2)

    def test_queued_creates_exclude_other_decks_and_closed_actions(self):
        path, _ = journal.store_generation(generation())
        journal.store_generation(generation('Why Z?'))
        journal.store_generation(generation('Elsewhere?', 'Other'))
        plan = json.loads(path.read_text()); plan['status']['0'] = 'dismissed'
        journal.save(path, plan)
        self.assertEqual([a['fields']['Question'] for a in journal.queued_creates('D')],
                         ['Why Z?'])


class ReviewTests(StateMixin, unittest.TestCase):
    def setUp(self):
        self.use_temp_state()
        self.path = self.root / '20260905T100000-test.json'
        journal.save(self.path, proposal())
        self.path.with_suffix('.txt').write_text('Preview')

    def test_latest_uses_creation_name_not_apply_mtime_and_filters_deck(self):
        newer = self.root / '20260905T110000-test.json'
        p = proposal(); p['deck'] = 'Other'; journal.save(newer, p)
        os.utime(self.path, (9999999999, 9999999999))
        self.assertEqual(journal.queued_plans()[0][0], newer)
        self.assertEqual(journal.queued_plans('D')[0][0], self.path)

    def test_review_summary_identifies_cards_and_caps_long_batches(self):
        p = proposal()
        p['actions'] *= 7
        output = StringIO()
        with patch('sys.stdout', output):
            journal.print_review_summary(p)
        text = output.getvalue()
        self.assertEqual(text.count('• create: Why X?'), 5)
        self.assertIn('• …and 2 more', text)

    def test_later_keeps_everything(self):
        with patch(DIALOG, return_value=('later', [])), patch.object(anki_connect, 'request') as api:
            journal.review_latest()
        api.assert_not_called()
        self.assertTrue(self.path.exists())
        self.assertTrue(self.path.with_suffix('.txt').exists())
        self.assertFalse(self.path.with_suffix('.lock').exists())

    def test_discard_archives_without_touching_anki(self):
        with patch(DIALOG, return_value=('discard', [])), patch.object(anki_connect, 'request') as api:
            journal.review_latest()
        api.assert_not_called()
        archived = self.root / 'archive' / self.path.name
        self.assertEqual(json.loads(archived.read_text())['status'], {'0': 'dismissed'})
        self.assertFalse(self.path.exists())
        self.assertTrue(archived.with_suffix('.txt').exists())
        self.assertEqual(journal.queued_plans(), [])

    def test_apply_selected_marks_remainder_dismissed(self):
        p = proposal(); p['actions'].append({**p['actions'][0], 'fields': {'Question': 'Why Z?', 'Answer': 'Y'}})
        journal.save(self.path, p)
        def apply(path, kinds, indices):
            self.assertEqual(indices, [0])
            self.assertTrue(path.with_suffix('.lock').exists())
            p = json.loads(path.read_text()); p['status']['0'] = 'done'; journal.save(path, p)
        with patch(DIALOG, return_value=('apply', [0])), patch.object(journal, '_apply_locked', side_effect=apply):
            journal.review_latest()
        p = json.loads((self.root / 'archive' / self.path.name).read_text())
        self.assertEqual(p['status'], {'0': 'done', '1': 'dismissed'})

    def test_apply_failure_keeps_batch_and_does_not_dismiss(self):
        with patch(DIALOG, return_value=('apply', [0])), \
             patch.object(journal, '_apply_locked', side_effect=RuntimeError('Stale note')), \
             self.assertRaises(RuntimeError):
            journal.review_latest()
        self.assertTrue(self.path.exists())
        self.assertEqual(json.loads(self.path.read_text())['status'], {})

    def test_uncertain_write_cannot_be_discarded_or_retried(self):
        p = proposal(); p['status']['0'] = 'pending'; journal.save(self.path, p)
        with patch(DIALOG) as dialog, self.assertRaises(RuntimeError):
            journal.review_latest()
        dialog.assert_not_called()
        self.assertTrue(self.path.exists())

    def test_review_holds_lock_while_user_decides(self):
        def dialog(*args):
            with self.assertRaises(FileExistsError):
                journal.apply_plan(self.path, {'create'})
            return 'later', []
        with patch(DIALOG, side_effect=dialog):
            journal.review_latest()

    def test_done_runs_not_selected(self):
        p = proposal(); p['status']['0'] = 'done'; journal.save(self.path, p)
        self.assertEqual(journal.queued_plans(), [])
        self.assertTrue(journal.archive_if_finished(self.path))

    def test_review_flags_stay_queued_in_automatic_mode(self):
        p = proposal(); p['actions'] = [{'action': 'review', 'reason': 'Conflict', 'evidence': 'Two claims'}]
        journal.save(self.path, p)
        self.assertFalse(journal.archive_if_finished(self.path))

    def test_empty_queue_does_not_launch_window_or_gemini(self):
        with patch(DIALOG) as dialog, patch.object(cli, 'Gemini') as gemini:
            journal.review_latest('No such deck')
        dialog.assert_not_called(); gemini.assert_not_called()

    def test_archived_dismissed_actions_cannot_be_applied(self):
        with patch(DIALOG, return_value=('discard', [])):
            journal.review_latest()
        with patch.object(anki_connect, 'request') as api:
            journal.apply_plan(self.root / 'archive' / self.path.name, {'create'})
        api.assert_not_called()

    def test_conflicting_modes_fail_before_external_calls(self):
        for modes in (['--auto-apply', '--preview-only'], ['--sync-index', '--preview-only']):
            with patch.object(cli.sys, 'argv', ['anki_gemini.py'] + modes), \
                 patch.object(anki_connect, 'request') as api, self.assertRaises(ValueError):
                cli.main()
            api.assert_not_called()

    def test_review_command_does_not_read_clipboard(self):
        with patch.object(cli.sys, 'argv', ['anki_gemini.py', '--review-latest']), \
             patch.object(cli, 'review_latest') as review, patch.object(cli, 'clipboard') as clip:
            cli.main()
        review.assert_called_once_with(None); clip.assert_not_called()

    def test_readable_preserves_media_reference_and_line_breaks(self):
        self.assertEqual(readable('<div>A</div><div>B</div>'), 'A\nB')
        self.assertIn('x.png', readable('<img src="x.png">'))
        self.assertIn('Source evidence', details(proposal(), 0))


class AutomaticPipelineTests(StateMixin, unittest.TestCase):
    def setUp(self):
        self.use_temp_state()

    def api(self, action, **params):
        if action == 'deckNames':
            return ['D']
        if action == 'modelFieldNames':
            return anki_connect.FIELDS[params['modelName']]
        raise AssertionError(action)

    def run_main(self, argv, actions, apply):
        g = Mock(); g.count.return_value = 10
        g.generate.return_value = {'actions': actions}; g.usage = []
        with patch.object(cli.sys, 'argv', ['anki_gemini.py'] + argv), \
             patch.object(cli, 'Gemini', return_value=g), \
             patch.object(anki_connect, 'request', side_effect=self.api), \
             patch.object(cli, 'clipboard', return_value=('X', None)), \
             patch.object(cli, 'retrieve', return_value=([], [], {})), \
             patch.object(cli, 'apply_plan', side_effect=apply):
            cli.main()
        self.assertEqual(g.generate.call_count, 1)

    def run_mode(self, mode, actions):
        seen = []
        def apply(path, kinds, only=None):
            seen.append(kinds)
            plan = json.loads(path.read_text())
            for index, a in enumerate(plan['actions']):
                if a['action'] in kinds and (only is None or index in only):
                    plan['status'][str(index)] = 'done'
            journal.save(path, plan)
        for old in self.root.glob('*.json'):
            old.unlink()
        self.run_main(['D', '--quiet'] + ([mode] if mode else []), actions, apply)
        return self.root, seen

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
        path, _ = journal.store_generation(generation())
        newer = generation('Why Z?')['actions'][0]
        applied = []
        def apply(saved_path, kinds, only=None):
            applied.append(only)
            plan = json.loads(saved_path.read_text())
            plan['status'][str(only[0])] = 'done'
            journal.save(saved_path, plan)
        self.run_main(['D', '--auto-apply', '--quiet'], [newer], apply)
        self.assertEqual(applied, [[1]])
        saved = json.loads(path.read_text())
        self.assertNotIn('0', saved['status'])
        self.assertEqual(saved['status']['1'], 'done')


if __name__ == '__main__':
    unittest.main()
