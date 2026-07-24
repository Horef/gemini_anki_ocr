import os

# Example Configuration File
# Copy this file to `config.py` and replace with your actual API keys.

ANKI_GEMINI_API_KEY = os.getenv("ANKI_GEMINI_API_KEY", "YOUR_ANKI_GEMINI_API_KEY_HERE")
OCR_GEMINI_API_KEY = os.getenv("OCR_GEMINI_API_KEY", "YOUR_OCR_GEMINI_API_KEY_HERE")

# Telegram Bot Settings
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
# Optional: restrict bot access to specific Telegram user ID(s), e.g. [123456789]
ALLOWED_TELEGRAM_USER_IDS = []
