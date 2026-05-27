# FastAPI Migration & API Design

## Overview

Replace the Flask web framework with FastAPI. Migrate all existing HTML/streaming routes, add a comprehensive JSON API for programmatic control, and update the control panel to surface new capabilities (track timing, curator chat, curator status, force-check).

## Architecture Decisions

- **Single-file replacement:** `server.py` is rewritten as a FastAPI app. All routes (HTML pages, JSON API, streaming) live in one file. No router split — the app isn't complex enough to justify it.
- **Auth:** HTTP Basic Auth with bcrypt, same as today. Implemented as a FastAPI dependency via `HTTPBasic` security scheme.
- **Templates:** Jinja2Templates from `fastapi.templating`, same `templates/` directory.
- **Server:** uvicorn replaces Flask's dev server in `__main__.py`.
- **Dependencies:** Drop `flask`, `flask-httpauth`. Add `fastapi`, `uvicorn`, `python-multipart`, `jinja2`.

## API Endpoints

All JSON endpoints live under `/api/`. All require HTTP Basic Auth.

### Now Playing

| Endpoint | Method | Response |
|---|---|---|
| `/api/now-playing` | GET | `{"track_name", "track_path", "elapsed", "duration", "remaining"}` |

`elapsed`, `duration`, `remaining` are floats in seconds. `duration` and `remaining` are `null` if ffprobe couldn't determine duration.

### Track Control

| Endpoint | Method | Body | Response |
|---|---|---|---|
| `/api/tracks/next` | POST | — | `{"ok": true}` |
| `/api/tracks/previous` | POST | — | `{"ok": true, "track": "..."}` or `{"ok": false}` if no history |
| `/api/tracks/play` | POST | `{"path": "browse/relative/path"}` | `{"ok": true}` or `{"ok": false}` |

The `path` in `/api/tracks/play` uses browse-relative paths (e.g., `entertainment/Show/season 01/01.mp3`), resolved via `scanner.resolve_browse_path()` for safety.

### Queue

| Endpoint | Method | Body/Params | Response |
|---|---|---|---|
| `/api/queue` | GET | — | `{"queue": [{"name", "path", "index"}]}` |
| `/api/queue` | POST | `{"path": "browse/relative/path"}` | `{"ok": true}` or `{"ok": false}` |
| `/api/queue/{index}` | DELETE | — | `{"ok": true}` or `{"ok": false}` |

### DJ

| Endpoint | Method | Body | Response |
|---|---|---|---|
| `/api/dj` | GET | — | `{"enabled": bool}` |
| `/api/dj` | POST | `{"enabled": bool}` | `{"enabled": bool}` |

### Curator

| Endpoint | Method | Body | Response |
|---|---|---|---|
| `/api/curator` | GET | — | `{"enabled", "reason", "tracks_since_check", "next_check_at"}` |
| `/api/curator` | POST | `{"enabled": bool}` | `{"enabled": bool}` |
| `/api/curator/force` | POST | — | `{"ok": true}` |
| `/api/curator/chat` | GET | — | `{"messages": [{"role", "content"}]}` |
| `/api/curator/chat` | POST | `{"message": "..."}` | `{"response": "...", "queued": [...]}` |

### File Browsing

| Endpoint | Method | Response |
|---|---|---|
| `/api/browse` | GET | `{"dirs": [{"name", "path"}], "files": []}` |
| `/api/browse/{path}` | GET | `{"dirs": [{"name", "path"}], "files": [{"name", "path"}]}` |

Paths in responses are browse-relative (e.g., `entertainment/Show/season 01`). These same paths are used in `/api/tracks/play` and `/api/queue` POST bodies.

### Legacy State Endpoint

`/api/state` remains for backward compatibility with the control panel JS. It gets enriched with elapsed/remaining/duration fields.

## Pipeline Changes

### Track Duration (ffprobe)

When `_start_decoder` is called, also run:
```
ffprobe -v error -show_entries format=duration -of csv=p=0 <path>
```

Store the result as `_track_duration: float | None` on the pipeline.

### Elapsed Time Tracking

Promote the existing local `bytes_written` variable in `_run()` to an instance variable `_track_bytes_written`, reset to 0 at the start of each track.

### Exposure Method

```python
def get_playback_info(self) -> dict:
    elapsed = self._track_bytes_written / BYTES_PER_SECOND
    return {
        "elapsed": round(elapsed, 1),
        "duration": round(self._track_duration, 1) if self._track_duration else None,
        "remaining": round(self._track_duration - elapsed, 1) if self._track_duration else None,
    }
```

## Curator Chat

### System Prompt

A separate `CURATOR_CHAT_PROMPT` oriented toward conversation rather than JSON decisions. The curator can answer questions about the library, take requests, and recommend content. If asked to play something, it responds with a JSON block containing tracks to queue.

### Conversation Flow

1. User POSTs `{"message": "..."}` to `/api/curator/chat`
2. Build Ollama request: chat system prompt + compact catalog summary + recent play history + chat history + new user message
3. Ollama responds conversationally
4. If response contains a JSON block matching the curator's queue format (`{"action": "queue", "tracks": [...]}`) — detected by searching the response text for a JSON object with `"action": "queue"` — resolve and queue those tracks using the existing `_resolve_tracks` method
5. Append user message and response to `_chat_history`
6. Return `{"response": "...", "queued": [...]}`

### State

- `_chat_history: list[dict]` on the Curator object — `{"role": "user"|"assistant", "content": "..."}`
- Session-scoped (cleared on restart, no persistence)
- GET `/api/curator/chat` returns the full history

### Curator Status

Expose existing internal counters via a new method:

```python
def get_status(self) -> dict:
    return {
        "enabled": self.state.curator_enabled,
        "reason": self.state.curator_reason,
        "tracks_since_check": self._tracks_since_check,
        "next_check_at": self._next_check_at,
    }
```

## Control Panel Updates

All new sections use the existing JS polling pattern (5-second interval against `/api/state`).

### Track Timing Display

Below the track name, show elapsed/remaining as `0:00 / 3:45`. Uses `aria-live="off"` on the timer element to avoid screen reader announcing every poll update. The track name change (already `aria-live="polite"`) remains the meaningful announcement.

### Curator Status + Force

Below the curator on/off toggle:
- Text showing "Tracks until next check: N"
- A "Force Check" button that POSTs to `/api/curator/force`

### Curator Chat

New section with:
- Scrollable message list (alternating user/curator messages)
- Text input with `<label>` for accessibility
- Send button
- Message list uses `aria-live="polite"` for new messages
- Send POSTs to `/api/curator/chat`, then refreshes the message list

### Browse Enhancement

The browse confirmation page (`play.html`) already has Play Now and Add to Queue. No structural changes needed — the existing form actions get updated to the new FastAPI routes.

## Migrated HTML Routes

These routes keep the same paths and behavior, just move from Flask to FastAPI:

- `GET /` — control panel (TemplateResponse)
- `GET /browse/`, `GET /browse/{path}` — file browser
- `GET /browse/play` — play confirmation page
- `POST /next`, `POST /previous` — track control (RedirectResponse)
- `POST /play` — play now from browse
- `POST /queue/add`, `POST /queue/remove` — queue management
- `POST /dj/toggle`, `POST /curator/toggle` — AI toggles
- `GET /stream.ogg`, `GET /stream.mp3` — audio streams (StreamingResponse)

## Security

- All routes require HTTP Basic Auth except streaming endpoints (which remain open as they are today)
- Browse/play/enqueue paths are resolved through `scanner.resolve_browse_path()` which validates against configured media roots and blocks path traversal
- No new secrets or credentials beyond existing .env values
