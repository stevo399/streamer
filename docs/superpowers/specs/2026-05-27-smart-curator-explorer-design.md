# Smart Curator & Library Explorer Design

## Goal

Make the curator chat actually knowledgeable by switching it to Gemini, and build an automated library explorer that generates show-level notes files so both the curator and DJ have rich context about the user's media library. Provide live SSE-based progress tracking for the explorer in both the API and control panel.

## Architecture

Three changes that build on each other:

1. **Curator chat switches to Gemini** — the `chat()` method uses `genai.Client` instead of Ollama. The periodic curator check (pass/queue decisions) stays on Ollama since it's structured JSON and works fine locally.
2. **Library explorer** — a new `explorer.py` module that iterates shows/podcasts and asks Gemini to write a `show.md` for each one. Writes to `NOTES_DIR`, which the existing notes system already reads.
3. **SSE progress** — the explorer publishes events to subscriber queues. An SSE endpoint streams them to the control panel in real time.

## 1. Curator Chat — Gemini Upgrade

### What changes

- `Curator.chat()` builds messages the same way but sends them to Gemini via `genai.Client` instead of Ollama's `/api/chat`
- `_extract_and_queue()` is unchanged — it parses JSON from the response regardless of which model produced it
- `get_chat_history()` unchanged
- The periodic check (`_check`, `_ask_ollama`) stays on Ollama — no change

### Config

- New env var: `CURATOR_CHAT_MODEL` — defaults to `gemini-2.5-flash`. If set to `ollama`, falls back to the existing Ollama path.
- Uses existing `GEMINI_API_KEY` (already configured for DJ)
- Uses existing `OLLAMA_URL` / `OLLAMA_MODEL` for the fallback path

### Prompt

`CURATOR_CHAT_PROMPT` stays the same — it already describes the curator's role, capabilities, and JSON queue format. Gemini will just be much better at answering questions like "which Family Guy episode has Republican Town."

## 2. Library Explorer

### New module: `src/streamer/explorer.py`

Responsible for scanning the media library and generating notes files using Gemini.

### ExplorerStatus

A thread-safe status object shared between the explorer thread and API consumers:

```
ExplorerStatus:
    running: bool
    total: int           # number of shows to process
    completed: int       # shows finished so far
    current_show: str    # name of the show currently being processed
    log: list[str]       # recent event messages (capped at 100)
    error: str | None    # set if the run failed
    _subscribers: list[queue.Queue]  # SSE listeners
```

Methods:
- `push_event(event_dict)` — appends to log, pushes to all subscriber queues
- `subscribe() -> queue.Queue` — adds a new subscriber, returns its queue
- `unsubscribe(q)` — removes a subscriber queue

### Exploration flow

1. Collect all show/podcast directories from scanner roots
2. Filter out shows that already have `{NOTES_DIR}/{show_name}/show.md` (unless `force=True`)
3. Set `status.total` to the number of shows remaining
4. For each show:
   - Set `status.current_show`
   - Collect season names and episode file stems
   - Build a prompt: "Here is a show called {name} with these seasons/episodes: {list}. Write a concise description covering: what the show is about, its tone and themes, and any particularly notable or fan-favorite episodes. If you don't recognize the show, say so briefly rather than fabricating details."
   - Call Gemini (`gemini-2.5-flash`)
   - Write response to `{NOTES_DIR}/{show_name}/show.md`
   - Increment `status.completed`, push event
5. Push `finished` event

### Error handling

- If a single show fails (API error, timeout), log the error, skip it, continue to the next
- If the Gemini API key is missing, fail immediately with an error event
- The explorer thread catches all exceptions so it never crashes silently

### Config

- `NOTES_DIR` — required to run the explorer. If not set, the `/api/explorer/start` endpoint returns an error telling the user to configure it. No magic defaults — the user picks where notes live.
- `GEMINI_API_KEY` — required (already exists)

### Skip/force behavior

- Default: skip shows that already have a `show.md`
- `force=True`: regenerate all notes, overwriting existing files
- This makes re-runs incremental — safe to trigger after adding new media

## 3. SSE Progress Streaming

### API Endpoints

All under the "Explorer" tag in OpenAPI docs.

**`POST /api/explorer/start`**
- Body: `{"force": false}` (optional, defaults to false)
- Returns 200 `{"ok": true, "total": N}` and starts the background thread
- Returns 409 `{"ok": false, "error": "Already running"}` if explorer is active

**`GET /api/explorer/status`**
- Returns current `ExplorerStatus` as JSON snapshot
- Response: `{"running": bool, "total": int, "completed": int, "current_show": str, "log": [...], "error": str|null}`

**`GET /api/explorer/progress`**
- SSE stream (`text/event-stream`)
- No auth required (same pattern as `/stream.ogg`)
- On connect: sends an initial `status` event with the current state
- Then streams events as they occur:
  - `{"type": "exploring", "show": "Family Guy", "completed": 5, "total": 42}`
  - `{"type": "completed", "show": "Family Guy", "completed": 6, "total": 42}`
  - `{"type": "finished", "completed": 42, "total": 42}`
  - `{"type": "error", "message": "Gemini API key not configured"}`
- Client subscribes on connect, unsubscribes on disconnect

### SSE implementation

- `StreamingResponse` with `media_type="text/event-stream"` and `Cache-Control: no-cache`
- Generator function subscribes to `ExplorerStatus`, blocks on `queue.Queue.get(timeout=30)`
- On timeout with no event, sends a `:keepalive\n\n` comment to prevent connection drop
- On client disconnect, the generator's finally block calls `unsubscribe()`

## 4. Control Panel Updates

New section in `index.html` between the Chat section and the Actions nav:

### "Library Explorer" section

- **"Generate Notes" button** — POST to `/api/explorer/start`, then opens `EventSource`
- **"Regenerate All" button** — same but with `force: true`
- **Progress bar** — `<progress>` element, updated on each SSE event
- **Current show label** — shows which show is being processed
- **Event log** — scrollable div with `role="log"` and `aria-live="polite"`, appends each event as a line
- Button disables while running, re-enables on `finished` or `error`
- On page load, GET `/api/explorer/status` — if running, reconnect the EventSource and resume display

## 5. Files Changed

- **Create:** `src/streamer/explorer.py` — ExplorerStatus, exploration logic, Gemini calls
- **Modify:** `src/streamer/curator.py` — `chat()` method gains Gemini path
- **Modify:** `src/streamer/config.py` — add `CURATOR_CHAT_MODEL`, default `NOTES_DIR`
- **Modify:** `src/streamer/server.py` — three new explorer API endpoints, response models
- **Modify:** `src/streamer/templates/index.html` — explorer section with SSE progress
- **Create:** `tests/test_explorer.py` — explorer unit tests
- **Modify:** `tests/test_curator.py` — update chat tests for Gemini path
- **Modify:** `tests/test_server.py` — explorer API endpoint tests
