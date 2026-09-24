"""AnkiConnect client and note helpers."""
import html
import json
import re
import urllib.error
import urllib.request

URL = 'http://localhost:8765'
FIELDS = {"ScientificBasic": ["Question", "Answer"],
          "ScientificTwoSided": ["Front", "Front question", "Back", "Back question"]}
BATCH = 100


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def plain(value):
    return html.unescape(re.sub(r'<[^>]+>', ' ', value)).strip()


def quoted(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def request(action, **params):
    req = urllib.request.Request(URL,
        data=packed(dict(action=action, version=6, params=params)).encode(),
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f'AnkiConnect action {action!r} returned HTTP {exc.code}. '
            f'Confirm that AnkiConnect is listening on {URL}.'
        ) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, 'reason', exc)
        detail = f' ({reason})' if str(reason) else ''
        raise RuntimeError(
            f'Cannot reach AnkiConnect for action {action!r} at '
            f'{URL}{detail}. Open Anki and confirm that the '
            'AnkiConnect add-on is installed and enabled.'
        ) from exc
    except TimeoutError as exc:
        raise RuntimeError(
            f'AnkiConnect timed out during action {action!r}. Confirm that Anki '
            'is responsive, then try again.'
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f'AnkiConnect returned invalid JSON during action {action!r}. '
            'Confirm that port 8765 is handled by the AnkiConnect add-on.'
        ) from exc
    if not isinstance(data, dict) or 'error' not in data or 'result' not in data:
        raise RuntimeError(f'{action}: malformed AnkiConnect response')
    if data['error'] is not None:
        raise RuntimeError(f'{action}: {data["error"]}')
    return data['result']


def snapshot(note):
    return dict(note_id=note['noteId'], model=note['modelName'],
                fields={k: v['value'] for k, v in note['fields'].items()},
                tags=sorted(note.get('tags', [])))


def supported_query(deck=None):
    models = ' OR '.join(f'note:{quoted(m)}' for m in FIELDS)
    return (f'deck:{quoted(deck)} ' if deck else '') + f'({models})'


def fetch_notes(ids):
    """Return snapshots in the order of ids; fail if any note disappeared."""
    notes = []
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        raw = request('notesInfo', notes=chunk)
        if len(raw) != len(chunk) or any(not n for n in raw):
            raise RuntimeError('Some candidate notes disappeared; rerun retrieval.')
        notes.extend(snapshot(n) for n in raw)
    return notes


def mod_times(ids):
    """Return {note_id: mod}, or None when this AnkiConnect lacks notesModTime."""
    try:
        items = request('notesModTime', notes=ids)
    except RuntimeError as exc:
        if 'unsupported action' in str(exc).lower():
            return None
        raise
    return {item['noteId']: item['mod'] for item in items}
