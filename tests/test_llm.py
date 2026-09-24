import unittest
from unittest.mock import patch

from ankigen import llm


def configured(values):
    return lambda name, default=None: values.get(name, default)


class TransportTests(unittest.TestCase):
    def build(self, values, key_name='ANKI_GEMINI_API_KEY'):
        with patch.object(llm, 'setting', side_effect=configured(values)), \
             patch('google.genai.Client') as client:
            llm.build_client(key_name)
        return client

    def test_direct_is_default_with_timeout_and_single_attempt(self):
        client = self.build({'ANKI_GEMINI_API_KEY': 'k'})
        kwargs = client.call_args.kwargs
        self.assertEqual(kwargs['api_key'], 'k')
        self.assertNotIn('vertexai', kwargs)
        self.assertEqual(kwargs['http_options'].timeout, llm.TIMEOUT_MS)
        self.assertEqual(kwargs['http_options'].retry_options.attempts, 1)

    def test_direct_uses_requested_key(self):
        client = self.build({'OCR_GEMINI_API_KEY': 'ocr'}, 'OCR_GEMINI_API_KEY')
        self.assertEqual(client.call_args.kwargs['api_key'], 'ocr')

    def test_apigee_uses_gateway_header_and_base_url(self):
        client = self.build({'GEMINI_TRANSPORT': 'Apigee', 'APIGEE_API_KEY': 'g',
                             'APIGEE_BASE_URL': 'https://gw.example/ai_gateway/v1/x/'})
        kwargs = client.call_args.kwargs
        self.assertTrue(kwargs['vertexai'])
        options = kwargs['http_options']
        self.assertEqual(options.base_url, 'https://gw.example/ai_gateway/v1/x')
        self.assertEqual(options.headers, {'x-apikey': 'g'})
        self.assertEqual(options.api_version, 'v1')
        self.assertEqual(options.retry_options.attempts, 1)

    def test_invalid_configuration_fails_before_client_creation(self):
        cases = [
            ({}, 'ANKI_GEMINI_API_KEY is not set'),
            ({'ANKI_GEMINI_API_KEY': 'YOUR_ANKI_GEMINI_API_KEY_HERE'}, 'is not set'),
            ({'GEMINI_TRANSPORT': 'apigee', 'APIGEE_API_KEY': 'g'}, 'APIGEE_BASE_URL'),
            ({'GEMINI_TRANSPORT': 'apigee', 'APIGEE_BASE_URL': 'https://x'}, 'APIGEE_API_KEY'),
            ({'GEMINI_TRANSPORT': 'apigee', 'APIGEE_API_KEY': 'g',
              'APIGEE_BASE_URL': 'http://x'}, 'https://'),
            ({'GEMINI_TRANSPORT': 'vertex'}, "Unsupported GEMINI_TRANSPORT 'vertex'"),
        ]
        for values, message in cases:
            with self.subTest(values=values), \
                 patch.object(llm, 'setting', side_effect=configured(values)), \
                 patch('google.genai.Client') as client, \
                 self.assertRaisesRegex(ValueError, message):
                llm.build_client()
            client.assert_not_called()


if __name__ == '__main__':
    unittest.main()
