# Example configuration. Copy to `config.py` (gitignored) and fill in your values.
# Every setting can instead be supplied as an environment variable of the same name;
# environment variables take priority over this file.

ANKI_GEMINI_API_KEY = "YOUR_ANKI_GEMINI_API_KEY_HERE"
OCR_GEMINI_API_KEY = "YOUR_OCR_GEMINI_API_KEY_HERE"

# Gemini transport: "direct" (Google Gemini API, uses the keys above) or
# "apigee" (AI gateway, uses the two APIGEE_* settings for every Gemini call).
GEMINI_TRANSPORT = "direct"
APIGEE_API_KEY = ""
APIGEE_BASE_URL = ""  # e.g. "https://<host>/ai_gateway/v1/<team>"

# Embedding model for the local retrieval index. Changing it re-embeds all notes.
ANKI_GEMINI_EMBEDDING_MODEL = "gemini-embedding-2"

# Telegram Bot Settings
TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN_HERE"
# Optional: restrict bot access to specific Telegram user ID(s), e.g. [123456789]
ALLOWED_TELEGRAM_USER_IDS = []
