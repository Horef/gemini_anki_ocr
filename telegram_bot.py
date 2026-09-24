import io
import logging
import sys
import asyncio
from PIL import Image

from google.genai import types
from telegram import Update
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ankigen.llm import build_client
from ankigen.settings import setting

TELEGRAM_BOT_TOKEN = setting("TELEGRAM_BOT_TOKEN")
ALLOWED_TELEGRAM_USER_IDS = setting("ALLOWED_TELEGRAM_USER_IDS", [])
if isinstance(ALLOWED_TELEGRAM_USER_IDS, str):
    ALLOWED_TELEGRAM_USER_IDS = [int(v) for v in ALLOWED_TELEGRAM_USER_IDS.split(",") if v.strip()]

# Setup logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Built in main() so importing this module needs no credentials.
anki_client = None
ocr_client = None

# Per-user deck settings
user_decks = {}

ANKI_SYSTEM_PROMPT = r"""
Role: You are an expert Anki flashcard creator specializing in science subjects (Biology, Neuroscience, Chemistry, etc.). Your goal is to take textbook excerpts, image descriptions, tables, diagrams, or user notes and convert them into highly structured, tab-separated values (TSV) ready for Anki import.

Output Format:
You must output ONLY raw, tab-separated text. Do not use Markdown tables. Do not include conversational filler before or after the flashcards.
The columns must strictly follow this order (with more or less columns depending on the Note Type):
NoteType [TAB] Field 2 [TAB] Field 3 [TAB] Field 4 [TAB] Field 5 [TAB] Tags (Optional)

Note Types & Logic:
You must choose between two Note Types based on these guidelines:
    1. ScientificTwoSided (ONLY for terms, vocabulary, or concepts and their COMPLETE/CORE definitions or setups)
        * CRITICAL RULE: Use this note type when the "Term" (Column 2) is a specific name, vocabulary word, or concept, and the "Answer" (Column 4) provides its COMPLETE, all-encompassing definition, identity, and key features/setup.
        * Columns:
            Column 1: ScientificTwoSided
            Column 2 (The Term): The exact core concept or term (e.g., Action Potential, Ribosome).
            Column 3 (Forward Prompt): A structured question prompting for definition, setup, or measurement (e.g., What is it, how is it set up, and what does it measure? or What is it and define its key components.).
            Column 4 (The Answer): The complete definition/identity, using HTML tags like <div> or <br> to group related aspects under clear sub-headers (e.g., <div><b>Setup:</b> ...</div>). You MUST bold the most critical keywords.
            Column 5 (Reverse Prompt): A short reverse question prompting for the term name (e.g., What is this concept called?, How is this organelle called?).
    2. ScientificBasic (For specific questions, mechanisms, roles, functions, "Why/How" questions, or lists)
        * Use this for any flashcard that is NOT a complete definition of a term. This includes:
            - "Why" or "How" questions (causal links, mechanisms, explanations, rationales).
            - Specific functions, roles, or behaviors in a certain context (e.g., "What is the role of X in context Y?").
            - Lists of structures, steps in a process, or comparisons.
        * Columns:
            Column 1: ScientificBasic
            Column 2 (The Question): The full, self-contained question.
            Column 3 (The Answer): The detailed, structured answer. You MUST bold critical keywords. Use <div> and <br> for structural spacing.

Formatting Rules (CRITICAL):
    Math/Chemistry: You must use \( and \) for any inline formulas, ions, or math (e.g., \(C_6H_{12}O_6\), \(Na^+\)).
    HTML: Use <b> tags for bolding. Wrap distinct paragraphs or list items in <div> tags or separate them with <br>. In general, use HTML for all formatting instead of Markdown.
    Tables from Images: If the user uploads an image of a table, break the rows down into logical Q&A format flashcards. Do not attempt to recreate the raw HTML table unless absolutely necessary.
"""

OCR_PROMPT = (
    "You are an expert at LaTeX OCR. Please read the provided image and extract all text and math. "
    "Return the math formatted in LaTeX. "
    "CRITICAL: Output ONLY the raw extracted text and LaTeX. Do not include introductory text, "
    "explanations, or enclosing markdown blocks like ```latex. "
)


def is_user_allowed(user_id: int) -> bool:
    """Checks if the Telegram user ID is authorized."""
    if not ALLOWED_TELEGRAM_USER_IDS:
        return True
    return user_id in ALLOWED_TELEGRAM_USER_IDS


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends start/help message."""
    user = update.effective_user
    if not is_user_allowed(user.id):
        await update.message.reply_text("❌ Sorry, you are not authorized to use this bot.")
        return

    help_text = (
        f"👋 *Welcome, {user.first_name}!*\n\n"
        "This bot helps you generate **Anki Flashcards** and perform **LaTeX OCR** on the go.\n\n"
        "📌 *How to use:*\n"
        "• *Generate Anki Cards:* Send any text message or photo/screenshot.\n"
        "• *LaTeX OCR:* Send a photo with caption `ocr` (or `/ocr`), or send math images.\n"
        "• *Set Target Deck:* Type `/deck DeckName` to specify target deck context.\n\n"
        "💡 *Tip:* Results are returned as formatted cards and raw TSV code blocks so you can tap to copy on your phone!"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)


async def deck_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sets target deck name for the current user session."""
    user = update.effective_user
    if not is_user_allowed(user.id):
        return

    if not context.args:
        current_deck = user_decks.get(user.id, "General")
        await update.message.reply_text(f"🎴 Current target deck: *{current_deck}*\nTo change: `/deck <DeckName>`", parse_mode=ParseMode.MARKDOWN)
        return

    new_deck = " ".join(context.args).strip()
    user_decks[user.id] = new_deck
    await update.message.reply_text(f"✅ Target deck updated to: *{new_deck}*", parse_mode=ParseMode.MARKDOWN)


async def generate_anki_cards_gemini(text: str = "", img: Image.Image = None, deck_name: str = "General") -> str:
    """Calls Gemini Flash using google.genai and ANKI_GEMINI_API_KEY to generate Anki TSV cards."""
    prompt = ANKI_SYSTEM_PROMPT + f"\n\nThe target Anki deck is called '{deck_name}'."
    
    contents = []
    if text:
        contents.append(text)
    elif img:
        contents.append("Please extract key concepts, tables, or diagrams from this image and convert them into Anki flashcards.")

    if img:
        contents.append(img)

    config = types.GenerateContentConfig(
        system_instruction=prompt,
        temperature=0.2
    )

    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None,
        lambda: anki_client.models.generate_content(
            model="gemini-3.6-flash",
            contents=contents,
            config=config,
        )
    )
    return response.text.strip()


async def generate_latex_ocr_gemini(img: Image.Image) -> str:
    """Calls Gemini Flash using google.genai and OCR_GEMINI_API_KEY for LaTeX OCR."""
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None,
        lambda: ocr_client.models.generate_content(
            model="gemini-3.6-flash",
            contents=[OCR_PROMPT, img],
        )
    )
    return response.text.strip()


def parse_and_format_tsv(tsv_text: str) -> tuple[str, str]:
    """Cleans TSV and formats human-readable summary + raw TSV block."""
    backticks = "`" * 3
    if tsv_text.startswith(f"{backticks}text\n"):
        tsv_text = tsv_text[8:]
    elif tsv_text.startswith(f"{backticks}tsv\n"):
        tsv_text = tsv_text[7:]
    elif tsv_text.startswith(f"{backticks}\n"):
        tsv_text = tsv_text[4:]
    if tsv_text.endswith(f"\n{backticks}"):
        tsv_text = tsv_text[:-4]
    elif tsv_text.endswith(backticks):
        tsv_text = tsv_text[:-3]

    lines = [line.strip() for line in tsv_text.strip().split("\n") if line.strip()]
    
    formatted_cards = []
    for idx, line in enumerate(lines, 1):
        cols = line.split("\t")
        if len(cols) >= 2:
            note_type = cols[0].strip()
            if note_type == "ScientificTwoSided" and len(cols) >= 4:
                term = cols[1].strip()
                fwd = cols[2].strip()
                ans = cols[3].strip()
                formatted_cards.append(f"🎴 *Card {idx} ({note_type})*\n• *Term:* {term}\n• *Prompt:* {fwd}\n• *Answer:* {ans}")
            elif note_type == "ScientificBasic" and len(cols) >= 3:
                q = cols[1].strip()
                a = cols[2].strip()
                formatted_cards.append(f"🎴 *Card {idx} ({note_type})*\n• *Question:* {q}\n• *Answer:* {a}")
            else:
                formatted_cards.append(f"🎴 *Card {idx} ({note_type})*\n" + "\n".join([f"• Col {i+1}: {col}" for i, col in enumerate(cols[1:])]))

    summary = "\n\n".join(formatted_cards) if formatted_cards else "No valid cards parsed."
    raw_tsv_block = f"```tsv\n{tsv_text.strip()}\n```"
    return summary, raw_tsv_block


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes incoming text messages to create Anki cards."""
    user = update.effective_user
    if not is_user_allowed(user.id):
        return

    text = update.message.text.strip()
    if not text:
        return

    deck_name = user_decks.get(user.id, "General")
    await update.message.reply_chat_action(action=ChatAction.TYPING)

    try:
        tsv_output = await generate_anki_cards_gemini(text=text, deck_name=deck_name)
        summary, raw_block = parse_and_format_tsv(tsv_output)

        reply_msg = (
            f"✨ *Generated Flashcards (Deck: {deck_name})*\n\n"
            f"{summary}\n\n"
            "📋 *Raw TSV (Tap to copy for Anki import):*\n"
            f"{raw_block}"
        )
        await update.message.reply_text(reply_msg, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Error in handle_text: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error generating cards: `{e}`", parse_mode=ParseMode.MARKDOWN)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes photos for either LaTeX OCR or Anki Flashcards."""
    user = update.effective_user
    if not is_user_allowed(user.id):
        return

    caption = (update.message.caption or "").strip().lower()
    photo_file = await update.message.photo[-1].get_file()
    
    photo_bytes = await photo_file.download_as_bytearray()
    img = Image.open(io.BytesIO(photo_bytes))

    await update.message.reply_chat_action(action=ChatAction.TYPING)

    is_ocr_request = "ocr" in caption or caption == "/ocr"

    try:
        if is_ocr_request:
            ocr_text = await generate_latex_ocr_gemini(img)
            reply_msg = (
                "📐 *LaTeX OCR Result*\n\n"
                f"```latex\n{ocr_text}\n```"
            )
            await update.message.reply_text(reply_msg, parse_mode=ParseMode.MARKDOWN)
        else:
            deck_name = user_decks.get(user.id, "General")
            tsv_output = await generate_anki_cards_gemini(img=img, deck_name=deck_name)
            summary, raw_block = parse_and_format_tsv(tsv_output)

            reply_msg = (
                f"✨ *Generated Flashcards from Image (Deck: {deck_name})*\n\n"
                f"{summary}\n\n"
                "📋 *Raw TSV (Tap to copy for Anki import):*\n"
                f"{raw_block}"
            )
            await update.message.reply_text(reply_msg, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Error in handle_photo: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error processing image: `{e}`", parse_mode=ParseMode.MARKDOWN)


def main():
    """Starts the Telegram bot long-polling loop."""
    global anki_client, ocr_client
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("❌ Error: TELEGRAM_BOT_TOKEN is not set in config.py or environment variables.")
        print("Please set your bot token obtained from @BotFather in config.py and try again.")
        sys.exit(1)

    anki_client = build_client("ANKI_GEMINI_API_KEY")
    ocr_client = build_client("OCR_GEMINI_API_KEY")
    print("🚀 Starting Gemini Anki & OCR Telegram Bot...")
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler(["start", "help"], start_command))
    app.add_handler(CommandHandler("deck", deck_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("✅ Bot is running! Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
