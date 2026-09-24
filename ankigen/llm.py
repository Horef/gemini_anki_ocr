"""Gemini client construction (direct or Apigee) and bounded API calls."""
from concurrent.futures import ThreadPoolExecutor
import json
import sys

from .anki_connect import packed
from .settings import setting

TIMEOUT_MS = 180000
TRANSPORTS = ('direct', 'apigee')
EMBED_WORKERS = 8


def _configured(name):
    value = setting(name)
    return None if not value or str(value).startswith('YOUR_') else str(value)


def build_client(key_name='ANKI_GEMINI_API_KEY', transport=None):
    """Return a genai.Client for GEMINI_TRANSPORT; validate before any network call."""
    from google import genai
    from google.genai import types

    transport = str(transport or setting('GEMINI_TRANSPORT', 'direct')).strip().lower()
    retry = types.HttpRetryOptions(attempts=1)
    if transport == 'direct':
        key = _configured(key_name)
        if not key:
            raise ValueError(f'{key_name} is not set in the environment or config.py.')
        return genai.Client(api_key=key, http_options=types.HttpOptions(
            timeout=TIMEOUT_MS, retry_options=retry))
    if transport == 'apigee':
        key, base = _configured('APIGEE_API_KEY'), _configured('APIGEE_BASE_URL')
        missing = [name for name, value in (('APIGEE_API_KEY', key),
                                            ('APIGEE_BASE_URL', base)) if not value]
        if missing:
            raise ValueError('Apigee transport needs ' + ' and '.join(missing)
                             + ' in the environment or config.py.')
        if not base.startswith('https://'):
            raise ValueError('APIGEE_BASE_URL must start with https://.')
        # The gateway authenticates with x-apikey; the SDK still requires some api_key.
        return genai.Client(api_key='apigee-placeholder', vertexai=True, project='', location='',
                            http_options=types.HttpOptions(
                                api_version='v1', base_url=base.rstrip('/'),
                                headers={'x-apikey': key}, timeout=TIMEOUT_MS,
                                retry_options=retry))
    raise ValueError(f'Unsupported GEMINI_TRANSPORT {transport!r}; use one of {TRANSPORTS}.')


class Gemini:
    max_generations = 1

    def __init__(self, args):
        from google.genai import types
        self.types, self.args = types, args
        self.client = build_client()
        self.calls = 0
        self.usage = []

    def count(self, model, parts, stage='token counting'):
        try:
            response = self.client.models.count_tokens(model=model, contents=parts)
        except Exception as exc:
            raise RuntimeError(f'Gemini {stage} failed for model {model}: {exc}') from exc
        n = response.total_tokens
        if not isinstance(n, int) or n < 0:
            raise RuntimeError('Token counting failed; no generation attempted.')
        return n

    def generate(self, model, parts, instruction, schema, output):
        if self.calls >= self.max_generations:
            raise RuntimeError('Generation-call limit reached.')
        # Count instructions and schema as text too; reserve space for framing overhead.
        tokens = self.count(model, parts + [instruction, packed(schema)],
                            stage='card comparison request token counting')
        if tokens + 1024 > self.args.request_tokens:
            raise ValueError(f'Request needs {tokens} tokens; exceeds request guardrail.')
        self.calls += 1
        try:
            r = self.client.models.generate_content(model=model, contents=parts,
                config=self.types.GenerateContentConfig(system_instruction=instruction,
                    response_mime_type='application/json', response_json_schema=schema,
                    temperature=0.2, max_output_tokens=output,
                    thinking_config=self.types.ThinkingConfig(thinking_level='low'),
                    automatic_function_calling=self.types.AutomaticFunctionCallingConfig(
                        disable=True)))
        except Exception as exc:
            raise RuntimeError(
                f'Gemini card comparison generation failed for model {model}: {exc}'
            ) from exc
        usage = r.usage_metadata.model_dump(mode='json') if r.usage_metadata else {}
        self.record(dict(model=model, counted_input=tokens, usage=usage))
        candidates = r.candidates or []
        reason = str(getattr(candidates[0], 'finish_reason', '')) if candidates else ''
        if reason.split('.')[-1] != 'STOP' or not r.text:
            raise RuntimeError(f'Incomplete Gemini response ({reason}); nothing imported.')
        return json.loads(r.text)

    def embed(self, items, model, dimension):
        """Embed each item (text or Part) separately; return one vector per item."""
        types = self.types
        contents = [types.Content(parts=[types.Part.from_text(text=item)
                                         if isinstance(item, str) else item])
                    for item in items]
        config = types.EmbedContentConfig(output_dimensionality=dimension)

        def call(batch):
            r = self.client.models.embed_content(model=model, contents=batch, config=config)
            return [e.values for e in (r.embeddings or [])]

        try:
            # Vertex-mode (Apigee) embedContent accepts one content per request.
            if getattr(self.client, 'vertexai', False) and model != 'gemini-embedding-001':
                with ThreadPoolExecutor(max_workers=EMBED_WORKERS) as pool:
                    vectors = [v for batch in pool.map(call, [[c] for c in contents])
                               for v in batch]
            else:
                vectors = call(contents)
        except Exception as exc:
            raise RuntimeError(f'Gemini embedding failed for model {model}: {exc}') from exc
        if len(vectors) != len(items) or any(not v or len(v) != dimension for v in vectors):
            raise RuntimeError(f'Gemini embedding returned unexpected vectors for model {model}.')
        self.record(dict(model=model, embedded_items=len(items)))
        return vectors

    def record(self, entry):
        self.usage.append(entry)
        if not getattr(self.args, 'quiet', False):
            print('Gemini usage: ' + packed(entry), file=sys.stderr)
