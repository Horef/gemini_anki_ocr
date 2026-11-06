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


# A LaTeX command written with one JSON backslash decodes as a control character
# (e.g. "\frac" -> form feed + "rac"). Anki silently drops these characters.
LATEX_ESCAPES = {'\f': '\\f', '\b': '\\b'}
CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def repair_escapes(data):
    """Restore LaTeX backslashes that Gemini's JSON turned into \\f or \\b."""
    for a in data.get('actions', []) if isinstance(data, dict) else []:
        fields = a.get('fields') if isinstance(a, dict) else None
        if isinstance(fields, dict):
            for k, v in fields.items():
                if isinstance(v, str):
                    fields[k] = v.translate(str.maketrans(LATEX_ESCAPES))
    return data


def media(value):
    return re.findall(r'<(?:img|audio|video|source)\b[^>]*>|\[sound:[^\]]+\]', value, re.I)


IMG_TAG = re.compile(r'<img\b[^>]*>', re.I)
IMG_SRC = re.compile(r"""\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)


def image_sources(value):
    """[(tag, src or None)] for every <img> in an HTML field value."""
    out = []
    for tag in IMG_TAG.findall(value):
        m = IMG_SRC.search(tag)
        out.append((tag, (m.group(1) if m.group(1) is not None else m.group(2)) if m else None))
    return out


def image_names(actions):
    """Image file names used by the fields of create/update actions."""
    return {src for a in actions for v in (a.get('fields') or {}).values()
            if isinstance(v, str) for _, src in image_sources(v) if src}


def check_images(index, fields, old_fields, supplied):
    """Reject <img> tags that point at anything but a supplied image (or, for an
    update, an image the note already shows)."""
    for k, v in fields.items():
        for tag, src in image_sources(v):
            if tag in old_fields.get(k, ''):
                continue
            if src is None:
                raise ValueError(f'Action {index}: <img> in {k} needs a quoted src.')
            if src not in supplied:
                names = ', '.join(sorted(supplied)) or 'none were supplied'
                raise ValueError(f'Action {index}: image {src!r} in {k} was not supplied. '
                                 f'Use only the supplied image names exactly as given ({names}).')


def validate(data, notes, limit, images=()):
    """`images`: names of images that may be shown with <img src="NAME">."""
    supplied = set(images)
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
        bad = [k for k, v in fields.items() if CONTROL.search(v)]
        if bad:
            raise ValueError(f'Action {index} has control characters in {", ".join(bad)} '
                             '(likely a broken LaTeX escape); nothing was written to Anki.')
        tags = a.get('tags', [])
        if not isinstance(tags, list) or any(not isinstance(t, str) or not re.fullmatch(r'[^\s<>]+', t) for t in tags):
            raise ValueError('Invalid tags.')
        if model == 'ScientificTwoSided':
            term = re.sub(r'\s*\([^)]*\)', '', plain(fields['Front'])).strip().casefold()
            reverse = plain(fields['Back'] + ' ' + fields['Back question']).casefold()
            if term and re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', reverse):
                raise ValueError(
                    f'Action {index} ({plain(fields["Front"])!r}): Back or Back question '
                    f'contains the Front term {term!r}. Rephrase them without the term, '
                    'its acronym or aliases, or use ScientificBasic.')
        if a['action'] == 'update':
            nid = a.get('note_id')
            if type(nid) is not int or nid not in allowed or nid in updated:
                raise ValueError('Unknown or repeated update ID.')
            old = allowed[nid]
            if model != old['model']:
                raise ValueError('Cannot change note type.')
            if tags:
                raise ValueError('Updates preserve existing tags; omit update tags.')
            check_images(index, fields, old['fields'], supplied)
            for k, v in fields.items():
                if any(v.count(m) < old['fields'][k].count(m) for m in media(old['fields'][k])):
                    raise ValueError('Update would remove embedded media.')
            updated.add(nid)
        else:
            if 'note_id' in a:
                raise ValueError('Create action cannot specify a note ID.')
            check_images(index, fields, {}, supplied)
            title = plain(fields[FIELDS[model][0]]).casefold()
            if (model, title) in titles:
                raise ValueError('Duplicate creation in batch.')
            titles.add((model, title))
    return actions
