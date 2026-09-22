import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
import urllib.error
from unittest.mock import patch, Mock
import anki_gemini as app


def note():
    return dict(note_id=123, model='ScientificBasic',
                fields={'Question': 'What does X do?', 'Answer': 'Old fact', 'Extra': 'Keep me'},
                tags=['original'])


def update():
    return dict(action='update', note_id=123, model='ScientificBasic',
                fields={'Question': 'What does X do?', 'Answer': 'Old fact and new fact'},
                reason='Adds defining feature', evidence='Source says new fact')


def create():
    a = update(); a['action'] = 'create'; del a['note_id']; a['tags'] = ['science']
    return a


class AnkiConnectTests(unittest.TestCase):
    @patch.object(app.urllib.request, 'urlopen')
    def test_connection_refused_explains_how_to_restore_ankiconnect(self, urlopen):
        urlopen.side_effect = urllib.error.URLError(
            ConnectionRefusedError(61, 'Connection refused'))

        with self.assertRaisesRegex(
                RuntimeError,
                r"Cannot reach AnkiConnect for action 'deckNames'.*Open Anki.*installed and enabled"):
            app.anki('deckNames')

    @patch.object(app.urllib.request, 'urlopen', side_effect=TimeoutError())
    def test_timeout_identifies_ankiconnect_action(self, _urlopen):
        with self.assertRaisesRegex(
                RuntimeError, r"AnkiConnect timed out during action 'notesInfo'"):
            app.anki('notesInfo', notes=[123])


class ValidationTests(unittest.TestCase):
    def test_schema_omits_server_rejected_max_items(self):
        schema = app.action_schema(100)
        self.assertNotIn('maxItems', schema['properties']['actions'])

    def test_schema_requires_each_complete_model_field_shape(self):
        fields = app.action_schema(100)['properties']['actions']['items'][
            'properties']['fields']['anyOf']
        self.assertEqual([set(shape['required']) for shape in fields],
                         [set(names) for names in app.FIELDS.values()])
        self.assertTrue(all(shape['additionalProperties'] is False
                            for shape in fields))

    def check_bad(self, action):
        with self.assertRaises(ValueError):
            app.validate({'actions': [action]}, [note()], 100)

    def test_valid_update(self):
        self.assertEqual(app.validate({'actions': [update()]}, [note()], 100), [update()])

    def test_missing_answer(self):
        a = create(); del a['fields']['Answer']
        with self.assertRaisesRegex(
                ValueError, r'Action 0 \(ScientificBasic\).*missing Answer'):
            app.validate({'actions': [a]}, [note()], 100)

    def test_empty_answer(self):
        a = create(); a['fields']['Answer'] = '<div> </div>'; self.check_bad(a)

    def test_unknown_id(self):
        a = update(); a['note_id'] = 999; self.check_bad(a)

    def test_bool_id(self):
        a = update(); a['note_id'] = True; self.check_bad(a)

    def test_extra_field(self):
        a = update(); a['fields']['Extra'] = 'Oops'; self.check_bad(a)

    def test_tags(self):
        a = create(); a['tags'] = ['two tags']; self.check_bad(a)

    def test_media_removal(self):
        n = note(); n['fields']['Answer'] += '<img src="x.png">[sound:y.mp3]'
        with self.assertRaises(ValueError):
            app.validate({'actions': [update()]}, [n], 100)
        a = update(); a['fields']['Answer'] += '<img src="x.png">[sound:y.mp3]'
        app.validate({'actions': [a]}, [n], 100)

    def test_reverse_leak(self):
        a = create(); a['model'] = 'ScientificTwoSided'
        a['fields'] = {'Front': 'Nucleus (Biology)', 'Front question': 'What is it?',
                       'Back': 'The nucleus contains DNA', 'Back question': 'Which organelle?'}
        self.check_bad(a)

    def test_duplicate_updates_and_creates(self):
        for a in (update(), create()):
            with self.assertRaises(ValueError):
                app.validate({'actions': [a, a]}, [note()], 100)

    def test_limit_and_unknown_action(self):
        with self.assertRaises(ValueError):
            app.validate({'actions': [create()]}, [], 0)
        a = create(); a['action'] = 'delete'; self.check_bad(a)

    def test_review_cannot_write(self):
        a = update(); a['action'] = 'review'; self.check_bad(a)


class ContextTests(unittest.TestCase):
    def test_complete_notes_and_budget(self):
        notes = [dict(note(), note_id=i) for i in range(10)]
        args = NS(context_notes=8, context_tokens=len(app.packed(notes[:3])))
        count = Mock(side_effect=len)
        chosen = app.select_context(notes, args, count)
        self.assertEqual(chosen, notes[:3])
        self.assertLessEqual(count.call_count, 5)

    def test_oversized_first_note(self):
        args = NS(context_notes=80, context_tokens=1)
        self.assertEqual(app.select_context([note()], args, len), [])

    def test_retrieval_round_robin_and_rank(self):
        args = NS(candidates=3)
        def api(action, **params):
            if action == 'findNotes':
                return [1, 2, 3] if 'alpha' in params['query'] else [4, 5]
            self.assertEqual(params['notes'], [3, 5, 2])
            return [dict(noteId=i, modelName='ScientificBasic', tags=[],
                    fields={'Question': {'value': 'beta' if i == 5 else 'other'},
                            'Answer': {'value': 'some text'}}) for i in params['notes']]
        with patch.object(app, 'anki', side_effect=api):
            self.assertEqual(app.retrieve('D', ['alpha', 'beta'], args)[0]['note_id'], 5)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'run.json'
        self.current = note()
        self.writes = []
        self.plan = dict(deck='D', notes=[note()], actions=[update()], status={}, limits={'actions': 100})
        app.save(self.path, self.plan)

    def api(self, action, **params):
        if action == 'findNotes':
            return [123]
        if action == 'notesInfo':
            n = self.current
            return [dict(noteId=n['note_id'], modelName=n['model'], tags=n['tags'],
                         fields={k: {'value': v} for k, v in n['fields'].items()})]
        if action == 'updateNoteFields':
            self.writes.append(params)
            self.assertEqual(json.loads(self.path.read_text())['status']['0'], 'pending')
            self.current['fields'].update(params['note']['fields'])
            return None
        raise AssertionError(action)

    def test_update_preserves_extra_and_resume_skips_done(self):
        with patch.object(app, 'anki', side_effect=self.api):
            app.apply_plan(self.path, {'update'})
            app.apply_plan(self.path, {'update'})
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.writes[0]['note']['fields'], {'Answer': 'Old fact and new fact'})
        self.assertEqual(self.current['fields']['Extra'], 'Keep me')
        self.assertEqual(self.current['tags'], ['original'])

    def test_changed_note_rejected_before_write(self):
        self.current['fields']['Answer'] = 'Manual edit'
        with patch.object(app, 'anki', side_effect=self.api), self.assertRaises(RuntimeError):
            app.apply_plan(self.path, {'update'})
        self.assertFalse(self.writes)

    def test_uncertain_write_never_retried(self):
        def fail(action, **params):
            result = self.api(action, **params)
            if action == 'updateNoteFields':
                raise TimeoutError('Lost response')
            return result
        with patch.object(app, 'anki', side_effect=fail), self.assertRaises(TimeoutError):
            app.apply_plan(self.path, {'update'})
        with patch.object(app, 'anki') as api, self.assertRaises(RuntimeError):
            app.apply_plan(self.path, {'update'})
        api.assert_not_called()

    def test_whole_batch_validated_before_any_write(self):
        bad = create(); bad['fields']['Answer'] = ''
        self.plan['actions'].append(bad); app.save(self.path, self.plan)
        with patch.object(app, 'anki') as api, self.assertRaises(ValueError):
            app.apply_plan(self.path, {'update'})
        api.assert_not_called()

    def test_default_create_only_does_not_update(self):
        with patch.object(app, 'anki') as api:
            app.apply_plan(self.path, {'create'})
        api.assert_not_called()

    def test_selective_apply_leaves_other_actions_unapplied(self):
        with patch.object(app, 'anki') as api:
            app.apply_plan(self.path, {'update'}, only=[])
        api.assert_not_called()
        with patch.object(app, 'anki') as api, self.assertRaises(ValueError):
            app.apply_plan(self.path, {'update'}, only=[4])
        api.assert_not_called()

    def test_create_records_id_and_tags(self):
        self.plan['actions'] = [create()]; app.save(self.path, self.plan)
        def api(action, **params):
            if action == 'addNote':
                self.assertEqual(params['note']['tags'], ['gemini_auto', 'science'])
                self.assertFalse(params['note']['options']['allowDuplicate'])
                self.current['fields'].update(params['note']['fields'])
                return 123
            return self.api(action, **params)
        with patch.object(app, 'anki', side_effect=api):
            app.apply_plan(self.path, {'create'})
        journal = json.loads(self.path.read_text())
        self.assertEqual(journal['created_ids']['0'], 123)
        self.assertEqual(journal['status']['0'], 'done')

    def test_lock_prevents_concurrent_apply(self):
        self.path.with_suffix('.lock').touch()
        with patch.object(app, 'anki') as api, self.assertRaises(FileExistsError):
            app.apply_plan(self.path, {'update'})
        api.assert_not_called()


class GenerationTests(unittest.TestCase):
    def gemini(self, finish='STOP'):
        g = app.Gemini.__new__(app.Gemini)
        g.args = NS(request_tokens=128000)
        g.calls = 0; g.usage = []
        g.types = NS(ThinkingConfig=lambda **kw: kw,
                     AutomaticFunctionCallingConfig=lambda **kw: kw,
                     GenerateContentConfig=lambda **kw: kw)
        g.client = NS(models=Mock())
        g.client.models.count_tokens.return_value = NS(total_tokens=100)
        g.client.models.generate_content.return_value = NS(text='{"actions": []}',
                    candidates=[NS(finish_reason=finish)], usage_metadata=None)
        return g

    def test_call_cap_and_config(self):
        g = self.gemini()
        for i in range(2):
            g.generate('model', ['input'], 'rules', {}, 24000, extraction=i == 0)
        with self.assertRaises(RuntimeError):
            g.generate('model', ['input'], 'rules', {}, 24000)
        self.assertEqual(g.client.models.generate_content.call_count, 2)
        cfg = g.client.models.generate_content.call_args.kwargs['config']
        self.assertEqual(cfg['max_output_tokens'], 24000)
        self.assertEqual(cfg['thinking_config'], {'thinking_level': 'low'})
        self.assertEqual(cfg['automatic_function_calling'], {'disable': True})

    def test_truncated_response_rejected(self):
        with self.assertRaises(RuntimeError):
            self.gemini('MAX_TOKENS').generate('model', ['input'], 'rules', {}, 24000)

    def test_request_limit_prevents_generation(self):
        g = self.gemini(); g.args.request_tokens = 100
        with self.assertRaises(ValueError):
            g.generate('model', ['input'], 'rules', {}, 24000)
        g.client.models.generate_content.assert_not_called()

    def test_count_error_identifies_stage_and_model(self):
        g = self.gemini()
        g.client.models.count_tokens.side_effect = ValueError('bad argument')
        with self.assertRaisesRegex(
                RuntimeError,
                'Gemini retrieved-note context token counting failed for model model: bad argument'):
            g.count('model', ['input'], stage='retrieved-note context token counting')

    def test_generation_error_identifies_stage_and_model(self):
        g = self.gemini()
        g.client.models.generate_content.side_effect = ValueError('bad argument')
        with self.assertRaisesRegex(
                RuntimeError,
                'Gemini card comparison generation failed for model model: bad argument'):
            g.generate('model', ['input'], 'rules', {}, 24000)


class PipelineTests(unittest.TestCase):
    def test_preview_and_apply_need_no_live_services(self):
        with tempfile.TemporaryDirectory() as directory:
            args = ['anki_gemini.py', 'D', '--preview-only']
            g = Mock()
            g.count.return_value = 10
            g.generate.side_effect = [['X'], {'actions': [update()]}]
            g.usage = [{'model': 'fake', 'usage': {}}]
            def api(action, **params):
                if action == 'deckNames':
                    return ['D']
                if action == 'modelFieldNames':
                    return app.FIELDS[params['modelName']]
                raise AssertionError('Preview attempted an Anki write')
            with patch.object(app.sys, 'argv', args), patch.object(app, 'STATE', Path(directory)), \
                 patch.object(app, 'Gemini', return_value=g), patch.object(app, 'anki', side_effect=api), \
                 patch.object(app, 'clipboard', return_value=('new fact', None)), \
                 patch.object(app, 'retrieve', return_value=[note()]):
                app.main()
            self.assertEqual(g.generate.call_count, 2)
            proposal = next(Path(directory).glob('*.json'))
            preview = proposal.with_suffix('.txt').read_text()
            self.assertIn('BEFORE: Old fact', preview)
            self.assertIn('AFTER: Old fact and new fact', preview)
            with patch.object(app.sys, 'argv', ['anki_gemini.py', '--apply', str(proposal)]), \
                 patch.object(app, 'Gemini') as constructor, patch.object(app, 'apply_plan') as apply:
                app.main()
            constructor.assert_not_called()
            apply.assert_called_once_with(proposal, {'create', 'update'}, None)

    def test_search_failure_does_not_become_empty_context(self):
        with patch.object(app, 'anki', side_effect=RuntimeError('Unavailable')), self.assertRaises(RuntimeError):
            app.retrieve('D', ['term'], NS(candidates=1000))


if __name__ == '__main__':
    unittest.main()
