import sys
import subprocess
from io import BytesIO
from google.genai import types
from PIL import ImageGrab, Image

from ankigen.llm import build_client


class RecitationBlockedError(RuntimeError):
    """Gemini declined to reproduce text that resembles a source."""


def extract_response_text(response):
    """Return Gemini's text, or raise an error that preserves response metadata."""
    text = response.text
    if isinstance(text, str) and text.strip():
        return text.strip()

    candidates = response.candidates or []
    candidate = candidates[0] if candidates else None
    finish_reason = getattr(candidate, "finish_reason", None)
    finish_message = getattr(candidate, "finish_message", None)
    prompt_feedback = getattr(response, "prompt_feedback", None)
    error_type = (
        RecitationBlockedError
        if getattr(finish_reason, "name", None) == "RECITATION"
        or str(finish_reason).endswith(".RECITATION")
        else RuntimeError
    )
    raise error_type(
        "Gemini returned no OCR text "
        f"(finish_reason={finish_reason!s}, finish_message={finish_message!r}, "
        f"prompt_feedback={prompt_feedback!s})."
    )


def local_ocr(img):
    """Use local Tesseract when Gemini's recitation filter blocks transcription."""
    png = BytesIO()
    img.save(png, format="PNG")
    try:
        result = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", "eng"],
            input=png.getvalue(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Gemini blocked verbatim transcription as RECITATION and the local "
            "Tesseract fallback is not installed. Install it with `brew install tesseract`."
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Local Tesseract OCR failed: {detail}") from exc

    text = result.stdout.decode("utf-8", errors="replace").strip()
    if not text:
        raise RuntimeError("Local Tesseract OCR returned no text.")
    return text

def notify(title, message):
    """Sends a native macOS notification."""
    escaped_message = message.replace('"', '\\"')
    subprocess.run(["osascript", "-e", f'display notification "{escaped_message}" with title "{title}"'])

def main():
    # 1. Grab the image from the clipboard
    img = ImageGrab.grabclipboard()
    
    if img is None:
        notify("LaTeX OCR Failed", "No image found in clipboard.")
        sys.exit(1)
        
    # Handle if the clipboard contains a copied file path instead of raw image data
    if isinstance(img, list):
        try:
            img = Image.open(img[0])
        except Exception:
            notify("LaTeX OCR Failed", "Clipboard contains an invalid file.")
            sys.exit(1)

    # 2. Initialize Gemini Flash model call
    prompt = (
        "You are an expert at LaTeX OCR. Please read the provided image and extract all text and math. "
        "Return the math formatted in LaTeX. "
        "CRITICAL: Output ONLY the raw extracted text and LaTeX. Do not include introductory text, "
        "explanations, or enclosing markdown blocks like ```latex. "
    )
    
    try:
        notify("LaTeX OCR", "Processing image...")
        client = build_client('OCR_GEMINI_API_KEY')
        response = client.models.generate_content(
            model='gemini-3.1-flash-lite',
            contents=[prompt, img],
            config=types.GenerateContentConfig(
                # OCR does not need tool calls or hidden reasoning. Disabling both
                # also prevents a response containing only non-text/thought parts.
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                max_output_tokens=8192,
            ),
        )
        try:
            latex_text = extract_response_text(response)
        except RecitationBlockedError:
            notify("LaTeX OCR", "Gemini blocked recitation; using local OCR...")
            latex_text = local_ocr(img)
        
        # Fail-safe: Strip markdown code blocks if the model includes them anyway
        if latex_text.startswith("```"):
            lines = latex_text.split('\n')
            if len(lines) >= 2:
                latex_text = '\n'.join(lines[1:-1])
        
        # 3. Copy the result to the macOS clipboard via pbcopy
        process = subprocess.Popen('pbcopy', env={'LANG': 'en_US.UTF-8'}, stdin=subprocess.PIPE)
        process.communicate(latex_text.encode('utf-8'))
        
        notify("LaTeX OCR Success", "LaTeX copied to clipboard!")
        print(latex_text.encode('utf-8'))
        
    except Exception as e:
        notify("LaTeX OCR Error", str(e))
        print(f"LaTeX OCR Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
