import os
import sys
import subprocess
from google import genai
from PIL import ImageGrab, Image

# --- CONFIGURATION ---
from config import OCR_GEMINI_API_KEY as API_KEY

client = genai.Client(api_key=API_KEY)

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
        response = client.models.generate_content(
            model='gemini-3.6-flash',
            contents=[prompt, img]
        )
        latex_text = response.text.strip()
        
        # Fail-safe: Strip markdown code blocks if the model includes them anyway
        if latex_text.startswith("```"):
            lines = latex_text.split('\n')
            if len(lines) >= 2:
                latex_text = '\n'.join(lines[1:-1])
        
        # 3. Copy the result to the macOS clipboard via pbcopy
        process = subprocess.Popen('pbcopy', env={'LANG': 'en_US.UTF-8'}, stdin=subprocess.PIPE)
        process.communicate(latex_text.encode('utf-8'))
        
        notify("LaTeX OCR Success", "LaTeX copied to clipboard!")
        
    except Exception as e:
        notify("LaTeX OCR Error", str(e))

if __name__ == "__main__":
    main()