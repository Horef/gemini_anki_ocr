# Gemini Anki Cards & LaTeX OCR

Automate Anki flashcard creation and LaTeX OCR on macOS and Telegram on the go using Google's Gemini models.

---

## 🌟 Features

- **Mobile Telegram Bot (`telegram_bot.py`)**:
  - Send text excerpts, textbook notes, or photos directly from your phone on Telegram.
  - Automatically formats structured Anki flashcards (`ScientificTwoSided`, `ScientificBasic`) with tap-to-copy TSV code blocks.
  - Send math images with `/ocr` or caption `ocr` to get formatted LaTeX.
  - Set custom target decks dynamically using `/deck DeckName`.

- **Mac Desktop Anki Automation (`anki_gemini.py`)**:
  - Takes text or screenshots directly from your macOS clipboard.
  - Automatically queries existing Anki cards via **AnkiConnect** to prevent duplicates.
  - Direct integration with local Anki app (`http://localhost:8765`).

- **Instant Mac LaTeX OCR (`ocr_gemini.py`)**:
  - Extracts text and equations from screenshots using `gemini-3.6-flash`.
  - Automatically copies LaTeX output to macOS clipboard (`pbcopy`) with native system notifications.

---

## 🚀 Setup & Installation

### 1. Prerequisites
- **Python 3.8+**
- **Google Gemini API Key**: Obtain from [Google AI Studio](https://aistudio.google.com/).
- **Telegram Bot Token** *(for mobile bot)*: Create a bot via [@BotFather](https://t.me/BotFather) on Telegram.
- **Anki + AnkiConnect** *(for desktop auto-import)*: Installed and running on `http://localhost:8765`.

### 2. Install Dependencies

Install the required Python packages:
```bash
pip install google-generativeai pillow python-telegram-bot
```

### 3. Configuration

Copy `config.example.py` to `config.py`:
```bash
cp config.example.py config.py
```

Open `config.py` and insert your API keys:
```python
ANKI_GEMINI_API_KEY = "your-gemini-api-key"
OCR_GEMINI_API_KEY = "your-gemini-api-key"

# Telegram Bot Token from @BotFather
TELEGRAM_BOT_TOKEN = "your-telegram-bot-token"

# Optional: Limit bot access to your Telegram user ID
ALLOWED_TELEGRAM_USER_IDS = []
```

---

## 🛠️ Usage

### 📱 Telegram Bot (On the go)

Run the Telegram bot server:
```bash
python3 telegram_bot.py
```

- **Generate Flashcards**: Send any text or photo/screenshot in chat.
- **LaTeX OCR**: Send a photo with caption `ocr` or send `/ocr`.
- **Set Target Deck**: Send `/deck Neuroscience`.

### 🖥️ Mac Desktop Scripts

- **Generate Anki Cards**:
  ```bash
  python3 anki_gemini.py "Medical Deck"
  ```
- **LaTeX OCR**:
  ```bash
  python3 ocr_gemini.py
  ```

---

## ⌨️ Integration with Keyboard Maestro

Bind hotkeys in **Keyboard Maestro** for desktop shortcut workflows:
- **LaTeX OCR Hotkey**: `Execute Shell Script` -> `python3 /path/to/ocr_gemini.py`
- **Anki Card Creator Hotkey**: `Execute Shell Script` -> `python3 /path/to/anki_gemini.py "%Variable%AnkiDeckName%"`

---

## 📁 Repository Structure

```
├── telegram_bot.py     # Mobile Telegram Chatbot for Anki cards & LaTeX OCR
├── anki_gemini.py      # Desktop AnkiConnect flashcard generation script
├── ocr_gemini.py       # Desktop LaTeX OCR script copying result to clipboard
├── config.py           # Local configuration file storing API keys (gitignored)
├── config.example.py   # Template configuration file
├── .gitignore          # Excludes secrets, cache, and system files
└── README.md           # Project documentation
```

---

## 📄 License

MIT License
