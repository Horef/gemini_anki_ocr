"""Structured-output schema and strict validation of proposed note actions."""
import re

from .anki_connect import FIELDS, plain

KINDS = ('create', 'update', 'skip', 'review')


def action_schema():
    def field_shape(names):
        return {'type': 'object',
                'properties': {name: {'type': 'string'} for name in names},
                'required': names, 'additionalProperties': False}

    properties = {
        'action': {'type': 'string', 'enum': list(KINDS)},
        'note_id': {'type': 'integer'}, 'model': {'type': 'string'},
        # Requiring one complete note-model shape prevents structured output from
        # returning only Question, Front, or another incomplete subset. The local
        # validator still ensures that this shape matches the declared model.
        'fields': {'anyOf': [field_shape(names) for names in FIELDS.values()]},
        'tags': {'type': 'array', 'items': {'type': 'string'}},
        'reason': {'type': 'string'}, 'evidence': {'type': 'string'}}
    # Do not send maxItems here. Gemini's structured-output schema compiler rejects
    # this nested action schema at ordinary limits (for example, 20 or 100), even
    # though the same schema without maxItems is accepted. validate() enforces the
    # requested action limit before any proposal can be saved or applied.
    return {'type': 'object', 'properties': {'actions': {'type': 'array',
        'items': {'type': 'object', 'properties': properties,
        'required': ['action', 'reason', 'evidence']}}}, 'required': ['actions']}


def media(value):
    return re.findall(r'<(?:img|audio|video|source)\b[^>]*>|\[sound:[^\]]+\]', value, re.I)


def validate(data, notes, limit):
    if not isinstance(data, dict) or set(data) != {'actions'}:
        raise ValueError('Expected an actions object.')
    actions = data['actions']
    if not isinstance(actions, list) or len(actions) > limit:
        raise ValueError('Invalid or excessive action count.')
    allowed = {n['note_id']: n for n in notes}
    updated, titles = set(), set()
    for index, a in enumerate(actions):
        if not isinstance(a, dict) or set(a) - {'action', 'note_id', 'model', 'fields', 'tags', 'reason', 'evidence'}:
            raise ValueError('Invalid action properties.')
        if a.get('action') not in KINDS:
            raise ValueError('Unknown action.')
        if any(not isinstance(a.get(k), str) or not a[k].strip() for k in ('reason', 'evidence')):
            raise ValueError('Every action requires a reason and source evidence.')
        if a['action'] in {'skip', 'review'}:
            if a.get('fields') or a.get('tags'):
                raise ValueError('Review/skip actions cannot modify fields or tags.')
            continue
        model, fields = a.get('model'), a.get('fields')
        if model not in FIELDS or not isinstance(fields, dict):
            raise ValueError('Unsupported note type or fields.')
        expected, actual = set(FIELDS[model]), set(fields)
        if actual != expected:
            missing, extra = sorted(expected - actual), sorted(actual - expected)
            details = []
            if missing:
                details.append('missing ' + ', '.join(missing))
            if extra:
                details.append('unexpected ' + ', '.join(extra))
            raise ValueError(
                f'Action {index} ({model}) must return all required fields: '
                + '; '.join(details) + '.')
        if any(not isinstance(v, str) or not plain(v) for v in fields.values()):
            raise ValueError('Empty required field.')
        tags = a.get('tags', [])
        if not isinstance(tags, list) or any(not isinstance(t, str) or not re.fullmatch(r'[^\s<>]+', t) for t in tags):
            raise ValueError('Invalid tags.')
        if model == 'ScientificTwoSided':
            term = re.sub(r'\s*\([^)]*\)', '', plain(fields['Front'])).strip().casefold()
            reverse = plain(fields['Back'] + ' ' + fields['Back question']).casefold()
            if term and re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', reverse):
                raise ValueError('Reverse side reveals the Front term.')
        if a['action'] == 'update':
            nid = a.get('note_id')
            if type(nid) is not int or nid not in allowed or nid in updated:
                raise ValueError('Unknown or repeated update ID.')
            old = allowed[nid]
            if model != old['model']:
                raise ValueError('Cannot change note type.')
            if tags:
                raise ValueError('Updates preserve existing tags; omit update tags.')
            for k, v in fields.items():
                if any(v.count(m) < old['fields'][k].count(m) for m in media(old['fields'][k])):
                    raise ValueError('Update would remove embedded media.')
            updated.add(nid)
        else:
            if 'note_id' in a:
                raise ValueError('Create action cannot specify a note ID.')
            title = plain(fields[FIELDS[model][0]]).casefold()
            if (model, title) in titles:
                raise ValueError('Duplicate creation in batch.')
            titles.add((model, title))
    return actions
