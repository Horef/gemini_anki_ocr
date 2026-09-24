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
  - Finds related existing cards with hybrid keyword + semantic search over a local,
    incrementally updated index, so similar cards are found even when worded differently.
  - Direct integration with local Anki app (`http://localhost:8765`).

- **Instant Mac LaTeX OCR (`ocr_gemini.py`)**:
  - Extracts text and equations from screenshots using `gemini-3.1-flash-lite`.
  - Automatically copies LaTeX output to macOS clipboard (`pbcopy`) with native system notifications.

---

## 🚀 Setup & Installation

### 1. Prerequisites
- **Conda** (Miniconda or Miniforge); the environment uses **Python 3.12.14**.
- **macOS** for desktop clipboard, notifications, and Keyboard Maestro integration. The Telegram bot can run independently on other platforms.
- **Google Gemini API Key**: Obtain from [Google AI Studio](https://aistudio.google.com/).
- **Telegram Bot Token** *(for mobile bot)*: Create a bot via [@BotFather](https://t.me/BotFather) on Telegram.
- **Anki + AnkiConnect** *(for desktop auto-import)*: Installed and running on `http://localhost:8765`.

### 2. Install Dependencies

From the downloaded or cloned project directory, create and activate the local
`gemini_api` environment:

```bash
conda env create --prefix ./gemini_api --file environment.yml
conda activate ./gemini_api
python -m pip check
```

If `conda activate` is unavailable, run `conda init` for your shell and reopen the
terminal first. Use the prefix (`./gemini_api`), because `conda activate gemini_api`
looks for a globally named environment instead of this project directory.

`environment.yml` pins Python, pip, and Tk (needed by the review window).
`requirements.txt` pins the Python dependencies, including transitive packages.
Conda chooses native builds for your platform; this is not a byte-identical
cross-platform lock. The setup was validated on Apple Silicon macOS.
The environment directory is ignored by Git; share the manifests, not the folder.

For an existing checkout after dependency changes:

```bash
conda env update --prefix ./gemini_api --file environment.yml
conda activate ./gemini_api
python -m pip check
python -m unittest discover -s tests -v
```

Updates do not necessarily remove obsolete pip packages; use a fresh checkout
and environment when verifying a clean reproduction. When deliberately upgrading
dependencies, test them in a clean environment and regenerate the pinned Python
requirements (excluding Conda-managed packaging tools):

```bash
python -m pip freeze --exclude packaging --exclude setuptools --exclude wheel > requirements.txt
```

Keep the Conda pins in `environment.yml` in sync with the tested environment.
Check that requirements contain version pins, not local `file://` build paths.

Without activation, run scripts with `./gemini_api/bin/python` on macOS/Linux
(or `.\gemini_api\python.exe` on Windows). Desktop scripts still require macOS.
Tests use the standard-library `unittest` runner; no extra test dependency is needed.

### 3. Configuration

Copy `config.example.py` to `config.py`:
```bash
cp config.example.py config.py
```

Open `config.py` and insert your API keys:
```python
ANKI_GEMINI_API_KEY = "your-gemini-api-key"
OCR_GEMINI_API_KEY = "your-gemini-api-key"

# "direct" (Google Gemini API) or "apigee" (AI gateway, no personal billing)
GEMINI_TRANSPORT = "direct"
APIGEE_API_KEY = ""
APIGEE_BASE_URL = ""

# Telegram Bot Token from @BotFather
TELEGRAM_BOT_TOKEN = "your-telegram-bot-token"

# Optional: Limit bot access to your Telegram user ID
ALLOWED_TELEGRAM_USER_IDS = []
```

Every setting can also be given as an environment variable of the same name, which
takes priority over `config.py`. With `GEMINI_TRANSPORT = "apigee"`, every Gemini
call (generation, token counting, embeddings, OCR, and the Telegram bot) goes through
the gateway using `APIGEE_API_KEY` as the `x-apikey` header; the per-script API keys
are then unused. Missing or invalid transport settings stop before any Gemini call.
`ANKI_GEMINI_EMBEDDING_MODEL` (default `gemini-embedding-2`) selects the retrieval
embedding model; changing it re-embeds the index automatically.

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

The configured **Gemini Anki Card** macro now offers two modes inside its existing
deck prompt:

- **Review later** (default): prepare all new notes and edits, show a short
  notification, and let you keep studying. Nothing is written to Anki yet.
- **Automatic**: create new notes and enrich existing notes immediately, without
  a review window. Validation and stale-note checks still apply. Conflicts marked
  `review` are left in the queue because they contain no applicable changes.

Until you review, every generation for the same deck is appended to one waiting
journal and one text preview instead of creating a file per operation. Different
decks keep separate journals because a journal has one target deck. When ready,
run **Review Gemini Anki Cards**. It automatically finds the newest waiting
journal across decks and opens a local review window. No file selection, copying
paths, or Terminal commands are needed. You can launch this macro through
your existing **Trigger Macro by Name** shortcut (`Control–Option–Command–T`),
then type its name. You can also assign it a dedicated hotkey in Keyboard Maestro.

In the review window:

- Select a note to see its fields, source evidence, and before/after changes.
- Click the **Apply** column to include/exclude a change, or press Space on the
  selected row. All applicable changes start selected; flagged conflicts cannot
  be selected for application. Select all / Select none are also available.
- **Apply selected & finish** applies the checked changes and discards the
  unchecked proposals. The batch leaves the queue.
- **Discard remaining** clears the batch without making additional Anki changes.
  Notes already created by an earlier normal run are not removed.
- **Later**, Escape, or closing the window keeps the batch for another time.

Finishing a journal moves its proposal and text preview into the hidden
`.anki_gemini_runs/archive/` folder. Original fields and write status remain
available for recovery; cleanup does not delete Anki notes or erase backups. The
next generation for that deck starts a new journal. It never opens another deck's
journal automatically while you are trying to return to studying.

The commands below use the project-local interpreter. Replace the absolute
project path with your checkout path and update the Execute Shell Script action
in each macro; creating the environment does not modify installed macros.

### Generation macro command

The mode variable `AnkiGenerationMode` uses the dropdown default
`Review later|Automatic`. Your existing deck selection and remembered deck are
preserved. The Execute Shell Script action saves results to `ScriptResult`; the
notification displays only `%Variable%ScriptResult%` so it no longer reports
“Card added” when changes are only queued.

```bash
case "${KMVAR_AnkiGenerationMode:-Review later}" in
  Automatic) anki_mode=--auto-apply ;;
  *) anki_mode=--preview-only ;;
esac
"/Users/sergiyhoref/Scripts/GeminiAnkiCards/gemini_api/bin/python" \
  "/Users/sergiyhoref/Scripts/GeminiAnkiCards/anki_gemini.py" \
  "${KMVAR_AnkiDeckName:-General}" "$anki_mode" --quiet 2>&1
```

The old text-only empty-clipboard condition is disabled. The script checks both
text and PNG clipboard content itself, so screenshots can reach the script.

### Review macro command

```bash
"/Users/sergiyhoref/Scripts/GeminiAnkiCards/gemini_api/bin/python" \
  "/Users/sergiyhoref/Scripts/GeminiAnkiCards/anki_gemini.py" \
  --review-latest --quiet 2>&1
```

This action displays its completion results in a notification. Keep it synchronous
and allow enough action timeout for a person to read the window. Review uses
Tkinter (available in your `gemini_api` environment) and makes no Gemini calls.
The view shows readable text and media references, not live Anki card templates or
rendered equations/images. Add `--review-deck "Deck Name"` to restrict latest-batch
selection to one exact saved deck. With no queued results it simply reports that
nothing is waiting.

The absolute Python path above is the interpreter already used by your macro.
If moving to another machine, use an interpreter with `google-genai` and Tkinter.
Quoted `KMVAR_` expansion passes deck names as data. See
[Keyboard Maestro's shell-script documentation](https://wiki.keyboardmaestro.com/action/Execute_a_Shell_Script).
The LaTeX OCR macro is unchanged.

---

## 📁 Repository Structure

```
├── anki_gemini.py        # Desktop entry point used by Keyboard Maestro
├── ocr_gemini.py         # Desktop LaTeX OCR script copying result to clipboard
├── telegram_bot.py       # Mobile Telegram Chatbot for Anki cards & LaTeX OCR
├── ankigen/              # Desktop card pipeline
│   ├── cli.py            # Arguments, clipboard, and the generation run
│   ├── retrieval.py      # Hybrid BM25 + embedding retrieval with rank fusion
│   ├── index.py          # Local SQLite index of notes and cached embeddings
│   ├── journal.py        # Proposal journals, review queue, verified apply
│   ├── actions.py        # Structured-output schema and action validation
│   ├── anki_connect.py   # AnkiConnect client
│   ├── llm.py            # Gemini client (direct or Apigee) and API calls
│   ├── settings.py       # Environment / config.py lookup
│   ├── review_window.py  # Local Tk review window
│   └── anki_prompt.txt   # Scientific note-writing rules
├── tests/                # Offline unittest suite (+ review_demo.py UI smoke test)
├── config.example.py     # Template configuration file (copy to gitignored config.py)
├── environment.yml       # Conda environment
└── requirements.txt      # Pinned Python dependencies
```

Local, gitignored state: `.anki_gemini_runs/` (proposal journals) and
`.anki_gemini_index/` (retrieval index; safe to delete, it is rebuilt).

---

## 📄 License

MIT License

## Desktop generation and existing-note updates

`anki_gemini.py` now compares complete existing answers, rather than skipping every
concept whose name already exists. The scientific note-writing rules live in
`ankigen/anki_prompt.txt`.

### What happens when you run the script?

| Mode | New notes | Existing-note edits | Generation calls |
| --- | --- | --- | --- |
| No mode flag | Applied automatically | Queued for review | At most 1 |
| `--preview-only` (KM Review later) | Queued | Queued | At most 1 |
| `--auto-apply` (KM Automatic) | Applied automatically | Applied automatically | At most 1 |
| `--review-latest` | Review/apply latest waiting batch | Review/apply latest waiting batch | 0 |
| `--apply RUN.json` | Apply outstanding creates | Apply outstanding edits | 0 |
| `--sync-index` | — | — | 0 (embeddings only) |

The plain CLI default is unchanged for compatibility. **The Keyboard Maestro
macro explicitly passes a mode**, so its Review later choice queues everything
and its Automatic choice applies both creates and updates. Generation never
opens a review window or waits for confirmation. Only `--review-latest` opens the
interactive window. `--auto-apply`, `--preview-only`, `--review-latest`,
`--apply`, and `--sync-index` are mutually exclusive.

Examples:

```bash
python3 anki_gemini.py "Medical Deck" --preview-only --quiet
python3 anki_gemini.py --review-latest
python3 anki_gemini.py "Medical Deck" --auto-apply --quiet
```

`--quiet` suppresses detailed token logs in notifications; usage is still saved
in proposal journals. Errors and retrieval guardrail warnings remain visible.
The generated `.json` journal and `.txt` preview live beside the script in
`.anki_gemini_runs/`, excluded from Git and written with owner-only permissions.
Additional generations append to the open journal for the same deck until it is
reviewed; their action indices and recovery statuses remain stable. Actions from
an Automatic or default-mode invocation are applied only for that invocation, so
previously queued work is not applied implicitly. Journals with no outstanding
edits or review flags are automatically archived. Uncertain/failed writes remain
available for investigation and block further appends to that journal. Journals
created by versions before aggregation remain separate, preserving their original
recovery records; the first generation after upgrading starts the shared journal.

For advanced path-based or selective application, the earlier interface remains:

```bash
python3 anki_gemini.py --apply .anki_gemini_runs/RUN.json --only 0 3
```

This applies only the selected zero-based indices and leaves other actions pending;
it does not perform the review window's finish/discard/archive step. Completed and
dismissed actions are never reapplied. `skip` and `review` actions never write to
Anki in any mode. Applying uses the saved deck and fields, not the clipboard, and
makes no Gemini calls. Neither Automatic nor review mode automatically resolves
conflicting information: clarify the source and regenerate if needed.

### Retrieval and decisions

1. **Index refresh (local, no Gemini).** All `ScientificBasic`/`ScientificTwoSided`
   notes are mirrored into `.anki_gemini_index/index.sqlite3`. AnkiConnect
   `notesModTime` identifies new or edited notes, so only those are re-read;
   deleted notes are dropped. Older AnkiConnect versions fall back to re-reading
   every note, and content digests still prevent unnecessary re-embedding.
2. **Embedding (cheap, cached).** Each note's title and answer are embedded once
   with `gemini-embedding-2` (768 dimensions) and re-embedded only when that text
   changes. Media-only notes have no searchable text and are skipped. The first run
   indexes your whole collection; run `anki_gemini.py --sync-index` once beforehand
   so the first generation is not slowed down. A generation run embeds at most
   `--embed-notes` new notes; the rest remain keyword-searchable until indexed.
3. **Hybrid search.** The clipboard text is split into sentence-aligned chunks
   (at most `--chunks`). For each chunk, local BM25 keyword search and embedding
   similarity each pick their top `--matches-per-query` notes in the selected deck
   (including subdecks); semantic matches must reach `--min-similarity`. A
   screenshot is embedded directly as an extra query. All lists are merged with
   reciprocal rank fusion, so notes found by several chunks or by both methods rank
   first, and at most `--context-notes` complete notes are passed on.
4. **Queued proposals.** Unreviewed create proposals for the same deck are ranked
   the same way and the similar ones are sent as `queued_proposals`, so clipping the
   same material twice before reviewing does not propose the same card twice.
5. **One comparison call** receives the source and the complete retrieved notes.
   It proposes `create`, `update`, `skip`, or `review`, with reasons and evidence.
   Enrichment must retain the original recall target; independent facts belong in
   separate notes, and conflicts should be flagged for review.
6. The entire response is validated before any imports. Unknown IDs, unsupported
   fields, incomplete answers, duplicate actions, malformed tags, literal reverse
   answer leakage, and removal of existing media references reject the batch.
   Non-STOP responses, including output truncation, also reject the batch.

If embeddings fail (network, quota, gateway), retrieval continues with keyword
ranking only and says so on stderr. An image-only clipboard cannot be searched
without embeddings, so that case stops before generation. AnkiConnect failures
always stop the run. Similarity is only used to choose which notes Gemini sees;
Gemini, not a threshold, decides whether something is a duplicate. There is no
conversation history, autonomous tool loop, model repair, continuation, or
automatic retry. A successful ordinary run uses exactly **one generation call**.

### Generous guardrails

These defaults are intended to catch unusually large or runaway requests, rather
than aggressively minimize ordinary usage. All numeric options must be positive.

| Option | Default | Meaning |
| --- | ---: | --- |
| `--source-tokens` | 64,000 | Maximum source input, including screenshot tokens |
| `--context-tokens` | 48,000 | Token budget for the complete retrieved-note JSON |
| `--request-tokens` | 128,000 | The generation request's counted input, including instructions/schema as text plus a 1,024-token framing reserve |
| `--output-tokens` | 24,000 | Comparison generation output ceiling |
| `--context-notes` | 30 | Maximum complete notes (and queued proposals) passed to comparison |
| `--actions` | 100 | Maximum create/update/skip/review proposals in each generation response |
| `--chunks` | 24 | Maximum source chunks used as search queries |
| `--matches-per-query` | 5 | Keyword and semantic matches kept per chunk |
| `--embed-notes` | 5,000 | Maximum notes embedded during one generation run (`--sync-index` embeds all) |
| `--min-similarity` | 0.65 | Cosine floor for semantic matches (0–1); raise it if unrelated notes appear, lower it if duplicates are missed |

For example, `--context-tokens 80000 --context-notes 60 --request-tokens 180000`
raises retrieval context allowance. The configured model must support your chosen
input/output sizes; the script does not silently clamp them or switch models.

The source is never silently truncated: oversized source or final requests stop.
When context is too large, the script selects the longest ranked prefix of whole
notes that fits, using bounded binary-search token counting. Lower-ranked notes
are omitted, not summarized or partially supplied. A very large first note can
leave the context empty; the script explicitly reports this and reduced duplicate
coverage. Candidate/context limit messages go to stderr.

Token counting is additional API traffic, but not generation. Normally there are
three count requests: source, context, comparison request. With 30 context notes,
context fitting adds at most five more counting requests.
Counts for instructions/schema use their text representation plus a reserve, so
this is a request guardrail rather than an exact billing prediction. Returned
usage metadata, including available thinking-token counts and embedded item counts,
is printed to stderr and stored in successful proposal journals. With `--quiet`,
the stderr usage log is suppressed; a failure before journal creation may therefore
leave no usage record. The generation sends `max_output_tokens` and uses low
thinking. There is no separate fixed dollar cap.

The comparison model defaults to `gemini-3.7-flash`; `--model` overrides it, but
replacements must support structured JSON and the low thinking level. Use a recent
`google-genai` SDK supporting `HttpRetryOptions`, `response_json_schema`,
`thinking_level`, and `gemini-embedding-2`.
HTTP requests use one attempt only: Anki timeout 20 seconds, Gemini timeout 180
seconds. Clipboard subprocesses time out after 15 seconds; raw clipboard size is
also capped at 2 MB text and 15 MB PNG before API submission.

### Applying and recovering changes

Updates call AnkiConnect `updateNoteFields` on the existing note ID and only send
changed fields. They preserve unrelated fields, tags, and embedded media; they do
not delete/recreate notes or explicitly modify scheduling. Both directions of a
ScientificTwoSided note are proposed together. Literal term leakage is checked in
code; scientific correctness, aliases, subtle answer leakage, and note scope still
need human review.

Before each update, the script re-reads all original fields, model, and tags and
checks deck membership. If any changed since generation, it stops. This is an
optimistic check, not an atomic Anki transaction: avoid editing the same notes
while applying. Writes are verified by reading the fields afterward.

The JSON journal contains original fields and is saved before each write. If the
process loses an API response or fails verification, the action remains `pending`
and is **not automatically retried**. Inspect the actual Anki note and journal
before recovering; do not simply clear the status and rerun. Completed earlier
actions remain applied: a batch is not transactional. For an update you wish to
undo, the original fields are available in the journal for manual restoration;
there is no automatic rollback or deletion of created notes. A `.lock` file blocks
concurrent review/application of the same proposal and stays held while the review
window is open. Failed or uncertain writes are never discarded or archived by the
review flow. If a process is killed, remove its
stale lock only after checking that no apply process is active and reviewing any
pending action.

Anki/API failures stop the workflow rather than pretending no duplicates exist.
Retrieval is bounded, so a duplicate can still be missed if it ranks below the
context limit; Anki's `allowDuplicate=False` is an additional exact-match check.
Screenshots are used as source material but are not automatically attached to new
notes. Model-returned tags are honored for creates; updates preserve existing tags.

Offline tests (no credentials, Gemini calls, or Anki changes):

```bash
python3 -m unittest discover -s tests -v
```
