# Streamer

A local network audio streaming server that continuously broadcasts audio files as a radio-style stream. All connected clients hear the same audio at the same point. Includes a web control panel, AI DJ, smart curator, library explorer, and a full REST API.

## Features

- Radio-style streaming: all listeners share the same playback position
- OGG Vorbis and MP3 stream endpoints
- Web control panel with file browser, queue management, and playback controls
- Smart shuffle with folder-weighted selection and repeat avoidance
- AI DJ with Gemini-generated commentary and text-to-speech
- AI Curator that monitors playback and suggests themed playlists
- Curator chat: talk to the curator to request specific content by title or theme
- Library Explorer that generates AI-powered descriptions for your media library
- REST API with OpenAPI documentation at `/docs`
- HTTP Basic Auth for the control panel (streams stay open)
- Screen reader accessible UI

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- [FFmpeg](https://ffmpeg.org/) on PATH
- [ripgrep](https://github.com/BurntSushi/ripgrep) on PATH (for title-based track search)

## Setup

1. Clone the repo and install dependencies:

   ```
   uv sync
   ```

2. Copy `.env.sample` to `.env` and configure your media folders:

   ```
   cp .env.sample .env
   ```

3. Start the server:

   ```
   uv run streamer
   ```

The server starts on `0.0.0.0:8054` by default:

- Control panel: `http://localhost:8054`
- API docs: `http://localhost:8054/docs`
- OGG stream: `http://localhost:8054/stream.ogg`
- MP3 stream: `http://localhost:8054/stream.mp3`

## Configuration

All settings are in `.env` (see `.env.sample` for all options):

| Variable | Description | Default |
|----------|-------------|---------|
| `MEDIA_ROOTS` | Comma-separated paths to media folders | *(required)* |
| `HOST` | Server bind address | `0.0.0.0` |
| `PORT` | Server port | `8054` |
| `GEMINI_API_KEY` | Gemini API key for DJ, curator chat, and explorer | |
| `TTS_ENGINE` | DJ text-to-speech engine: `cloud` or `gemini` | `cloud` |
| `TTS_VOICE` | Voice name when `TTS_ENGINE=gemini` | `Kore` |
| `CURATOR_CHAT_MODEL` | Curator chat model: `gemini-2.5-flash` or `ollama` | `gemini-2.5-flash` |
| `OLLAMA_URL` | Ollama server URL for periodic curator checks | `http://localhost:11434` |
| `OLLAMA_MODEL` | Ollama model for curator checks | `llama3.1` |
| `NOTES_DIR` | Directory for show/episode notes and explorer output | |
| `AUTH_USERNAME` | Basic auth username | |
| `AUTH_PASSWORD_HASH` | bcrypt hash of password | |

## Authentication

The control panel can be password-protected with HTTP Basic Auth. Stream endpoints remain open so media players can connect without credentials.

1. Generate a password hash:

   ```
   uv run streamer-hashpw
   ```

2. Add the credentials to your `.env`:

   ```
   AUTH_USERNAME=admin
   AUTH_PASSWORD_HASH=$2b$12$...the hash from step 1...
   ```

3. Restart the server.

## AI DJ

The DJ generates commentary between tracks using Google Gemini and speaks it via text-to-speech. Enable it from the control panel.

1. Get a Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey) and add it to your `.env`:

   ```
   GEMINI_API_KEY=your-key-here
   ```

2. For Cloud TTS (`TTS_ENGINE=cloud`), install the [gcloud CLI](https://cloud.google.com/sdk/docs/install) and authenticate:

   ```
   gcloud auth application-default login
   ```

   For Gemini TTS (`TTS_ENGINE=gemini`), only the `GEMINI_API_KEY` is needed.

3. Toggle DJ mode on from the control panel.

## AI Curator

The curator periodically reviews recent play history and can intervene with themed playlists, marathons, or genre-focused queues. It uses Ollama for periodic checks and Gemini (by default) for the interactive chat.

You can chat with the curator directly from the control panel to request specific episodes by title, ask for recommendations, or queue content by theme.

Track resolution supports episode numbers, titles ("Get Schwifty"), and partial filename matches, using ripgrep for filesystem search when exact catalog lookups miss.

### Setup

1. Set `GEMINI_API_KEY` in your `.env` for curator chat.

2. For periodic curator checks, install [Ollama](https://ollama.ai/) and pull a model:

   ```
   ollama pull llama3.1
   ```

3. Enable the curator from the control panel.

To use Ollama for chat instead of Gemini, set `CURATOR_CHAT_MODEL=ollama` in your `.env`.

## Library Explorer

The explorer generates AI-powered descriptions for every directory in your media library. It walks the full directory tree recursively and writes a `show.md` note for each directory, mirroring your library structure in the notes folder.

1. Set `GEMINI_API_KEY` and `NOTES_DIR` in your `.env`.

2. Click "Generate Notes" in the control panel. Progress streams live via SSE.

Re-runs skip directories that already have notes. Use "Regenerate All" to overwrite.

## Notes

The DJ and curator use notes from `NOTES_DIR` to provide context-aware commentary. The explorer can generate these automatically, or you can write them by hand:

```
NOTES_DIR/
  Show Name/
    show.md              # show-level description
    season 01/
      show.md            # season-level description
      episode_stem.md    # per-episode notes
  Podcast Name/
    show.md
    episode_stem.md
```

## Network Access

To listen from other devices on your local network, allow the server port through your firewall. On Windows:

```powershell
New-NetFirewallRule -DisplayName "Streamer" -Direction Inbound -LocalPort 8054 -Protocol TCP -Action Allow
```

Then connect from other devices at `http://<your-ip>:8054/stream.ogg`.

## Tests

```
uv run pytest
```
