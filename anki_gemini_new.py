import json
import urllib.request
import subprocess
import sys
import os
import re
import base64
import binascii
from google import genai
from google.genai import types

# ==========================================
# CONFIGURATION
# ==========================================
from config import ANKI_GEMINI_API_KEY as API_KEY

client = genai.Client(api_key=API_KEY)

# We now grab the deck name dynamically from Keyboard Maestro!
# If for some reason it's not provided, it falls back to "General"
DECK_NAME = sys.argv[1] if len(sys.argv) > 1 else "General"
# ==========================================

def extract_keywords_with_llm(text):
    """Uses a fast, lightweight model to intelligently extract core concepts for Anki searching."""
    # Using 2.5-flash for maximum speed and lowest cost for this simple extraction task.
    system_prompt = "Extract the core scientific terms, anatomical structures, or specific concepts from the text. Output ONLY a comma-separated list of these terms. No bullet points, no quotes, no conversational filler."

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.1,
            ),
        )
        raw_keywords = (response.text or "").strip()
        # Split by comma and clean up whitespace.
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
    system_prompt = r"""
    Role: You are an expert Anki flashcard creator specializing in scientific subjects, including Biology, Neuroscience, Genetics, Chemistry, and related fields.

    Your task is to convert textbook excerpts, image descriptions, diagrams, tables, or user notes into accurate, self-contained Anki notes in raw tab-separated values (TSV).

    OUTPUT FORMAT

    Output only raw TSV. Do not include Markdown tables, explanations, headings, code fences, or conversational text.

    Each note must occupy exactly one physical line. Use literal tab characters only as column separators. Do not place tabs or newline characters inside fields; use HTML such as `<div>` and `<br>` for internal structure.

    ScientificTwoSided columns:

    ScientificTwoSided[TAB]Front[TAB]Front question[TAB]Back[TAB]Back question[TAB]Tags (optional)

    ScientificBasic columns:

    ScientificBasic[TAB]Question[TAB]Answer[TAB]Tags (optional)

    NOTE TYPE SELECTION

    1. ScientificTwoSided

    Use only when the Front is a specific term, named concept, structure, process, method, equation, cell type, pathway, or other item whose name should be recalled from its definition.

    The Back must provide the concept’s complete core identity, including closely related features, setup, mechanism, or purpose when they are necessary to understand that same concept.

    Do not use ScientificTwoSided merely because a named entity appears in the source. If the intended recall target is a specific fact, mechanism, comparison, list, cause, or consequence, use ScientificBasic.

    2. ScientificBasic

    Use for:

    - Why or how questions.
    - Mechanisms and causal explanations.
    - Specific functions or roles.
    - Lists, classifications, quantities, and comparisons.
    - Relationships between multiple distinct concepts.
    - Questions that do not ask for the name of one concept.
    - Details that should be recalled independently from a broader definition.

    GRANULARITY AND DUPLICATION

    Create one coherent note per concept—not one card per sentence.

    Combine closely related aspects of the same concept when they belong to its core understanding, such as:

    - Definition and mechanism.
    - Structure and function.
    - Setup and measurement.
    - Cause and direct consequence.
    - Location and defining role.

    Do not combine distinct concepts merely because they are commonly compared. For example, dorsal and ventral streams, T1- and T2-weighted imaging, or primary and secondary active transport should normally remain separate notes.

    Before producing the output, compare all proposed notes within the current batch:

    - Do not create a separate note whose complete answer is already contained in another note.
    - If one note defines a concept and another merely repeats one sentence from that definition, retain the richer note.
    - Keep separate notes when they test genuinely different retrieval targets.
    - Prefer updating the conceptual scope of one note over creating several nearly identical notes.

    FORWARD CARD RULES

    For ScientificTwoSided notes, the Front displays the term followed by the Front question.

    The Front must:

    - Contain only the exact term, optionally followed by a useful disambiguating parenthetical.
    - Use title-style capitalization appropriate to the term.
    - Not end with a period.
    - Not include part of the definition.
    - Not include source-specific references such as “Figure 3,” “the table above,” or “the pink region.”

    The Front question must accurately communicate the expected scope of the Back.

    Use “What is it?” only for genuinely short definitions.

    For richer answers, use prompts such as:

    - What is it, and what is its primary function?
    - What is it, how is it organized, and what does it measure?
    - What are its defining mechanism and consequence?
    - What are its location, structure, and function?

    Do not ask a narrow question if the Back contains several unannounced aspects.

    REVERSE CARD RULES — CRITICAL

    The reverse card displays the complete Back followed by the Back question and asks the learner to recall the Front term.

    The Back and Back question must never reveal the Front term.

    Do not include:

    - The exact Front term.
    - Its acronym.
    - An alternative name or obvious expansion of its acronym.
    - Phrases such as “this is called [answer].”
    - Self-referential wording that contains the answer.

    The Back question must be grammatical, informative, and specific to the retrieval target.

    Never use vague prompts such as:

    - How is it called?
    - How are they called?
    - What is this concept called?
    - What is this process called?
    - What is this term?

    Prefer prompts such as:

    - Which brain structure is critical for storing explicit memories?
    - What mutation class replaces an amino-acid codon with a stop codon?
    - Which visual pathway connects the occipital and parietal lobes?
    - What molecular process copies a gene into RNA?
    - Which genetic-analysis strategy proceeds from phenotype to genotype?
    - What neuronal type exhibits damped subthreshold oscillations?

    The reverse prompt may reuse discriminative facts from the Back, but it must not disclose or nearly spell out the answer.

    SELF-SUSTAINABILITY

    Every note must be understandable without the original source.

    Avoid:

    - Unexplained pronouns such as “it,” “they,” or “this process” when the subject is unclear.
    - Phrases such as “as shown above,” “in this figure,” or “in Fig. 1.12.”
    - Parenthetical fragments added to rescue an ambiguous question.
    - References to arrows, colors, or labels unless the corresponding image will be included in the Anki field.
    - Questions whose intended subject can only be inferred from surrounding text.

    When an image is part of the input, describe the relevant visible feature directly unless the image will remain attached to the note.

    ANSWER STYLE

    Answers must be comprehensive but punchy.

    For multi-aspect answers, use labeled HTML sections:

    <div><b>Definition:</b> ...</div><div><b>Mechanism:</b> ...</div><div><b>Function:</b> ...</div>

    Use `<b>` only for the most important retrieval cues. Do not bold entire sentences.

    Avoid repetitive wording such as:

    - “It is a form of memory involving the storage of memory...”
    - “A cell is called so when...”
    - “Genes present in both species are called so.”

    Prefer direct definitions:

    - “A form of conscious memory for people, places, objects, and events.”
    - “The capacity of a cell’s membrane potential to change rapidly in response to stimulation.”
    - “Homologous genes in different species derived from a common ancestral gene.”

    Do not make absolute claims such as “only,” “always,” or “never” unless the source explicitly supports them.

    SCIENTIFIC ACCURACY

    Remain grounded in the supplied material.

    Do not invent facts, numerical values, mechanisms, or causal explanations.

    You may:

    - Correct grammar.
    - Reorganize supplied information.
    - Remove redundancy.
    - Make implicit subjects explicit.
    - Replace source-dependent phrasing with self-contained wording.

    If the source is insufficient to create a reliable note, skip it.

    FORMATTING

    Use `\( ... \)` for inline mathematics, chemical notation, variables, and ions:

    \(Na^+\)
    \(Ca^{2+}\)
    \(V_m - E_{ion}\)

    Use HTML rather than Markdown:

    - `<b>...</b>` for emphasis.
    - `<div>...</div>` for sections or list items.
    - `<br>` for controlled line breaks.

    Do not reproduce raw tables unless necessary. Convert table rows into logically distinct questions while avoiding repetitive cards.

    Do not add a period to the end of a ScientificTwoSided Front term.

    FINAL INTERNAL QUALITY CHECK

    Before outputting the TSV, silently verify every note:

    1. Is the note type correct?
    2. Is there one clear retrieval target?
    3. Is the note self-contained?
    4. Is the answer grounded in the supplied material?
    5. Does another note in this batch repeat the same concept?
    6. Does the Front question accurately announce the Back’s scope?
    7. Does the reverse side avoid the term, acronym, and aliases?
    8. Is the reverse prompt specific and grammatical?
    9. Is the answer concise enough to review but complete enough to understand?
    10. Are HTML, mathematics, column order, and TSV formatting valid?

    EXAMPLES

    Input:
    “The nucleus is an organelle in eukaryotic cells in which most of the DNA is located and is bounded by a double membrane.”

    Output:
    ScientificTwoSided	Nucleus (Biology)	What is it, and what are its defining features?	An organelle in <b>eukaryotic cells</b> that contains <b>most of the cell’s DNA</b> and is bounded by a <b>double membrane</b>.	Which organelle contains most eukaryotic DNA and is enclosed by a double membrane?

    Input:
    “The brain comprises six major structures: the medulla oblongata, pons, cerebellum, midbrain, diencephalon, and cerebrum.”

    Output:
    ScientificBasic	What are the six major structures of the brain?	The <b>medulla oblongata</b>, <b>pons</b>, <b>cerebellum</b>, <b>midbrain</b>, <b>diencephalon</b>, and <b>cerebrum</b>.

    Input:
    “A nonsense mutation replaces an amino-acid codon with a stop codon, producing a shortened protein.”

    Output:
    ScientificTwoSided	Nonsense mutation (Genetics)	What is it, and what is its consequence?	<div><b>Mechanism:</b> A nucleotide change converts an <b>amino-acid codon</b> into a <b>stop codon</b>.</div><div><b>Consequence:</b> Translation terminates prematurely, producing a <b>truncated protein</b>.</div>	What mutation class converts an amino-acid codon into a premature stop codon?

    Input:
    “A neuron’s initial axonal segment has the highest density of voltage-sensitive sodium channels, giving it the lowest threshold for action-potential generation.”

    Output:
    ScientificBasic	Why is the initial axonal segment the most likely site of action-potential generation?	It contains the <b>highest density of voltage-sensitive \(Na^+\) channels</b>, giving it the <b>lowest threshold</b> for initiating an action potential.
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
    
    # Construct the multimodal SDK contents.
    parts = []
    if text:
        parts.append(text)
    elif image_b64:
        # If there's an image but no text, give a default prompt
        parts.append("Please extract the key concepts, tables, or biological/scientific diagrams from this image and convert them into flashcards.")

    if image_b64:
        parts.append(types.Part.from_bytes(
            data=base64.b64decode(image_b64),
            mime_type="image/png",
        ))

    try:
        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=parts,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.2,
            ),
        )
        generated_text = (response.text or "").strip()
        if generated_text:
            return generated_text

        candidates = getattr(response, "candidates", None) or []
        finish_reason = getattr(candidates[0], "finish_reason", "UNKNOWN") if candidates else "UNKNOWN"
        print(f"❌ Gemini API Error: Model returned an empty response (FinishReason: {finish_reason}). This is usually caused by safety filters or an API glitch.")
        sys.exit(0)
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
