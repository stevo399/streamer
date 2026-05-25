from unittest.mock import MagicMock, patch

from streamer.dj import (
    CloudTTSEngine,
    GeminiTTSEngine,
    TTSEngine,
    _is_raw_pcm_mime,
    _parse_audio_mime,
    _wrap_in_wav,
    generate_commentary,
    generate_dj_clip,
)


class TestTTSEngineContract:
    def test_cloud_tts_is_subclass(self):
        assert issubclass(CloudTTSEngine, TTSEngine)

    def test_gemini_tts_is_subclass(self):
        assert issubclass(GeminiTTSEngine, TTSEngine)

    def test_tts_engine_is_abstract(self):
        import inspect
        assert inspect.isabstract(TTSEngine)


class TestPCMHelpers:
    def test_parse_audio_mime_rate(self):
        assert _parse_audio_mime("audio/L16;rate=24000") == (24000, 1)

    def test_parse_audio_mime_rate_and_channels(self):
        assert _parse_audio_mime("audio/L16;rate=16000;channels=2") == (16000, 2)

    def test_parse_audio_mime_defaults(self):
        assert _parse_audio_mime("audio/L16") == (24000, 1)

    def test_is_raw_pcm_mime_l16(self):
        assert _is_raw_pcm_mime("audio/L16;rate=24000")

    def test_is_raw_pcm_mime_pcm(self):
        assert _is_raw_pcm_mime("audio/pcm;rate=22050")

    def test_is_raw_pcm_mime_wav_is_not_raw(self):
        assert not _is_raw_pcm_mime("audio/wav")

    def test_is_raw_pcm_mime_mp3_is_not_raw(self):
        assert not _is_raw_pcm_mime("audio/mpeg")

    def test_wrap_in_wav_header(self):
        pcm = b"\x00\x01" * 100
        wav = _wrap_in_wav(pcm, sample_rate=24000, channels=1)
        assert wav[:4] == b"RIFF"
        assert wav[8:12] == b"WAVE"
        assert wav[12:16] == b"fmt "
        assert wav[-len(pcm):] == pcm

    def test_wrap_in_wav_length(self):
        pcm = b"\x00" * 200
        wav = _wrap_in_wav(pcm, sample_rate=24000, channels=1)
        assert len(wav) == 44 + 200  # 44-byte WAV header


class TestGenerateCommentary:
    @patch("streamer.dj.genai")
    @patch("streamer.dj.GEMINI_API_KEY", "test-key")
    def test_returns_text(self, mock_genai):
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = MagicMock(text="Great episode!")
        mock_genai.Client.return_value = mock_client

        result = generate_commentary(
            "Some Show, Season 9, Episode 4 (from entertainment)",
            "Example Podcast, episode 287 (from podcast)",
        )
        assert result == "Great episode!"

    @patch("streamer.dj.genai")
    @patch("streamer.dj.GEMINI_API_KEY", "test-key")
    def test_returns_none_on_failure(self, mock_genai):
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = Exception("API error")
        mock_genai.Client.return_value = mock_client

        result = generate_commentary("prev", "next")
        assert result is None

    @patch("streamer.dj.GEMINI_API_KEY", "")
    def test_returns_none_without_api_key(self):
        result = generate_commentary("prev", "next")
        assert result is None


class TestCloudTTSEngine:
    @patch("streamer.dj.texttospeech")
    def test_returns_audio_bytes(self, mock_tts):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.audio_content = b"fake_audio_data"
        mock_client.synthesize_speech.return_value = mock_response
        mock_tts.TextToSpeechClient.return_value = mock_client

        result = CloudTTSEngine().synthesize("Hello world")
        assert result == b"fake_audio_data"

    @patch("streamer.dj.texttospeech")
    def test_returns_none_on_failure(self, mock_tts):
        mock_client = MagicMock()
        mock_client.synthesize_speech.side_effect = Exception("TTS error")
        mock_tts.TextToSpeechClient.return_value = mock_client

        result = CloudTTSEngine().synthesize("Hello world")
        assert result is None


class TestGeminiTTSEngine:
    def _make_mock_response(self, mock_genai, audio_data: bytes, mime_type: str):
        fake_part = MagicMock()
        fake_part.inline_data.data = audio_data
        fake_part.inline_data.mime_type = mime_type

        fake_candidate = MagicMock()
        fake_candidate.content.parts = [fake_part]

        mock_response = MagicMock()
        mock_response.candidates = [fake_candidate]

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response
        mock_genai.Client.return_value = mock_client

    @patch("streamer.dj.genai")
    @patch("streamer.dj.GEMINI_API_KEY", "test-key")
    def test_wraps_raw_pcm_in_wav(self, mock_genai):
        raw_pcm = b"\x01\x02" * 50
        self._make_mock_response(mock_genai, raw_pcm, "audio/L16;rate=24000")

        result = GeminiTTSEngine(voice_name="Kore").synthesize("Hello world")

        assert result is not None
        assert result[:4] == b"RIFF"   # WAV container
        assert result[8:12] == b"WAVE"
        assert result[-len(raw_pcm):] == raw_pcm

    @patch("streamer.dj.genai")
    @patch("streamer.dj.GEMINI_API_KEY", "test-key")
    def test_passes_through_non_pcm_audio(self, mock_genai):
        mp3_bytes = b"fake_mp3_data"
        self._make_mock_response(mock_genai, mp3_bytes, "audio/mpeg")

        result = GeminiTTSEngine(voice_name="Kore").synthesize("Hello world")
        assert result == mp3_bytes

    @patch("streamer.dj.genai")
    @patch("streamer.dj.GEMINI_API_KEY", "test-key")
    def test_returns_none_on_failure(self, mock_genai):
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = Exception("API error")
        mock_genai.Client.return_value = mock_client

        result = GeminiTTSEngine().synthesize("Hello world")
        assert result is None

    def test_default_voice_and_model(self):
        engine = GeminiTTSEngine()
        assert engine.voice_name == "Kore"
        assert engine.model == GeminiTTSEngine.DEFAULT_MODEL

    def test_custom_voice(self):
        engine = GeminiTTSEngine(voice_name="Puck")
        assert engine.voice_name == "Puck"


class TestGenerateDJClip:
    @patch("streamer.dj.generate_commentary")
    def test_returns_none_when_commentary_fails(self, mock_comm):
        mock_comm.return_value = None
        mock_engine = MagicMock(spec=TTSEngine)

        result = generate_dj_clip("prev.mp3", "next.mp3", engine=mock_engine)
        assert result is None
        mock_engine.synthesize.assert_not_called()

    @patch("streamer.dj.generate_commentary")
    def test_returns_none_when_tts_fails(self, mock_comm):
        mock_comm.return_value = "Nice episode!"
        mock_engine = MagicMock(spec=TTSEngine)
        mock_engine.synthesize.return_value = None

        result = generate_dj_clip("prev.mp3", "next.mp3", engine=mock_engine)
        assert result is None

    @patch("streamer.dj.decode_to_pcm")
    @patch("streamer.dj.generate_commentary")
    def test_returns_pcm_on_success(self, mock_comm, mock_dec):
        mock_comm.return_value = "Nice episode!"
        mock_dec.return_value = b"fake_pcm"
        mock_engine = MagicMock(spec=TTSEngine)
        mock_engine.synthesize.return_value = b"fake_audio"

        result = generate_dj_clip("prev.mp3", "next.mp3", engine=mock_engine)
        assert result == b"fake_pcm"
        mock_engine.synthesize.assert_called_once_with("Nice episode!")

    @patch("streamer.dj.decode_to_pcm")
    @patch("streamer.dj.generate_commentary")
    @patch("streamer.dj.TTS_ENGINE", "cloud")
    def test_defaults_to_cloud_tts_engine(self, mock_comm, mock_dec):
        mock_comm.return_value = "Nice episode!"
        mock_dec.return_value = b"fake_pcm"

        with patch("streamer.dj.CloudTTSEngine") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.synthesize.return_value = b"fake_audio"
            mock_cls.return_value = mock_instance

            result = generate_dj_clip("prev.mp3", "next.mp3")
            assert result == b"fake_pcm"
            mock_cls.assert_called_once()

    @patch("streamer.dj.decode_to_pcm")
    @patch("streamer.dj.generate_commentary")
    @patch("streamer.dj.TTS_VOICE", "Puck")
    @patch("streamer.dj.TTS_ENGINE", "gemini")
    def test_uses_gemini_tts_engine_from_config(self, mock_comm, mock_dec):
        mock_comm.return_value = "Nice episode!"
        mock_dec.return_value = b"fake_pcm"

        with patch("streamer.dj.GeminiTTSEngine") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.synthesize.return_value = b"fake_audio"
            mock_cls.return_value = mock_instance

            result = generate_dj_clip("prev.mp3", "next.mp3")
            assert result == b"fake_pcm"
            mock_cls.assert_called_once_with(voice_name="Puck")


class TestNotesIntegration:
    @patch("streamer.dj.genai")
    @patch("streamer.dj.GEMINI_API_KEY", "test-key")
    def test_commentary_includes_notes(self, mock_genai):
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = MagicMock(text="Great!")
        mock_genai.Client.return_value = mock_client

        generate_commentary(
            "Some Show, Season 1, Episode 5 (from entertainment)",
            "Example Podcast, episode 287 (from podcast)",
            prev_notes="[Show context]\nA show about testing.",
            next_notes="[Show context]\nA podcast about examples.",
        )

        call_args = mock_client.models.generate_content.call_args
        prompt = call_args.kwargs["contents"]
        assert "A show about testing." in prompt
        assert "A podcast about examples." in prompt
