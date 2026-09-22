import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

import ocr_gemini


class ExtractResponseTextTests(unittest.TestCase):
    def test_strips_nonempty_text(self):
        response = SimpleNamespace(text="  x^2  ", candidates=[])

        self.assertEqual(ocr_gemini.extract_response_text(response), "x^2")

    def test_empty_response_reports_finish_metadata(self):
        candidate = SimpleNamespace(
            finish_reason="MAX_TOKENS", finish_message="output limit reached"
        )
        response = SimpleNamespace(
            text=None,
            candidates=[candidate],
            prompt_feedback="not blocked",
        )

        with self.assertRaisesRegex(RuntimeError, "finish_reason=MAX_TOKENS"):
            ocr_gemini.extract_response_text(response)

    def test_response_without_candidates_is_explained(self):
        response = SimpleNamespace(
            text=None, candidates=[], prompt_feedback="blocked"
        )

        with self.assertRaisesRegex(RuntimeError, "prompt_feedback=blocked"):
            ocr_gemini.extract_response_text(response)

    def test_recitation_uses_specific_error(self):
        finish_reason = SimpleNamespace(name="RECITATION")
        candidate = SimpleNamespace(finish_reason=finish_reason, finish_message=None)
        response = SimpleNamespace(text=None, candidates=[candidate], prompt_feedback=None)

        with self.assertRaises(ocr_gemini.RecitationBlockedError):
            ocr_gemini.extract_response_text(response)


class LocalOcrTests(unittest.TestCase):
    @patch.object(ocr_gemini.subprocess, "run")
    def test_returns_tesseract_output(self, run):
        run.return_value = SimpleNamespace(stdout=b"NMDA receptor\n", stderr=b"")

        text = ocr_gemini.local_ocr(Image.new("RGB", (10, 10), "white"))

        self.assertEqual(text, "NMDA receptor")
        self.assertEqual(run.call_args.args[0], ["tesseract", "stdin", "stdout", "-l", "eng"])


if __name__ == "__main__":
    unittest.main()
