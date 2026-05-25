import logging
import struct
import subprocess
from abc import ABC, abstractmethod

from google import genai
from google.cloud import texttospeech
from google.genai import types

from streamer.config import GEMINI_API_KEY, MEDIA_ROOTS, NOTES_DIR, TTS_ENGINE, TTS_VOICE
from streamer.context import format_track_context, load_notes, parse_track_context

log = logging.getLogger(__name__)

DJ_SYSTEM_PROMPT = (
    "You are a radio DJ on a personal streaming station that plays TV show "
    "audio and podcast episodes. Your style is witty, dry, sarcastic, and "
    "somewhat cynical. You make brief comments between tracks.\n\n"
    "You will be given a show name, season number, and episode number. Use "
    "your knowledge to identify the specific episode and comment on its plot, "
    "characters, memorable moments, or themes. If you recognize the episode "
    "from the show name and number, reference it specifically.\n\n"
    "For podcasts, comment on the podcast's style, hosts, or general themes "
    "if you recognize it.\n\n"
    "If transitioning between very different genres, riff on the tonal "
    "whiplash.\n\n"
    "When notes are provided about the content, use them to make your "
    "commentary more specific and informed.\n\n"
    "Keep it to 1-3 sentences. Never be mean-spirited, just amusingly jaded. "
    "If you truly don't recognize the content, keep it brief rather than "
    "making things up."
)


class DJError(Exception):
    pass


# ── TTS contract ──────────────────────────────────────────────────────────────


class TTSEngine(ABC):
    @abstractmethod
    def synthesize(self, text: str) -> bytes | None:
        """Convert text to raw audio bytes. Returns None on any failure."""


# ── PCM/WAV helpers ───────────────────────────────────────────────────────────


def _parse_audio_mime(mime_type: str) -> tuple[int, int]:
    """Return (sample_rate, channels) from a MIME like audio/L16;rate=24000."""
    sample_rate, channels = 24000, 1
    for field in mime_type.split(";"):
        field = field.strip()
        if field.startswith("rate="):
            try:
                sample_rate = int(field[5:])
            except ValueError:
                pass
        elif field.startswith("channels="):
            try:
                channels = int(field[9:])
            except ValueError:
                pass
    return sample_rate, channels


def _wrap_in_wav(pcm_bytes: bytes, sample_rate: int, channels: int) -> bytes:
    """Wrap raw signed 16-bit little-endian PCM in a RIFF/WAV container."""
    data_len = len(pcm_bytes)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_len, b"WAVE",
        b"fmt ", 16,
        1,                              # PCM
        channels,
        sample_rate,
        sample_rate * channels * 2,     # byte rate
        channels * 2,                   # block align
        16,                             # bits per sample
        b"data", data_len,
    )
    return header + pcm_bytes


def _is_raw_pcm_mime(mime_type: str) -> bool:
    """True for MIME types that represent raw (headerless) PCM audio."""
    m = mime_type.lower()
    return m.startswith("audio/l16") or m.startswith("audio/pcm") or (
        "pcm" in m and "wav" not in m
    )


# ── Implementations ───────────────────────────────────────────────────────────


class CloudTTSEngine(TTSEngine):
    """Google Cloud Text-to-Speech (WaveNet / Standard voices)."""

    def synthesize(self, text: str) -> bytes | None:
        try:
            client = texttospeech.TextToSpeechClient()
            synthesis_input = texttospeech.SynthesisInput(text=text)
            voice = texttospeech.VoiceSelectionParams(
                language_code="en-US",
                ssml_gender=texttospeech.SsmlVoiceGender.NEUTRAL,
            )
            audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3,
            )
            response = client.synthesize_speech(
                input=synthesis_input, voice=voice, audio_config=audio_config,
            )
            return response.audio_content
        except Exception as exc:
            log.warning("CloudTTSEngine.synthesize failed: %s", exc)
            return None


class GeminiTTSEngine(TTSEngine):
    """Gemini native TTS via the google-genai SDK.

    Uses generative audio models (e.g. gemini-2.5-flash-preview-tts) that
    produce expressive speech with emotion and pacing control via audio tags.
    Available voices: Kore, Puck, Zephyr, Charon, Fenrir, Aoede, and more.
    """

    DEFAULT_MODEL = "gemini-2.5-flash-preview-tts"

    def __init__(self, voice_name: str = "Kore", model: str = DEFAULT_MODEL):
        self.voice_name = voice_name
        self.model = model

    def synthesize(self, text: str) -> bytes | None:
        try:
            client = genai.Client(api_key=GEMINI_API_KEY)
            response = client.models.generate_content(
                model=self.model,
                contents=text,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=self.voice_name,
                            )
                        )
                    ),
                ),
            )
            part = response.candidates[0].content.parts[0]
            raw = part.inline_data.data
            mime = part.inline_data.mime_type or ""

            # Gemini TTS returns raw signed-16 PCM with no container header.
            # Wrap it in WAV so FFmpeg can identify the format reliably.
            if _is_raw_pcm_mime(mime):
                sample_rate, channels = _parse_audio_mime(mime)
                log.debug(
                    "GeminiTTSEngine: wrapping %d bytes of raw PCM "
                    "(%dHz, %dch) in WAV container",
                    len(raw), sample_rate, channels,
                )
                return _wrap_in_wav(raw, sample_rate, channels)

            return raw
        except Exception as exc:
            log.warning("GeminiTTSEngine.synthesize failed: %s", exc)
            return None


# ── Commentary + pipeline ─────────────────────────────────────────────────────


def generate_commentary(
    prev_context: str,
    next_context: str,
    prev_notes: str | None = None,
    next_notes: str | None = None,
) -> str | None:
    try:
        if not GEMINI_API_KEY:
            return None
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = f"Just finished: {prev_context}"
        if prev_notes:
            prompt += f"\n{prev_notes}"
        prompt += f"\nComing up: {next_context}"
        if next_notes:
            prompt += f"\n{next_notes}"
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=DJ_SYSTEM_PROMPT,
                max_output_tokens=1024,
            ),
        )
        return response.text.strip() if response.text else None
    except Exception as exc:
        log.warning("generate_commentary failed: %s", exc)
        return None


def decode_to_pcm(audio_bytes: bytes) -> bytes | None:
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-i", "pipe:0",
                "-f", "s16le", "-acodec", "pcm_s16le",
                "-ar", "44100", "-ac", "2", "pipe:1",
            ],
            input=audio_bytes,
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if proc.returncode != 0:
            log.warning(
                "decode_to_pcm: ffmpeg exited %d: %s",
                proc.returncode,
                proc.stderr.decode(errors="replace").strip(),
            )
            return None
        return proc.stdout
    except Exception as exc:
        log.warning("decode_to_pcm failed: %s", exc)
        return None


def generate_dj_clip(
    prev_track: str,
    next_track: str,
    roots: list | None = None,
    engine: TTSEngine | None = None,
) -> bytes | None:
    try:
        media_roots = roots if roots is not None else MEDIA_ROOTS
        root_strs = [str(r) for r in media_roots]
        prev_parsed = parse_track_context(prev_track, *root_strs)
        next_parsed = parse_track_context(next_track, *root_strs)
        prev_ctx = format_track_context(prev_parsed)
        next_ctx = format_track_context(next_parsed)

        notes_dir = str(NOTES_DIR) if NOTES_DIR else None
        prev_notes = load_notes(prev_parsed, notes_dir)
        next_notes = load_notes(next_parsed, notes_dir)

        text = generate_commentary(prev_ctx, next_ctx, prev_notes, next_notes)
        if not text:
            return None
        log.debug("DJ commentary: %s", text)

        if engine is not None:
            tts = engine
        elif TTS_ENGINE == "gemini":
            tts = GeminiTTSEngine(voice_name=TTS_VOICE)
        else:
            tts = CloudTTSEngine()

        audio = tts.synthesize(text)
        if not audio:
            return None

        return decode_to_pcm(audio)
    except Exception as exc:
        log.warning("generate_dj_clip failed: %s", exc)
        return None
