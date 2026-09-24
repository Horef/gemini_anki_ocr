import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
import urllib.error
from unittest.mock import patch, Mock

from ankigen import actions, anki_connect, cli, journal, llm


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
    @patch.object(anki_connect.urllib.request, 'urlopen')
    def test_connection_refused_explains_how_to_restore_ankiconnect(self, urlopen):
        urlopen.side_effect = urllib.error.URLError(
            ConnectionRefusedError(61, 'Connection refused'))

        with self.assertRaisesRegex(
                RuntimeError,
                r"Cannot reach AnkiConnect for action 'deckNames'.*Open Anki.*installed and enabled"):
            anki_connect.request('deckNames')

    @patch.object(anki_connect.urllib.request, 'urlopen', side_effect=TimeoutError())
    def test_timeout_identifies_ankiconnect_action(self, _urlopen):
        with self.assertRaisesRegex(
                RuntimeError, r"AnkiConnect timed out during action 'notesInfo'"):
            anki_connect.request('notesInfo', notes=[123])

    def test_mod_times_falls_back_when_action_is_unsupported(self):
        with patch.object(anki_connect, 'request',
                          side_effect=RuntimeError('notesModTime: unsupported action')):
            self.assertIsNone(anki_connect.mod_times([1]))
        with patch.object(anki_connect, 'request', side_effect=RuntimeError('Anki closed')), \
                self.assertRaises(RuntimeError):
            anki_connect.mod_times([1])

    def test_fetch_notes_rejects_disappeared_notes(self):
        with patch.object(anki_connect, 'request', return_value=[{}]), \
                self.assertRaises(RuntimeError):
            anki_connect.fetch_notes([1])


class ValidationTests(unittest.TestCase):
    def test_schema_omits_server_rejected_max_items(self):
        self.assertNotIn('maxItems', actions.action_schema()['properties']['actions'])

    def test_schema_requires_each_complete_model_field_shape(self):
        fields = actions.action_schema()['properties']['actions']['items'][
            'properties']['fields']['anyOf']
        self.assertEqual([set(shape['required']) for shape in fields],
                         [set(names) for names in anki_connect.FIELDS.values()])
        self.assertTrue(all(shape['additionalProperties'] is False for shape in fields))

    def check_bad(self, action):
        with self.assertRaises(ValueError):
            actions.validate({'actions': [action]}, [note()], 100)

    def test_valid_update(self):
        self.assertEqual(actions.validate({'actions': [update()]}, [note()], 100), [update()])

    def test_missing_answer(self):
        a = create(); del a['fields']['Answer']
        with self.assertRaisesRegex(
                ValueError, r'Action 0 \(ScientificBasic\).*missing Answer'):
            actions.validate({'actions': [a]}, [note()], 100)

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
            actions.validate({'actions': [update()]}, [n], 100)
        a = update(); a['fields']['Answer'] += '<img src="x.png">[sound:y.mp3]'
        actions.validate({'actions': [a]}, [n], 100)

    def test_reverse_leak(self):
        a = create(); a['model'] = 'ScientificTwoSided'
        a['fields'] = {'Front': 'Nucleus (Biology)', 'Front question': 'What is it?',
                       'Back': 'The nucleus contains DNA', 'Back question': 'Which organelle?'}
        self.check_bad(a)

    def test_duplicate_updates_and_creates(self):
        for a in (update(), create()):
            with self.assertRaises(ValueError):
                actions.validate({'actions': [a, a]}, [note()], 100)

    def test_limit_and_unknown_action(self):
        with self.assertRaises(ValueError):
            actions.validate({'actions': [create()]}, [], 0)
        a = create(); a['action'] = 'delete'; self.check_bad(a)

    def test_review_cannot_write(self):
        a = update(); a['action'] = 'review'; self.check_bad(a)


class ContextTests(unittest.TestCase):
    def test_complete_notes_and_budget(self):
        notes = [dict(note(), note_id=i) for i in range(10)]
        args = NS(context_notes=8, context_tokens=len(anki_connect.packed(notes[:3])))
        count = Mock(side_effect=len)
        chosen = cli.select_context(notes, args, count)
        self.assertEqual(chosen, notes[:3])
        self.assertLessEqual(count.call_count, 5)

    def test_oversized_first_note(self):
        args = NS(context_notes=80, context_tokens=1)
        self.assertEqual(cli.select_context([note()], args, len), [])


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'run.json'
        self.current = note()
        self.writes = []
        self.plan = dict(deck='D', notes=[note()], actions=[update()], status={}, limits={'actions': 100})
        journal.save(self.path, self.plan)

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
        with patch.object(anki_connect, 'request', side_effect=self.api):
            journal.apply_plan(self.path, {'update'})
            journal.apply_plan(self.path, {'update'})
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.writes[0]['note']['fields'], {'Answer': 'Old fact and new fact'})
        self.assertEqual(self.current['fields']['Extra'], 'Keep me')
        self.assertEqual(self.current['tags'], ['original'])

    def test_changed_note_rejected_before_write(self):
        self.current['fields']['Answer'] = 'Manual edit'
        with patch.object(anki_connect, 'request', side_effect=self.api), self.assertRaises(RuntimeError):
            journal.apply_plan(self.path, {'update'})
        self.assertFalse(self.writes)

    def test_uncertain_write_never_retried(self):
        def fail(action, **params):
            result = self.api(action, **params)
            if action == 'updateNoteFields':
                raise TimeoutError('Lost response')
            return result
        with patch.object(anki_connect, 'request', side_effect=fail), self.assertRaises(TimeoutError):
            journal.apply_plan(self.path, {'update'})
        with patch.object(anki_connect, 'request') as api, self.assertRaises(RuntimeError):
            journal.apply_plan(self.path, {'update'})
        api.assert_not_called()

    def test_whole_batch_validated_before_any_write(self):
        bad = create(); bad['fields']['Answer'] = ''
        self.plan['actions'].append(bad); journal.save(self.path, self.plan)
        with patch.object(anki_connect, 'request') as api, self.assertRaises(ValueError):
            journal.apply_plan(self.path, {'update'})
        api.assert_not_called()

    def test_default_create_only_does_not_update(self):
        with patch.object(anki_connect, 'request') as api:
            journal.apply_plan(self.path, {'create'})
        api.assert_not_called()

    def test_selective_apply_leaves_other_actions_unapplied(self):
        with patch.object(anki_connect, 'request') as api:
            journal.apply_plan(self.path, {'update'}, only=[])
        api.assert_not_called()
        with patch.object(anki_connect, 'request') as api, self.assertRaises(ValueError):
            journal.apply_plan(self.path, {'update'}, only=[4])
        api.assert_not_called()

    def test_create_records_id_and_tags(self):
        self.plan['actions'] = [create()]; journal.save(self.path, self.plan)
        def api(action, **params):
            if action == 'addNote':
                self.assertEqual(params['note']['tags'], ['gemini_auto', 'science'])
                self.assertFalse(params['note']['options']['allowDuplicate'])
                self.current['fields'].update(params['note']['fields'])
                return 123
            return self.api(action, **params)
        with patch.object(anki_connect, 'request', side_effect=api):
            journal.apply_plan(self.path, {'create'})
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['created_ids']['0'], 123)
        self.assertEqual(saved['status']['0'], 'done')

    def test_lock_prevents_concurrent_apply(self):
        self.path.with_suffix('.lock').touch()
        with patch.object(anki_connect, 'request') as api, self.assertRaises(FileExistsError):
            journal.apply_plan(self.path, {'update'})
        api.assert_not_called()


class GenerationTests(unittest.TestCase):
    def gemini(self, finish='STOP'):
        g = llm.Gemini.__new__(llm.Gemini)
        g.args = NS(request_tokens=128000, quiet=True)
        g.calls = 0; g.usage = []
        g.types = NS(ThinkingConfig=lambda **kw: kw,
                     AutomaticFunctionCallingConfig=lambda **kw: kw,
                     GenerateContentConfig=lambda **kw: kw)
        g.client = NS(models=Mock())
        g.client.models.count_tokens.return_value = NS(total_tokens=100)
        g.client.models.generate_content.return_value = NS(text='{"actions": []}',
                    candidates=[NS(finish_reason=finish)], usage_metadata=None)
        return g

    def test_single_generation_cap_and_config(self):
        g = self.gemini()
        g.generate('model', ['input'], 'rules', {}, 24000)
        with self.assertRaises(RuntimeError):
            g.generate('model', ['input'], 'rules', {}, 24000)
        self.assertEqual(g.client.models.generate_content.call_count, 1)
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

    def test_embed_requests_one_vector_per_item(self):
        from google.genai import types
        g = self.gemini(); g.types = types
        g.client.models.embed_content.return_value = NS(
            embeddings=[NS(values=[1.0, 0.0]), NS(values=[0.0, 1.0])])
        self.assertEqual(g.embed(['a', 'b'], 'emb', 2), [[1.0, 0.0], [0.0, 1.0]])
        contents = g.client.models.embed_content.call_args.kwargs['contents']
        self.assertEqual(len(contents), 2)
        g.client.models.embed_content.return_value = NS(embeddings=[NS(values=[1.0])])
        with self.assertRaisesRegex(RuntimeError, 'unexpected vectors'):
            g.embed(['a', 'b'], 'emb', 2)

    def test_vertex_mode_embeds_one_content_per_request(self):
        from google.genai import types
        g = self.gemini(); g.types = types; g.client.vertexai = True
        g.client.models.embed_content.return_value = NS(embeddings=[NS(values=[1.0, 0.0])])
        self.assertEqual(len(g.embed(['a', 'b', 'c'], 'gemini-embedding-2', 2)), 3)
        calls = g.client.models.embed_content.call_args_list
        self.assertEqual([len(c.kwargs['contents']) for c in calls], [1, 1, 1])


class PipelineTests(unittest.TestCase):
    def test_preview_and_apply_need_no_live_services(self):
        with tempfile.TemporaryDirectory() as directory:
            args = ['anki_gemini.py', 'D', '--preview-only']
            g = Mock()
            g.count.return_value = 10
            g.generate.return_value = {'actions': [update()]}
            g.usage = [{'model': 'fake', 'usage': {}}]
            def api(action, **params):
                if action == 'deckNames':
                    return ['D']
                if action == 'modelFieldNames':
                    return anki_connect.FIELDS[params['modelName']]
                raise AssertionError('Preview attempted an Anki write')
            with patch.object(cli.sys, 'argv', args), patch.object(journal, 'STATE', Path(directory)), \
                 patch.object(cli, 'Gemini', return_value=g), \
                 patch.object(anki_connect, 'request', side_effect=api), \
                 patch.object(cli, 'clipboard', return_value=('new fact', None)), \
                 patch.object(cli, 'retrieve', return_value=([note()], [])) as retrieve:
                cli.main()
            self.assertEqual(g.generate.call_count, 1)
            self.assertEqual(retrieve.call_args.args[:3], ('D', 'new fact', None))
            payload = json.loads(g.generate.call_args.args[1][-1])
            self.assertNotIn('queued_proposals', payload)
            proposal = next(Path(directory).glob('*.json'))
            preview = proposal.with_suffix('.txt').read_text()
            self.assertIn('BEFORE: Old fact', preview)
            self.assertIn('AFTER: Old fact and new fact', preview)
            with patch.object(cli.sys, 'argv', ['anki_gemini.py', '--apply', str(proposal)]), \
                 patch.object(cli, 'Gemini') as constructor, patch.object(cli, 'apply_plan') as apply:
                cli.main()
            constructor.assert_not_called()
            apply.assert_called_once_with(proposal, {'create', 'update'}, None)

    def test_queued_proposals_are_sent_as_data(self):
        with tempfile.TemporaryDirectory() as directory:
            g = Mock(); g.count.return_value = 10; g.usage = []
            g.generate.return_value = {'actions': []}
            queued = [{'model': 'ScientificBasic', 'fields': {'Question': 'Q', 'Answer': 'A'}}]
            with patch.object(journal, 'STATE', Path(directory)), \
                 patch.object(cli, 'retrieve', return_value=([], queued)):
                cli.generate(g, cli.parser().parse_args(['D', '--preview-only']), 'text', None)
            payload = json.loads(g.generate.call_args.args[1][-1])
            self.assertEqual(payload['queued_proposals'], queued)

    def test_sync_index_makes_no_generation(self):
        g = Mock()
        with patch.object(cli.sys, 'argv', ['anki_gemini.py', '--sync-index']), \
             patch.object(cli, 'Gemini', return_value=g), \
             patch.object(cli, 'build_index') as build, patch.object(cli, 'clipboard') as clip:
            cli.main()
        build.assert_called_once()
        clip.assert_not_called(); g.generate.assert_not_called(); g.client.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
