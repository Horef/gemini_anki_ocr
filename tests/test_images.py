"""Images on cards: rendering, loading, validation, journals, upload. Offline: a fake
renderer replaces Chrome (one test renders for real and is skipped without Chrome)."""
import base64
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch
import unittest

from ankigen import actions, anki_connect, cli, images, journal
from tests.test_anki_gemini import create, note, update

PNG = b'\x89PNG\r\n\x1a\nfake-figure'
SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 30 20"><rect width="30" height="20" fill="red"/></svg>'


def fake_render(svg, scale=2.0):
    return PNG + svg.encode()[:8]


def with_image(action, name, field='Question'):
    action['fields'][field] = f'<img src="{name}"> ' + action['fields'][field]
    return action


class RenderTests(unittest.TestCase):
    def test_size_from_viewbox_width_and_height(self):
        self.assertEqual(images.svg_size(SVG), (30.0, 20.0))
        self.assertEqual(images.svg_size('<svg width="100px" height="50" viewBox="0 0 10 10">'), (100.0, 50.0))
        self.assertEqual(images.svg_size('<svg width="200" viewBox="0 0 40 20">'), (200.0, 100.0))
        self.assertIsNone(images.svg_size('<svg><rect/></svg>'))

    def test_unknown_size_is_an_error(self):
        with self.assertRaisesRegex(ValueError, 'size is unknown'):
            images.svg_to_png('<svg><rect/></svg>')

    def test_missing_chrome_says_how_to_continue(self):
        with patch.object(images, 'find_chrome', return_value=None), \
             self.assertRaisesRegex(RuntimeError, '--no-images'):
            images.svg_to_png(SVG)

    @unittest.skipUnless(images.find_chrome(), 'Chrome is not installed')
    def test_real_chrome_renders_exact_size_at_2x_on_white(self):
        from PIL import Image
        png = images.svg_to_png(SVG)
        shot = Image.open(io.BytesIO(png))
        self.assertEqual(shot.size, (60, 40))
        self.assertEqual(shot.convert('RGB').getpixel((30, 20)), (255, 0, 0))


class NamingAndLoadingTests(unittest.TestCase):
    def test_names_are_stable_unique_and_media_safe(self):
        a = images.named('My Guide - 2.4 fig1', b'one', b'one', 'image/png')
        self.assertEqual(a, images.named('My Guide - 2.4 fig1', b'one', b'one', 'image/png'))
        self.assertRegex(a.name, r'^my-guide-2-4-fig1-[0-9a-f]{8}\.png$')
        self.assertNotEqual(a.name, images.named('My Guide - 2.4 fig1', b'two', b'two', 'image/png').name)
        self.assertTrue(images.named('x', b'1', b'1', 'image/jpeg').name.endswith('.jpg'))

    def test_load_files_and_svg(self):
        with tempfile.TemporaryDirectory() as d, patch.object(images, 'svg_to_png', fake_render):
            (Path(d) / 'a.png').write_bytes(PNG)
            (Path(d) / 'b.svg').write_text(SVG)
            (Path(d) / 'c.txt').write_text('x')
            a, b = images.load(Path(d) / 'a.png'), images.load(Path(d) / 'b.svg')
            self.assertEqual((a.mime, a.data), ('image/png', PNG))
            self.assertEqual((b.mime, b.name[-4:]), ('image/png', '.png'))
            self.assertEqual(b.data, fake_render(SVG))
            with self.assertRaisesRegex(ValueError, 'Unsupported image type'):
                images.load(Path(d) / 'c.txt')
            with self.assertRaisesRegex(ValueError, 'not found'):
                images.load(Path(d) / 'nope.png')
            self.assertEqual(len(images.load_all([Path(d) / 'a.png', Path(d) / 'a.png'])), 1)
            with self.assertRaisesRegex(ValueError, 'At most'):
                images.load_all([Path(d) / 'a.png'] * 11)


class ValidationTests(unittest.TestCase):
    def check(self, action, supplied=(), notes=()):
        return actions.validate({'actions': [action]}, list(notes), 100, supplied)

    def test_supplied_image_is_allowed_on_create(self):
        self.check(with_image(create(), 'fig-1.png'), ['fig-1.png'])

    def test_unsupplied_or_invented_image_is_rejected_with_the_allowed_names(self):
        with self.assertRaisesRegex(ValueError, "'other.png'.*fig-1.png"):
            self.check(with_image(create(), 'other.png'), ['fig-1.png'])
        with self.assertRaisesRegex(ValueError, 'none were supplied'):
            self.check(with_image(create(), 'fig-1.png'))
        bad = create(); bad['fields']['Answer'] += ' <img alt="no src">'
        with self.assertRaisesRegex(ValueError, 'quoted src'):
            self.check(bad, ['fig-1.png'])

    def test_update_keeps_existing_images_but_cannot_add_unsupplied_ones(self):
        old = note(); old['fields']['Answer'] = 'Old fact <img src="own.jpg">'
        keep = update(); keep['fields']['Answer'] = 'Old fact <img src="own.jpg"> and more'
        self.check(keep, notes=[old])
        add = update(); add['fields']['Answer'] = 'Old fact <img src="own.jpg"> <img src="new.png">'
        with self.assertRaisesRegex(ValueError, "'new.png'"):
            self.check(add, notes=[old])
        self.check(add, ['new.png'], notes=[old])

    def test_image_names_lists_used_images_only(self):
        used = with_image(create(), 'fig-2.png')
        self.assertEqual(actions.image_names([used, update()]), {'fig-2.png'})


class JournalMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'run.json'
        self.calls = []

    def api(self, action, **params):
        self.calls.append((action, params))
        if action == 'storeMediaFile':
            return params['filename']
        if action == 'addNote':
            return 124
        if action == 'notesInfo':
            fields = self.fields
            return [dict(noteId=124, modelName='ScientificBasic', tags=['gemini_auto', 'science'],
                         fields={k: {'value': v} for k, v in fields.items()})]
        raise AssertionError(action)

    def test_only_referenced_images_are_uploaded_before_add_note(self):
        used = with_image(create(), 'fig-1.png')
        self.fields = used['fields']
        plan = dict(deck='D', notes=[], actions=[used], status={}, limits={'actions': 100},
                    media={'fig-1.png': images.media_entry(images.Image('fig-1.png', PNG, 'image/png'))})
        journal.save(self.path, plan)
        with patch.object(anki_connect, 'request', side_effect=self.api):
            journal.apply_plan(self.path, {'create'})
        kinds = [c[0] for c in self.calls]
        self.assertEqual(kinds[:2], ['storeMediaFile', 'addNote'])
        self.assertEqual(self.calls[0][1], {'filename': 'fig-1.png',
                                            'data': base64.b64encode(PNG).decode()})
        self.assertEqual(kinds.count('storeMediaFile'), 1)

    def test_upload_failure_leaves_nothing_pending(self):
        used = with_image(create(), 'fig-1.png')
        plan = dict(deck='D', notes=[], actions=[used], status={}, limits={'actions': 100},
                    media={'fig-1.png': images.media_entry(images.Image('fig-1.png', PNG, 'image/png'))})
        journal.save(self.path, plan)
        with patch.object(anki_connect, 'request', side_effect=RuntimeError('storeMediaFile: boom')), \
             self.assertRaisesRegex(RuntimeError, 'boom'):
            journal.apply_plan(self.path, {'create'})
        self.assertEqual(json.loads(self.path.read_text())['status'], {})

    def test_journal_without_media_rejects_image_tags(self):
        plan = dict(deck='D', notes=[], actions=[with_image(create(), 'fig-1.png')], status={},
                    limits={'actions': 100})
        journal.save(self.path, plan)
        with patch.object(anki_connect, 'request') as api, self.assertRaises(ValueError):
            journal.apply_plan(self.path, {'create'})
        api.assert_not_called()

    def test_new_generation_merges_media_into_the_open_journal(self):
        def plan(name):
            return dict(version=2, deck='D', notes=[], actions=[with_image(create(), name)],
                        status={}, limits={'actions': 100}, usage=[], runs=[],
                        media={name: images.media_entry(images.Image(name, PNG, 'image/png'))})
        first = plan('a.png')
        second = plan('b.png'); second['actions'][0]['fields']['Question'] = '<img src="b.png"> Other?'
        with patch.object(journal, 'STATE', Path(self.tmp.name)):
            path, _ = journal.store_generation(first)
            journal.store_generation(second)
        saved = json.loads(path.read_text())
        self.assertEqual(set(saved['media']), {'a.png', 'b.png'})
        self.assertEqual(len(saved['actions']), 2)


class GenerationTests(unittest.TestCase):
    def gemini(self, action):
        g = Mock()
        g.count.return_value = 10
        g.usage = []
        g.generate.return_value = {'actions': [action]}
        g.types.Part.from_bytes.side_effect = lambda data, mime_type: ('part', mime_type, data)
        return g

    def run_generate(self, argv, action, text='Block text', clipboard=None, attachments=()):
        with tempfile.TemporaryDirectory() as d:
            g = self.gemini(action)
            args = cli.parser().parse_args(argv)
            with patch.object(journal, 'STATE', Path(d)), \
                 patch.object(cli, 'retrieve', return_value=([], [], {})):
                cli.generate(g, args, text, clipboard, attachments)
            path = next(Path(d).glob('*.json'))
            return g, json.loads(path.read_text())

    def test_gemini_gets_every_image_and_the_names_and_rules(self):
        a = images.Image('fig-a.png', PNG + b'a', 'image/png')
        b = images.Image('fig-b.jpg', b'jpg', 'image/jpeg')
        g, plan = self.run_generate(['D', '--preview-only'], with_image(create(), 'fig-b.jpg'),
                                    attachments=[a, b])
        contents = g.generate.call_args.args[1]
        self.assertEqual(contents[0], 'Block text')
        self.assertEqual(contents[1], 'Attached images, in order: fig-a.png, fig-b.jpg.')
        self.assertEqual(contents[2:4], [('part', 'image/png', PNG + b'a'), ('part', 'image/jpeg', b'jpg')])
        self.assertIn('<img src="NAME">', g.generate.call_args.args[2])
        # Only the image that an action uses is kept in the journal.
        self.assertEqual(list(plan['media']), ['fig-b.jpg'])
        self.assertEqual(base64.b64decode(plan['media']['fig-b.jpg']['data']), b'jpg')

    def test_a_card_cannot_use_an_image_nobody_supplied(self):
        with self.assertRaisesRegex(ValueError, 'was not supplied'):
            self.run_generate(['D', '--preview-only'], with_image(create(), 'invented.png'),
                              attachments=[images.Image('fig-a.png', PNG, 'image/png')])

    def test_no_images_means_no_image_text_and_no_instruction(self):
        g, plan = self.run_generate(['D', '--preview-only'], create())
        self.assertEqual(g.generate.call_args.args[1][0], 'Block text')
        self.assertFalse(any('Attached images' in str(p) for p in g.generate.call_args.args[1]))
        self.assertNotIn('<img src="NAME">', g.generate.call_args.args[2])
        self.assertEqual(plan['media'], {})

    def test_clipboard_image_can_be_put_on_a_card_unless_no_images(self):
        clip = PNG + b'clipboard'
        name = images.clipboard_image(clip).name
        g, plan = self.run_generate(['D', '--preview-only'], with_image(create(), name),
                                    text='', clipboard=clip)
        self.assertIn(name, g.generate.call_args.args[1][2])
        self.assertEqual(list(plan['media']), [name])
        # With --no-images Gemini still reads the picture, but cannot use it on a card.
        with self.assertRaisesRegex(ValueError, 'was not supplied'):
            self.run_generate(['D', '--preview-only', '--no-images'], with_image(create(), name),
                              text='', clipboard=clip)
        g, _ = self.run_generate(['D', '--preview-only', '--no-images'], create(), clipboard=clip)
        self.assertEqual(g.generate.call_args.args[1][1], ('part', 'image/png', clip))


class CommandLineTests(unittest.TestCase):
    def run_main(self, argv, **patches):
        def api(action, **params):
            return {'deckNames': ['D'], 'modelFieldNames': anki_connect.FIELDS[params['modelName']]
                    if action == 'modelFieldNames' else None}[action]
        with patch.object(cli.sys, 'argv', ['anki_gemini.py'] + argv), \
             patch.object(anki_connect, 'request', side_effect=api), \
             patch.object(cli, 'Gemini', return_value=Mock()), \
             patch.object(cli, 'clipboard', return_value=('text', None)), \
             patch.object(cli, 'generate') as generate:
            cli.main()
        return generate

    def test_image_option_loads_files_and_renders_svg(self):
        with tempfile.TemporaryDirectory() as d, patch.object(images, 'svg_to_png', fake_render):
            (Path(d) / 'a.png').write_bytes(PNG)
            (Path(d) / 'b.svg').write_text(SVG)
            generate = self.run_main(['D', '--preview-only', '--image', str(Path(d) / 'a.png'),
                                      '--image', str(Path(d) / 'b.svg')])
        attachments = generate.call_args.args[4]
        self.assertEqual([a.mime for a in attachments], ['image/png', 'image/png'])
        self.assertEqual(attachments[0].data, PNG)
        self.assertEqual(attachments[1].data, fake_render(SVG))

    def test_missing_image_fails_before_any_gemini_call(self):
        with self.assertRaisesRegex(ValueError, 'not found'):
            self.run_main(['D', '--preview-only', '--image', '/nonexistent/x.png'])

    def test_contradictory_or_misplaced_image_options(self):
        with self.assertRaisesRegex(ValueError, 'contradict'):
            self.run_main(['D', '--image', 'x.png', '--no-images'])
        with self.assertRaisesRegex(ValueError, 'only applies when generating'):
            self.run_main(['--review-latest', '--image', 'x.png'])


if __name__ == '__main__':
    unittest.main()
