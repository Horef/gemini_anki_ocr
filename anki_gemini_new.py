import json
import urllib.request
import subprocess
import sys
import os
import re
import base64
import binascii

# ==========================================
# CONFIGURATION
# ==========================================
from config import ANKI_GEMINI_API_KEY as API_KEY

# We now grab the deck name dynamically from Keyboard Maestro!
# If for some reason it's not provided, it falls back to "General"
DECK_NAME = sys.argv[1] if len(sys.argv) > 1 else "General"
# ==========================================

def extract_keywords_with_llm(text):
    """Uses a fast, lightweight model to intelligently extract core concepts for Anki searching."""
    # Using 2.5-flash for maximum speed and lowest cost for this simple extraction task
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={API_KEY}"
    
    system_prompt = "Extract the core scientific terms, anatomical structures, or specific concepts from the text. Output ONLY a comma-separated list of these terms. No bullet points, no quotes, no conversational filler."
    
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {"temperature": 0.1}
    }
    
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            raw_keywords = res_data['candidates'][0]['content']['parts'][0]['text'].strip()
            # Split by comma and clean up whitespace
            return [k.strip() for k in raw_keywords.split(',') if k.strip()]
    except Exception:
        # If it fails, return an empty list so the main script can gracefully continue
        return []

def get_relevant_anki_concepts(text, limit=30):
    """Fetches the Front/Question of relevant notes by keyword searching Anki."""
    
    is_keyword_search = False
    query = f'deck:"{DECK_NAME}"'
    
    # If we have text, try to extract keywords and build a specific search query
    if text:
        keywords = extract_keywords_with_llm(text)
        # print(f"🔍 Extracted keywords for Anki search: {keywords}")
        if keywords:
            # Escape any accidental quotes from the LLM and build the OR clause
            safe_keywords = [k.replace('"', '\\"') for k in keywords]
            or_clause = " OR ".join([f'"{k}"' for k in safe_keywords])
            query = f'deck:"{DECK_NAME}" ({or_clause})'
            is_keyword_search = True
            
    payload = {
        "action": "findNotes",
        "version": 6,
        "params": {"query": query}
    }
    
    try:
        req = urllib.request.Request('http://localhost:8765', data=json.dumps(payload).encode('utf-8'))
        with urllib.request.urlopen(req) as res:
            response = json.loads(res.read().decode('utf-8'))
            note_ids = response.get('result', [])
            
            # Fallback: If keyword search found nothing, grab the 10 most recent cards instead
            if not note_ids and is_keyword_search:
                fallback_payload = {
                    "action": "findNotes",
                    "version": 6,
                    "params": {"query": f'deck:"{DECK_NAME}"'}
                }
                req_fb = urllib.request.Request('http://localhost:8765', data=json.dumps(fallback_payload).encode('utf-8'))
                with urllib.request.urlopen(req_fb) as res_fb:
                    response_fb = json.loads(res_fb.read().decode('utf-8'))
                    note_ids = response_fb.get('result', [])
                is_keyword_search = False
                limit = 10  # Reduce limit to 10 for the fallback to minimize noise
            
            if not note_ids:
                return []
            
            # If we searched by keywords, take the first N relevant hits.
            # If we didn't (image fallback), take the last N added.
            if is_keyword_search:
                target_ids = note_ids[:limit]
            else:
                target_ids = note_ids[-limit:]
            
            info_payload = {
                "action": "notesInfo",
                "version": 6,
                "params": {"notes": target_ids}
            }
            info_req = urllib.request.Request('http://localhost:8765', data=json.dumps(info_payload).encode('utf-8'))
            with urllib.request.urlopen(info_req) as info_res:
                info_response = json.loads(info_res.read().decode('utf-8'))
                notes_info = info_response.get('result', [])
                
                concepts = []
                for note in notes_info:
                    fields = note.get('fields', {})
                    raw_val = ""
                    if 'Front' in fields:
                        raw_val = fields['Front']['value']
                    elif 'Question' in fields:
                        raw_val = fields['Question']['value']
                        
                    if raw_val:
                        # Strip HTML tags from the Anki field to keep the context clean
                        clean_val = re.sub(r'<[^>]+>', '', raw_val).strip()
                        concepts.append(clean_val)
                return concepts
    except Exception as e:
        # If AnkiConnect fails here, we return an empty list and proceed without context
        return []

def get_fields(note_type, cols):
    """Maps the TSV columns from Gemini to your specific Anki fields."""
    
    # Safety check: if Gemini forgets trailing tabs, this prevents IndexErrors
    def safe_get(idx):
        return cols[idx].strip() if len(cols) > idx else ""

    if note_type == "ScientificTwoSided":
        return {
            "Front": safe_get(1),
            "Front question": safe_get(2),
            "Back": safe_get(3),
            "Back question": safe_get(4)
        }
    elif note_type == "ScientificBasic":
        return {
            "Question": safe_get(1),
            "Answer": safe_get(2)
        }
    return None

def get_clipboard_text():
    """Gets plain text from the macOS clipboard."""
    p = subprocess.Popen(['pbpaste'], stdout=subprocess.PIPE)
    p.wait()
    return p.stdout.read().decode('utf-8')

def get_clipboard_image_base64():
    """Extracts an image from the macOS clipboard and converts it to a Base64 string natively."""
    try:
        # Ask macOS for the clipboard contents formatted as PNG hex data
        p = subprocess.Popen(['osascript', '-e', 'get the clipboard as «class PNGf»'], 
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = p.communicate()
        
        if p.returncode == 0:
            hex_str = stdout.decode('utf-8').strip()
            # The output looks like: «data PNGf89504E47...»
            if hex_str.startswith('«data PNGf') and hex_str.endswith('»'):
                # Extract just the hex characters
                hex_data = hex_str[10:-1].replace('\n', '').replace(' ', '')
                # Convert hex to binary bytes, then encode to base64
                img_bytes = binascii.unhexlify(hex_data)
                return base64.b64encode(img_bytes).decode('utf-8')
    except Exception as e:
        pass
    return None

def call_gemini(text, image_b64, existing_concepts):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent?key={API_KEY}"
    
    system_prompt = r"""
    Role: You are an expert Anki flashcard creator specializing in science subjects (Biology, Neuroscience, Chemistry, etc.). Your goal is to take textbook excerpts, image descriptions, tables, diagrams, or user notes and convert them into highly structured, tab-separated values (TSV) ready for Anki import.
    
    Output Format:
    You must output ONLY raw, tab-separated text. Do not use Markdown tables. Do not include conversational filler before or after the flashcards.
    The columns must strictly follow this order (with more or less columns depending on the Note Type):
    NoteType [TAB] Field 2 [TAB] Field 3 [TAB] Field 4 [TAB] Field 5 [TAB] Tags (Optional)

    Note Types & Logic:
    You must choose between two Note Types based on these strict guidelines:
        1. ScientificTwoSided (ONLY for terms, vocabulary, or concepts and their COMPLETE/CORE definitions)
            * CRITICAL RULE: Use this note type ONLY when the "Term" (Column 2) is a specific name, vocabulary word, or concept, and the "Answer" (Column 4) provides its COMPLETE, all-encompassing definition or identity.
            * DO NOT use this note type for specific roles, partial features, context-dependent details, or "why/how" questions about the term, as this will lead to confusing flashcards. If the card represents a specific detail or partial description rather than a complete definition, use ScientificBasic instead.
            * Columns:
                Column 1: ScientificTwoSided
                Column 2 (The Term): The exact core concept or term (e.g., Action Potential, Ribosome).
                Column 3 (Forward Prompt): A short question (e.g., What is it?, Define this term.).
                Column 4 (The Answer): The complete definition/identity. You MUST bold the most critical keywords. Use <div> and <br> for spacing if there are multiple points.
                Column 5 (Reverse Prompt): A short reverse question prompting for the term name (e.g., What is this concept called?, How is this organelle called?).
        2. ScientificBasic (For specific questions, mechanisms, roles, functions, "Why/How" questions, or lists)
            * Use this for any flashcard that is NOT a complete definition of a term. This includes:
                - "Why" or "How" questions (causal links, mechanisms, explanations, rationales).
                - Specific functions, roles, or behaviors in a certain context (e.g., "What is the role of X in context Y?").
                - Lists of structures, steps in a process, or comparisons.
            * Columns:
                Column 1: ScientificBasic
                Column 2 (The Question): The full, self-contained question.
                Column 3 (The Answer): The detailed answer. You MUST bold critical keywords. Use <div> and <br> for structural spacing.
                
    Targeting Deeper Insights & Concepts:
        * Do not just extract superficial facts, definitions, or numbers. You must identify and capture the deeper "why" and "how" questions, functional reasons, causal links, and biological/scientific rationales.
        * Look for phrases indicating necessity or purpose (e.g., "is needed to...", "in order to...", "because..."), and create cards specifically targeting these insights.
        * For example, if a text explains that a structure has a specific feature to solve a particular problem, create a ScientificBasic card asking WHY that feature is necessary or HOW it solves the problem.

    Atomicity (Splitting Rules - VERY IMPORTANT): 
        If the input text or image describes multiple distinct concepts, anatomical structures, or processes, you MUST split them into separate flashcards. 
        Generate one tab-separated row per flashcard. 
        For example, if the text describes both "cerebral cortex" and "cerebellar cortex", output TWO separate 'ScientificTwoSided' rows. Do not combine them into one massive card.

    Formatting Rules (CRITICAL):
        Math/Chemistry: You must use \( and \) for any inline formulas, ions, or math (e.g., \(C_6H_{12}O_6\), \(Na^+\)).
        HTML: Use <b> tags for bolding. Wrap distinct paragraphs or list items in <div> tags or separate them with <br>. In general, use HTML for all formatting instead of Markdown.
        Tables from Images: If the user uploads an image of a table, break the rows down into logical Q&A format flashcards. Do not attempt to recreate the raw HTML table unless absolutely necessary.

    Uniqueness: While filling the columns, if you feel that name of the term or concept has multiple meanings, you can add a clarifying parenthetical in the Term column (e.g., "Sodium (Na+)", "Action Potential (Neuroscience)").

    Conciseness: Keep the answers comprehensive but punchy. Emulate the style of a rigorous university student.

    Groundness: Do not invent information (except for the sake of formatting). If the input text has not enough information to create a flashcard, you can skip it. Do not create vague or low-quality cards. Quality over quantity is key.

    Self-Sustainability: Each flashcard should be understandable on its own without needing to refer back to the original text. Avoid pronouns or vague references that would make the card confusing when reviewed in Anki.

    Examples:
    
    Input: "The nucleus is an organelle in eukaryotic cells in which most of the DNA is located, and which is bounded by a double membrane."
    Output:
    ScientificTwoSided	Nucleus (Biology)	What is it?	An organelle in eukaryotic cells in which <b>most of the DNA is located</b>, bounded by a <b>double membrane</b>.	How is this organelle called?
    
    Input: "The brain comprises six major structures: the medulla oblongata, pons, cerebellum, midbrain, diencephalon, and cerebrum."
    Output:
    ScientificBasic	What are the six major structures that comprise the brain?	The <b>medulla oblongata</b>, <b>pons</b>, <b>cerebellum</b>, <b>midbrain</b>, <b>diencephalon</b>, and <b>cerebrum</b>.
    
    Input: "Cerebellar granule cells provide an extreme example of divergent feedforward connectivity, with the information carried by approximately 200 million input fibers (called mossy fibers) mixed and expanded onto the 50 billion granule cells. Such a large representation is needed to handle the many different ways that multiple channels of information can be combined. Because the large number of possible combinations would be difficult to specify genetically, it is generally thought that the assignment of mossy fibers to their granule cell targets is largely random."
    Output:
    ScientificBasic	How do cerebellar granule cells implement divergent feedforward connectivity?	By mixing and expanding information from <b>~200 million input fibers (mossy fibers)</b> onto <b>50 billion granule cells</b>.
    ScientificBasic	Why is such a large representation of cerebellar granule cells (50 billion) needed to handle inputs from 200 million mossy fibers?	To handle the <b>many different ways</b> that <b>multiple channels of information</b> can be combined.
    ScientificBasic	Why is the assignment of mossy fibers to cerebellar granule cells thought to be largely random?	Because the large number of possible combinations would be <b>difficult to specify genetically</b>.
    """

    # Adding the name of the deck to the system prompt for better context
    system_prompt += f"\n\nThe target Anki deck is called '{DECK_NAME}'. "

    # Dynamically inject the existing context if we found any cards!
    if existing_concepts:
        bullet_points = "\n    - ".join(existing_concepts)
        context_section = f"""
    
    Context - Previously Created Cards:
    To prevent duplicates and save time, do NOT generate flashcards for the following concepts because they have recently been added to the deck. Skip them entirely if they appear in the input text or image:
    - {bullet_points}
        """
        system_prompt += context_section
    
    # Construct the multimodal payload
    parts = []
    if text:
        parts.append({"text": text})
    elif image_b64:
        # If there's an image but no text, give a default prompt
        parts.append({"text": "Please extract the key concepts, tables, or biological/scientific diagrams from this image and convert them into flashcards."})

    if image_b64:
        parts.append({
            "inlineData": {
                "mimeType": "image/png",
                "data": image_b64
            }
        })

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0.2}
    }
    
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            
            # Robust extraction to handle the empty 'parts' array bug
            candidates = res_data.get('candidates', [])
            if not candidates:
                print("❌ Gemini API Error: No candidates returned (Possible block/filter).")
                sys.exit(0)
                
            candidate = candidates[0]
            content = candidate.get('content', {})
            response_parts = content.get('parts', [])
            
            if not response_parts:
                finish_reason = candidate.get('finishReason', 'UNKNOWN')
                print(f"❌ Gemini API Error: Model returned an empty response (FinishReason: {finish_reason}). This is usually caused by safety filters or an API glitch.")
                sys.exit(0)
                
            return response_parts[0]['text'].strip()
    except Exception as e:
        print(f"❌ Gemini API Error: {e}")
        sys.exit(1)

def send_to_anki(note_type, fields):
    payload = {
        "action": "addNote",
        "version": 6,
        "params": {
            "note": {
                "deckName": DECK_NAME,
                "modelName": note_type,
                "fields": fields,
                "options": {"allowDuplicate": False},
                "tags": ["gemini_auto"]
            }
        }
    }
    req = urllib.request.Request('http://localhost:8765', data=json.dumps(payload).encode('utf-8'))
    try:
        with urllib.request.urlopen(req) as res:
            response = json.loads(res.read().decode('utf-8'))
            if response.get("error"):
                # Returning the error message and the name of the card that failed for better debugging
                return False, response['error'] + f" (Card: {fields.get('Front') or fields.get('Question')})"
            return True, "Success"
    except Exception as e:
        return False, f"AnkiConnect Error: {e}"

if __name__ == "__main__":
    # Get clipboard contents
    clipboard_text = get_clipboard_text().strip()
    image_b64 = get_clipboard_image_base64()
    
    if not clipboard_text and not image_b64:
        print("❌ Clipboard is empty. Highlight text or take a screenshot to the clipboard and try again.")
        sys.exit(0)
        
    # Fetch existing concepts relevant to the current text (or fallback to recent for images)
    existing_concepts = get_relevant_anki_concepts(clipboard_text, limit=30)
    
    # Pass them as context to Gemini (Now handles both text and images!)
    gemini_tsv = call_gemini(clipboard_text, image_b64, existing_concepts)
    
    # Clean up markdown code blocks if the AI accidentally adds them
    backticks = "`" * 3 
    if gemini_tsv.startswith(f"{backticks}text\n"): 
        gemini_tsv = gemini_tsv[8:]
    elif gemini_tsv.startswith(f"{backticks}\n"): 
        gemini_tsv = gemini_tsv[4:]
    if gemini_tsv.endswith(f"\n{backticks}"): 
        gemini_tsv = gemini_tsv[:-4]
    elif gemini_tsv.endswith(backticks):
        gemini_tsv = gemini_tsv[:-3]
    
    lines = gemini_tsv.strip().split('\n')
    results_messages = []
    
    for line in lines:
        cols = line.split('\t')
        if len(cols) >= 2:
            note_type = cols[0].strip()
            fields = get_fields(note_type, cols)
            
            if fields:
                success, error_msg = send_to_anki(note_type, fields)
                
                # Grab a snippet of the front of the card for the notification
                card_title = fields.get("Front") or fields.get("Question") or "New Card"
                if len(card_title) > 40: 
                    card_title = card_title[:37] + "..."
                
                if success:
                    results_messages.append(f"✅ {card_title}")
                else:
                    results_messages.append(f"❌ Failed: {error_msg}")
                    
    if not results_messages:
        print("💡 No new flashcards were created (Concepts may have already existed in Anki).")
    else:
        # Print all messages so Keyboard Maestro can capture them into ScriptResult!
        print("\n".join(results_messages))