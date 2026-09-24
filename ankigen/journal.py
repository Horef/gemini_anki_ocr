"""Proposal journals: locking, review queue, verified apply, and archiving."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import uuid

from . import anki_connect
from .actions import validate
from .anki_connect import plain, quoted, snapshot

STATE = Path(__file__).resolve().parents[1] / '.anki_gemini_runs'
CLOSED = {'done', 'dismissed'}


def save(path, data):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w', encoding='utf-8') as f:
        os.chmod(temp, 0o600)
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush(); os.fsync(f.fileno())
    temp.replace(path)


def save_preview(path, plan):
    report = Path(path).with_suffix('.txt')
    temp = report.with_suffix(report.suffix + '.tmp')
    temp.write_text(preview(plan), encoding='utf-8')
    os.chmod(temp, 0o600)
    temp.replace(report)


def preview(plan):
    old = {n['note_id']: n for n in plan['notes']}
    out = [f"Deck: {plan['deck']}\n"]
    for i, a in enumerate(plan['actions']):
        out.append(f"\n[{i}] {a['action'].upper()} — {a['reason']}\nEvidence: {a['evidence']}")
        if a.get('model'):
            out.append(f"Model: {a['model']} | Note ID: {a.get('note_id', 'new note')}")
        for k, v in a.get('fields', {}).items():
            before = old[a['note_id']]['fields'][k] if a['action'] == 'update' else None
            if before != v:
                if before is not None:
                    out.append(f'{k} BEFORE: {before}')
                out.append(f'{k} AFTER: {v}')
        if a['action'] == 'create':
            out.append('Tags: ' + ', '.join(sorted(set(a.get('tags', []) + ['gemini_auto']))))
    return '\n'.join(out) + '\n'


def action_identifier(action, limit=90):
    fields = action.get('fields') or {}
    value = fields.get('Front') or fields.get('Question') or action.get('reason', '')
    value = re.sub(r'\s+', ' ', plain(value))
    return value if len(value) <= limit else value[:limit - 1].rstrip() + '…'


def waiting(plan):
    return [(i, a) for i, a in enumerate(plan['actions'])
            if a['action'] != 'skip' and plan['status'].get(str(i)) not in CLOSED]


def print_review_summary(plan, limit=5):
    items = waiting(plan)
    if not items:
        return
    print('Proposals waiting for review:')
    for _, action in items[:limit]:
        print(f'• {action["action"]}: {action_identifier(action)}')
    if len(items) > limit:
        print(f'• …and {len(items) - limit} more')


@contextmanager
def plan_lock(path, conflict=(
        'This batch is already open or being applied. Close its review window first.')):
    lock = Path(path).with_suffix('.lock')
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise FileExistsError(conflict)
    os.close(fd)
    try:
        yield
    finally:
        lock.unlink()


def apply_plan(path, kinds, only=None):
    path = Path(path)
    with plan_lock(path):
        _apply_locked(path, kinds, only)


def _apply_locked(path, kinds, only=None):
    plan = json.loads(path.read_text())
    validate({'actions': plan['actions']}, plan['notes'], plan['limits']['actions'])
    if only is not None and any(i < 0 or i >= len(plan['actions']) for i in only):
        raise ValueError('Selected action index is outside the proposal.')
    old = {n['note_id']: n for n in plan['notes']}
    for i, a in enumerate(plan['actions']):
        key = str(i)
        if (a['action'] not in kinds or plan['status'].get(key) in CLOSED
                or (only is not None and i not in only)):
            continue
        if key in plan['status']:
            raise RuntimeError(f'Action {i} has an uncertain prior result. Inspect Anki and the journal before retrying.')
        if a['action'] == 'update':
            current = snapshot(anki_connect.request('notesInfo', notes=[a['note_id']])[0])
            if current != old[a['note_id']]:
                raise RuntimeError(f'Note {a["note_id"]} changed after preview; regenerate proposal.')
            if a['note_id'] not in anki_connect.request(
                    'findNotes', query=f'deck:{quoted(plan["deck"])} nid:{a["note_id"]}'):
                raise RuntimeError('Note moved outside the target deck.')
        plan['status'][key] = 'pending'
        save(path, plan)  # Original fields are already in plan, before every write.
        if a['action'] == 'create':
            nid = anki_connect.request('addNote', note=dict(
                deckName=plan['deck'], modelName=a['model'], fields=a['fields'],
                options={'allowDuplicate': False},
                tags=sorted(set(a.get('tags', []) + ['gemini_auto']))))
            if type(nid) is not int:
                raise RuntimeError('addNote did not return a note ID.')
            plan.setdefault('created_ids', {})[key] = nid
            save(path, plan)
        else:
            nid = a['note_id']
            changes = {k: v for k, v in a['fields'].items() if old[nid]['fields'][k] != v}
            if changes:
                anki_connect.request('updateNoteFields', note={'id': nid, 'fields': changes})
        actual = snapshot(anki_connect.request('notesInfo', notes=[nid])[0])
        if any(actual['fields'].get(k) != v for k, v in a['fields'].items()):
            raise RuntimeError(f'Verification failed for note {nid}; inspect journal.')
        if a['action'] == 'update':
            expected = {**old[nid]['fields'], **a['fields']}
            if actual['fields'] != expected or actual['tags'] != old[nid]['tags']:
                raise RuntimeError('Unrelated fields or tags changed; inspect journal.')
        plan['status'][key] = 'done'; save(path, plan)
        title = plain(a['fields'].get('Front') or a['fields'].get('Question', 'Note'))
        verb = 'Added' if a['action'] == 'create' else 'Updated'
        print(f'✅ {verb}: {title[:80]}')


def needs_review(plan):
    return not plan.get('closed_at') and bool(waiting(plan))


def queued_plans(deck=None):
    results = []
    for path in STATE.glob('*.json'):
        try:
            plan = json.loads(path.read_text())
            if (deck is None or plan['deck'] == deck) and needs_review(plan):
                results.append((path, plan))
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f'Cannot read queued batch {path.name}: {exc}') from exc
    # Generated filenames include UTC creation time; mtime changes during apply.
    return sorted(results, key=lambda item: item[0].name, reverse=True)


def queued_creates(deck):
    """Unapplied create proposals for deck; they are not in Anki yet."""
    return [a for _, plan in queued_plans(deck) for _, a in waiting(plan)
            if a['action'] == 'create']


def store_generation(plan):
    """Append a generation to this deck's open journal, or start a new one."""
    STATE.mkdir(mode=0o700, exist_ok=True)
    # Serialize discovery and creation so concurrent generators cannot both decide
    # that the deck has no open journal. The per-plan lock also excludes review.
    with plan_lock(STATE / 'generation-queue',
                   'Another generation is being saved; wait for it to finish and try again.'):
        # Version-1 journals predate aggregation. Leave them immutable so their
        # recovery record keeps its original meaning; the first new run starts v2.
        open_plans = [(path, saved) for path, saved in queued_plans(plan['deck'])
                      if saved.get('version') == 2]
        if len(open_plans) > 1:
            raise RuntimeError(
                f'Multiple open journals exist for {plan["deck"]}; review them before generating more cards.')
        if not open_plans:
            path = STATE / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
                            + '-' + uuid.uuid4().hex[:8] + '.json')
            with plan_lock(path):
                save(path, plan)
                save_preview(path, plan)
            return path, list(range(len(plan['actions'])))

        path, _ = open_plans[0]
        with plan_lock(path):
            saved = json.loads(path.read_text())
            if not needs_review(saved) or saved.get('version') != 2:
                raise RuntimeError('The open journal changed while this generation was being saved; try again.')
            if any(value == 'pending' for value in saved['status'].values()):
                raise RuntimeError(
                    f'The open journal {path.name} has an uncertain write; resolve it before adding actions.')

            old_notes = {note['note_id']: note for note in saved['notes']}
            referenced = {action['note_id'] for action in plan['actions']
                          if action['action'] == 'update'}
            for note in plan['notes']:
                note_id = note['note_id']
                if note_id in referenced and note_id in old_notes and old_notes[note_id] != note:
                    raise RuntimeError(
                        f'Note {note_id} changed since the open journal was created; review it before generating another update.')
                if note_id not in old_notes:
                    saved['notes'].append(note)
                    old_notes[note_id] = note

            first = len(saved['actions'])
            combined = saved['actions'] + plan['actions']
            combined_limit = saved['limits']['actions'] + plan['limits']['actions']
            validate({'actions': combined}, saved['notes'], combined_limit)
            saved['actions'] = combined
            saved['limits']['actions'] = combined_limit
            saved.setdefault('usage', []).extend(plan.get('usage', []))
            saved.setdefault('runs', []).extend(plan.get('runs', []))
            saved['updated_at'] = datetime.now(timezone.utc).isoformat()
            save(path, saved)
            save_preview(path, saved)
            return path, list(range(first, len(combined)))


def archive_locked(path, plan, resolution):
    if any(v == 'pending' for v in plan['status'].values()):
        raise RuntimeError('An uncertain write must be resolved before this batch can be archived.')
    plan['closed_at'] = datetime.now(timezone.utc).isoformat()
    plan['resolution'] = resolution
    save(path, plan)
    archive = path.parent / 'archive'
    archive.mkdir(mode=0o700, exist_ok=True)
    if (archive / path.name).exists():
        raise FileExistsError('Archive already contains this batch; original files retained.')
    path.replace(archive / path.name)
    report = path.with_suffix('.txt')
    if report.exists():
        report.replace(archive / report.name)


def archive_if_finished(path):
    with plan_lock(path):
        plan = json.loads(path.read_text())
        if not needs_review(plan):
            archive_locked(path, plan, 'completed')
            return True
    return False


def review_latest(deck=None):
    batches = queued_plans(deck)
    if not batches:
        print('Nothing waiting for review' + (f' in {deck}.' if deck else '.'))
        return
    path, _ = batches[0]
    with plan_lock(path):
        plan = json.loads(path.read_text())
        validate({'actions': plan['actions']}, plan['notes'], plan['limits']['actions'])
        if any(v == 'pending' for v in plan['status'].values()):
            raise RuntimeError(f'This batch has an uncertain write. Inspect Anki and {path}; it will not be retried or discarded automatically.')
        from . import review_window
        choice, selected = review_window.review_dialog(plan, path.stem, len(batches))
        if choice == 'later':
            print('Saved for later. No changes made.')
            return
        if choice not in {'apply', 'discard'}:
            raise ValueError('Unknown review decision.')
        if choice == 'apply':
            _apply_locked(path, {'create', 'update'}, selected)
        plan = json.loads(path.read_text())
        for i, a in enumerate(plan['actions']):
            if a['action'] != 'skip' and plan['status'].get(str(i)) != 'done':
                plan['status'][str(i)] = 'dismissed'
        archive_locked(path, plan, choice)
        print('Review finished. Batch cleared; recovery records archived.')
        remaining = len(queued_plans(deck))
        if remaining:
            print(f'{remaining} older batch(es) waiting. Run --review-latest again when ready.')
