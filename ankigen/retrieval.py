"""Hybrid note retrieval: local BM25 plus Gemini embeddings, fused by reciprocal rank."""
from collections import Counter, defaultdict
import math
import re
import sys

import numpy as np

from . import anki_connect, journal
from .anki_connect import FIELDS, plain, supported_query
from .index import Index, digest, unit
from .settings import setting

DIMENSION = 768
RRF_K = 60
EMBED_BATCH = 100
CHUNK_CHARS = 500
MAX_DOCUMENT_CHARS = 8000
# Fields that carry meaning; two-sided prompt fields are generic question templates.
DOCUMENT_FIELDS = {'ScientificBasic': ('Question', 'Answer'),
                   'ScientificTwoSided': ('Front', 'Back')}
STOPWORDS = frozenset(
    'a an and are as at be been by can do does for from has have how in into is it its '
    'of on or that the their there these this those to was were what when where which '
    'who why will with'.split())


def embedding_model():
    return setting('ANKI_GEMINI_EMBEDDING_MODEL', 'gemini-embedding-2')


def tokens(text):
    return [w for w in re.findall(r'\w+', text.casefold()) if len(w) > 1 and w not in STOPWORDS]


def title_and_body(model, fields):
    return tuple(plain(fields.get(name, '')) for name in DOCUMENT_FIELDS[model])


def has_text(model, fields):
    """Media-only notes embed identically and would match every query."""
    return any(title_and_body(model, fields))


def document(model, fields):
    title, body = title_and_body(model, fields)
    return f'title: {title or "none"} | text: {body}'[:MAX_DOCUMENT_CHARS]


def query_text(chunk):
    return f'task: search result | query: {chunk}'


def chunks(text, limit):
    """Split source text into at most limit sentence-aligned chunks."""
    pieces = []
    for sentence in re.split(r'(?<=[.!?])\s+|\n+', text or ''):
        sentence = ' '.join(sentence.split())
        pieces.extend(sentence[i:i + MAX_DOCUMENT_CHARS]
                      for i in range(0, len(sentence), MAX_DOCUMENT_CHARS))
    if not pieces:
        return []
    target = max(CHUNK_CHARS, math.ceil(sum(map(len, pieces)) / limit))
    while True:
        out, current = [], ''
        for piece in pieces:
            if current and len(current) + len(piece) + 1 > target:
                out.append(current)
                current = piece
            else:
                current = f'{current} {piece}'.strip()
        out.append(current)
        if len(out) <= limit:
            return out
        target = math.ceil(target * 1.5)


class BM25:
    def __init__(self, docs, k1=1.2, b=0.75):
        self.postings = defaultdict(list)
        lengths = {}
        for key, words in docs.items():
            lengths[key] = len(words)
            for word, freq in Counter(words).items():
                self.postings[word].append((key, freq))
        n = len(docs)
        average = (sum(lengths.values()) / n) if n else 1.0
        self.norm = {key: k1 * (1 - b + b * length / (average or 1.0))
                     for key, length in lengths.items()}
        self.idf = {w: math.log(1 + (n - len(p) + 0.5) / (len(p) + 0.5))
                    for w, p in self.postings.items()}
        self.k1 = k1

    def rank(self, query, top):
        scores = defaultdict(float)
        for word in set(tokens(query)):
            for key, freq in self.postings.get(word, ()):
                scores[key] += self.idf[word] * freq * (self.k1 + 1) / (freq + self.norm[key])
        return [key for key, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:top]]


def fuse(rankings, k=RRF_K):
    """Reciprocal rank fusion; ties break on the smaller key for determinism."""
    scores = defaultdict(float)
    for ranking in rankings:
        for position, key in enumerate(ranking, 1):
            scores[key] += 1 / (k + position)
    return [key for key, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))]


def refresh(index):
    """Mirror all supported Anki notes into the index; only changed notes are fetched."""
    ids = anki_connect.request('findNotes', query=supported_query())
    mods = anki_connect.mod_times(ids) if ids else {}
    known = index.mods()
    stale = ids if mods is None else [i for i in ids if i not in known or known[i] != mods.get(i)]
    rows = []
    for note in anki_connect.fetch_notes(stale):
        model = note['model']
        if model not in FIELDS:
            continue
        fields = {name: note['fields'].get(name, '') for name in FIELDS[model]}
        rows.append((note['note_id'], None if mods is None else mods.get(note['note_id']),
                     model, fields, digest(document(model, fields))))
    index.upsert(rows)
    index.remove_missing(ids)
    return len(rows)


def embed_documents(gemini, index, pending, model, limit):
    """Embed and store (key, text) pairs; return how many were stored."""
    stored, batch, position = 0, EMBED_BATCH, 0
    pending = pending[:limit]
    while position < len(pending):
        chunk = pending[position:position + batch]
        try:
            vectors = gemini.embed([text for _, text in chunk], model, DIMENSION)
        except RuntimeError:
            if batch == 1:
                raise
            # Some gateways accept only one input per request; probe once, then continue singly.
            batch, chunk = 1, chunk[:1]
            vectors = gemini.embed([chunk[0][1]], model, DIMENSION)
        index.store_vectors((key, vector) for (key, _), vector in zip(chunk, vectors))
        stored += len(chunk)
        position += len(chunk)
    return stored


def build_index(gemini, args, index=None):
    """Synchronise and embed every supported note; used by --sync-index."""
    index = index or Index()
    try:
        changed = refresh(index)
        model = embedding_model()
        notes = {nid: n for nid, n in index.notes().items() if has_text(n['model'], n['fields'])}
        keys = {nid: digest(model, DIMENSION, n['digest']) for nid, n in notes.items()}
        have = index.vectors(keys.values())
        pending = [(keys[nid], document(n['model'], n['fields']))
                   for nid, n in sorted(notes.items()) if keys[nid] not in have]
        stored = embed_documents(gemini, index, pending, model, args.embed_notes)
        index.prune_vectors(set(keys.values()))
        print(f'Index: {len(notes)} notes, {changed} refreshed, {stored} embedded, '
              f'{len(pending) - stored} still waiting.')
    finally:
        index.close()


def retrieve(deck, text, image, gemini, args, index=None):
    """Return (existing note snapshots, queued create proposals) most relevant to the source."""
    index = index or Index()
    try:
        return _retrieve(index, deck, text, image, gemini, args)
    finally:
        index.close()


def _retrieve(index, deck, text, image, gemini, args):
    refresh(index)
    deck_ids = set(anki_connect.request('findNotes', query=supported_query(deck)))
    docs = {nid: (n['model'], n['fields'], n['digest'])
            for nid, n in index.notes(deck_ids).items() if has_text(n['model'], n['fields'])}
    # Queued proposals get negative keys so they never collide with Anki note IDs.
    queued = journal.queued_creates(deck)
    for i, action in enumerate(queued):
        fields = action['fields']
        docs[-1 - i] = (action['model'], fields, digest(document(action['model'], fields)))

    queries = chunks(text, args.chunks)
    per_query = args.matches_per_query
    lexical = BM25({key: tokens(' '.join(title_and_body(m, f))) for key, (m, f, _) in docs.items()})
    rankings = [lexical.rank(q, per_query) for q in queries]
    dense = _dense_rankings(gemini, index, docs, queries, image, per_query, args)
    if dense is None and not queries:
        raise RuntimeError('Semantic search is unavailable and the clipboard has no text, so '
                           'existing notes cannot be compared. Nothing was generated.')
    rankings.extend(dense or [])

    ranked = fuse(rankings)
    note_ids = [key for key in ranked if key >= 0][:args.context_notes]
    similar_queued = [{'model': queued[-1 - key]['model'], 'fields': queued[-1 - key]['fields']}
                      for key in ranked if key < 0][:args.context_notes]
    return anki_connect.fetch_notes(note_ids), similar_queued


def _dense_rankings(gemini, index, docs, queries, image, per_query, args):
    """Embedding rankings per query, or None when semantic search is unavailable."""
    model = embedding_model()
    keys = {key: digest(model, DIMENSION, d) for key, (_, _, d) in docs.items()}
    vectors = index.vectors(keys.values())
    pending = sorted({keys[k]: document(m, f) for k, (m, f, _) in docs.items()
                      if keys[k] not in vectors}.items())
    if pending:
        try:
            embed_documents(gemini, index, pending, model, args.embed_notes)
        except RuntimeError as exc:
            print(f'Indexing paused; notes indexed so far remain usable. {exc}', file=sys.stderr)
        vectors = index.vectors(keys.values())
    items = [query_text(q) for q in queries]
    if image:
        items.append(gemini.types.Part.from_bytes(data=image, mime_type='image/png'))
    try:
        query_vectors = [unit(v) for v in gemini.embed(items, model, DIMENSION)] if items else []
    except (RuntimeError, ValueError) as exc:
        print(f'Semantic search unavailable; using keyword ranking only. {exc}', file=sys.stderr)
        return None
    missing = sum(keys[k] not in vectors for k in docs)
    if missing:
        print(f'Semantic index covers {len(docs) - missing}/{len(docs)} notes; the rest are '
              'ranked by keywords only. Run again or use --sync-index to finish indexing.',
              file=sys.stderr)
    indexed = sorted(k for k in docs if keys[k] in vectors)
    if not indexed or not query_vectors:
        return []
    matrix = np.stack([vectors[keys[k]] for k in indexed])
    similarity = matrix @ np.stack(query_vectors).T
    rankings = []
    for column in similarity.T:
        top = np.lexsort((np.array(indexed), -column))[:per_query]
        rankings.append([indexed[i] for i in top if column[i] >= args.min_similarity])
    return rankings
