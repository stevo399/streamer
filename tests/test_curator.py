import json
from unittest.mock import MagicMock, patch

import pytest

from streamer.curator import Curator
from streamer.state import ServerState


def _make_curator(state=None, scanner=None):
    state = state or ServerState()
    scanner = scanner or MagicMock()
    scanner.roots = []
    return Curator(state, scanner)


class TestHandleResponse:
    def test_pass_response(self):
        curator = _make_curator()
        path_lookup = {"Show/season 01/01": "/path/to/01.mp3"}
        curator._handle_response({"action": "pass"}, path_lookup)
        assert curator.state.queue == []

    def test_queue_response(self):
        curator = _make_curator()
        path_lookup = {
            "Show/season 01/01": "/path/to/01.mp3",
            "Show/season 01/02": "/path/to/02.mp3",
        }
        response = {
            "action": "queue",
            "tracks": ["Show/season 01/01", "Show/season 01/02"],
            "reason": "Marathon: Show Season 1",
        }
        curator._handle_response(response, path_lookup)
        assert len(curator.state.queue) == 2
        assert curator.state.queue[0] == "/path/to/01.mp3"
        assert curator.state.curator_reason == "Marathon: Show Season 1"

    def test_unresolved_tracks_skipped(self):
        curator = _make_curator()
        path_lookup = {"Show/season 01/01": "/path/to/01.mp3"}
        response = {
            "action": "queue",
            "tracks": ["Show/season 01/01", "Nonexistent/season 01/99"],
            "reason": "Test",
        }
        curator._handle_response(response, path_lookup)
        assert len(curator.state.queue) == 1

    def test_invalid_response_ignored(self):
        curator = _make_curator()
        curator._handle_response({"garbage": True}, {})
        assert curator.state.queue == []

    def test_empty_tracks_no_reason_set(self):
        curator = _make_curator()
        response = {"action": "queue", "tracks": ["bogus/path"], "reason": "Test"}
        curator._handle_response(response, {})
        assert curator.state.curator_reason is None

    def test_fuzzy_case_mismatch(self):
        curator = _make_curator()
        path_lookup = {"Red Dwarf/season 01/01": "/path/to/01.mp3"}
        response = {
            "action": "queue",
            "tracks": ["Red Dwarf/Season 01/01"],
            "reason": "Test",
        }
        curator._handle_response(response, path_lookup)
        assert len(curator.state.queue) == 1

    def test_fuzzy_season_format_mismatch(self):
        curator = _make_curator()
        path_lookup = {"Rick and Morty/Season 1 (2013)/01": "/path/to/01.mp3"}
        response = {
            "action": "queue",
            "tracks": ["Rick and Morty/Season 1/01"],
            "reason": "Test",
        }
        curator._handle_response(response, path_lookup)
        assert len(curator.state.queue) == 1

    def test_fuzzy_episode_number_padding(self):
        curator = _make_curator()
        path_lookup = {"Show/season 01/01": "/path/to/01.mp3"}
        response = {
            "action": "queue",
            "tracks": ["Show/Season 1/1"],
            "reason": "Test",
        }
        curator._handle_response(response, path_lookup)
        assert len(curator.state.queue) == 1

    def test_fuzzy_folder_expands_to_files(self):
        curator = _make_curator()
        path_lookup = {
            "Main Menu/2020/ep01": "/path/to/ep01.mp3",
            "Main Menu/2020/ep02": "/path/to/ep02.mp3",
            "Main Menu/2021/ep03": "/path/to/ep03.mp3",
        }
        response = {
            "action": "queue",
            "tracks": ["Main Menu/2020"],
            "reason": "Main Menu 2020 marathon",
        }
        curator._handle_response(response, path_lookup)
        assert len(curator.state.queue) == 2
        assert curator.state.curator_reason == "Main Menu 2020 marathon"

    def test_fuzzy_episode_with_title_stem(self):
        curator = _make_curator()
        path_lookup = {
            "Rick and Morty/Season 1 (2013)/[S01.E01] Pilot": "/path/to/pilot.mp3",
            "Rick and Morty/Season 1 (2013)/[S01.E02] Lawnmower Dog": "/path/to/lawnmower.mp3",
            "Rick and Morty/Season 1 (2013)/[S01.E03] Anatomy Park": "/path/to/anatomy.mp3",
        }
        response = {
            "action": "queue",
            "tracks": [
                "Rick and Morty/Season 1/01",
                "Rick and Morty/Season 1/02",
                "Rick and Morty/Season 1/03",
            ],
            "reason": "Marathon: Rick and Morty S1",
        }
        curator._handle_response(response, path_lookup)
        assert len(curator.state.queue) == 3
        assert curator.state.queue[0] == "/path/to/pilot.mp3"
        assert curator.state.queue[1] == "/path/to/lawnmower.mp3"
        assert curator.state.curator_reason == "Marathon: Rick and Morty S1"


class TestCuratorStatus:
    def test_get_status(self):
        curator = _make_curator()
        curator.state.curator_enabled = True
        curator.state.curator_reason = "Marathon"
        curator._tracks_since_check = 3
        curator._next_check_at = 7
        status = curator.get_status()
        assert status["enabled"] is True
        assert status["reason"] == "Marathon"
        assert status["tracks_since_check"] == 3
        assert status["next_check_at"] == 7


class TestCuratorChat:
    def test_get_chat_history_initially_empty(self):
        curator = _make_curator()
        assert curator.get_chat_history() == []

    def test_chat_appends_to_history(self):
        curator = _make_curator()
        curator._chat_history.append({"role": "user", "content": "hello"})
        curator._chat_history.append({"role": "assistant", "content": "hi"})
        history = curator.get_chat_history()
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"

    @patch("streamer.curator.CURATOR_CHAT_MODEL", "ollama")
    @patch("streamer.curator.OLLAMA_URL", "http://localhost:11434")
    @patch("streamer.curator.urllib.request.urlopen")
    def test_chat_returns_response(self, mock_urlopen):
        response_body = json.dumps({
            "message": {"content": "I recommend Red Dwarf!"}
        }).encode()
        mock_response = MagicMock()
        mock_response.read.return_value = response_body
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        curator = _make_curator()
        result = curator.chat("What should I listen to?")
        assert result["response"] == "I recommend Red Dwarf!"
        assert result["queued"] == []
        assert len(curator.get_chat_history()) == 2

    @patch("streamer.curator.CURATOR_CHAT_MODEL", "ollama")
    @patch("streamer.curator.OLLAMA_URL", "http://localhost:11434")
    @patch("streamer.curator.urllib.request.urlopen")
    def test_chat_extracts_queue_action(self, mock_urlopen):
        response_text = (
            'Sure! Here you go.\n'
            '{"action": "queue", "tracks": ["Show/season 01/01"], "reason": "By request"}'
        )
        response_body = json.dumps({
            "message": {"content": response_text}
        }).encode()
        mock_response = MagicMock()
        mock_response.read.return_value = response_body
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        state = ServerState()
        scanner = MagicMock()
        scanner.roots = []
        curator = Curator(state, scanner)

        with patch.object(curator, "_resolve_tracks", return_value=["/path/01.mp3"]):
            with patch.object(curator, "_ensure_ollama_running", return_value=True):
                result = curator.chat("Play Show season 1")

        assert len(result["queued"]) == 1
        assert result["queued"][0] == "/path/01.mp3"
        assert state.queue == ["/path/01.mp3"]

    @patch("streamer.curator.CURATOR_CHAT_MODEL", "ollama")
    @patch("streamer.curator.OLLAMA_URL", "")
    def test_chat_fails_without_ollama_url(self):
        curator = _make_curator()
        result = curator.chat("hello")
        assert "queued" in result
        assert result["queued"] == []


class TestCuratorChatGemini:
    @patch("streamer.curator.GEMINI_API_KEY", "fake-key")
    @patch("streamer.curator.CURATOR_CHAT_MODEL", "gemini-2.5-flash")
    @patch("streamer.curator.genai")
    def test_chat_uses_gemini(self, mock_genai):
        mock_response = MagicMock()
        mock_response.text = "I recommend Family Guy!"
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response
        mock_genai.Client.return_value = mock_client

        curator = _make_curator()
        result = curator.chat("What has Republican Town?")

        assert result["response"] == "I recommend Family Guy!"
        assert result["queued"] == []
        mock_genai.Client.assert_called_once_with(api_key="fake-key")
        assert len(curator.get_chat_history()) == 2

    @patch("streamer.curator.GEMINI_API_KEY", "fake-key")
    @patch("streamer.curator.CURATOR_CHAT_MODEL", "gemini-2.5-flash")
    @patch("streamer.curator.genai")
    def test_gemini_chat_extracts_queue_action(self, mock_genai):
        response_text = (
            'Sure! Here you go.\n'
            '{"action": "queue", "tracks": ["Show/season 01/01"], "reason": "By request"}'
        )
        mock_response = MagicMock()
        mock_response.text = response_text
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response
        mock_genai.Client.return_value = mock_client

        state = ServerState()
        scanner = MagicMock()
        scanner.roots = []
        curator = Curator(state, scanner)

        with patch.object(curator, "_resolve_tracks", return_value=["/path/01.mp3"]):
            result = curator.chat("Play Show season 1")

        assert len(result["queued"]) == 1
        assert state.queue == ["/path/01.mp3"]

    @patch("streamer.curator.GEMINI_API_KEY", "fake-key")
    @patch("streamer.curator.CURATOR_CHAT_MODEL", "gemini-2.5-flash")
    @patch("streamer.curator.genai")
    def test_gemini_chat_handles_failure(self, mock_genai):
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = Exception("API error")
        mock_genai.Client.return_value = mock_client

        curator = _make_curator()
        result = curator.chat("hello")

        assert "went wrong" in result["response"]
        assert result["queued"] == []
        assert len(curator.get_chat_history()) == 0

    @patch("streamer.curator.GEMINI_API_KEY", "")
    @patch("streamer.curator.CURATOR_CHAT_MODEL", "gemini-2.5-flash")
    def test_gemini_chat_fails_without_api_key(self):
        curator = _make_curator()
        result = curator.chat("hello")
        assert "not configured" in result["response"].lower()
        assert result["queued"] == []

    @patch("streamer.curator.CURATOR_CHAT_MODEL", "ollama")
    @patch("streamer.curator.OLLAMA_URL", "http://localhost:11434")
    @patch("streamer.curator.urllib.request.urlopen")
    def test_ollama_fallback(self, mock_urlopen):
        response_body = json.dumps({
            "message": {"content": "Hello from Ollama!"}
        }).encode()
        mock_response = MagicMock()
        mock_response.read.return_value = response_body
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        curator = _make_curator()
        result = curator.chat("hello")
        assert result["response"] == "Hello from Ollama!"


class TestCheck:
    @patch("streamer.curator.build_catalog")
    @patch.object(Curator, "_ask_ollama")
    def test_skips_when_disabled(self, mock_ollama, mock_catalog):
        curator = _make_curator()
        curator.state.curator_enabled = False
        curator._check()
        mock_ollama.assert_not_called()
        mock_catalog.assert_not_called()

    @patch("streamer.curator.build_catalog")
    @patch.object(Curator, "_ask_ollama")
    def test_skips_when_queue_nonempty(self, mock_ollama, mock_catalog):
        curator = _make_curator()
        curator.state.curator_enabled = True
        curator.state.queue_add("something.mp3")
        curator._check()
        mock_ollama.assert_not_called()

    @patch("streamer.curator.build_catalog")
    @patch.object(Curator, "_ask_ollama")
    def test_calls_ollama_when_enabled(self, mock_ollama, mock_catalog):
        mock_catalog.return_value = ("catalog text", {})
        mock_ollama.return_value = {"action": "pass"}
        curator = _make_curator()
        curator.state.curator_enabled = True
        curator._check()
        mock_ollama.assert_called_once()

    @patch("streamer.curator.build_catalog")
    @patch.object(Curator, "_ask_ollama")
    def test_handles_ollama_failure(self, mock_ollama, mock_catalog):
        mock_catalog.return_value = ("catalog text", {})
        mock_ollama.return_value = None
        curator = _make_curator()
        curator.state.curator_enabled = True
        curator._check()
        assert curator.state.queue == []

    @patch("streamer.curator.build_catalog")
    @patch.object(Curator, "_ask_ollama")
    def test_handles_invalid_json(self, mock_ollama, mock_catalog):
        mock_catalog.return_value = ("catalog text", {})
        mock_ollama.return_value = "not a dict"
        curator = _make_curator()
        curator.state.curator_enabled = True
        curator._check()
        assert curator.state.queue == []


class TestAskOllama:
    @patch("streamer.curator.OLLAMA_URL", "")
    def test_returns_none_without_url(self):
        curator = _make_curator()
        result = curator._ask_ollama("catalog", "history")
        assert result is None

    @patch("streamer.curator.OLLAMA_URL", "http://localhost:11434")
    @patch("streamer.curator.urllib.request.urlopen")
    def test_returns_parsed_json(self, mock_urlopen):
        response_body = json.dumps({
            "message": {"content": json.dumps({"action": "pass"})}
        }).encode()
        mock_response = MagicMock()
        mock_response.read.return_value = response_body
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        curator = _make_curator()
        result = curator._ask_ollama("catalog", "history")
        assert result == {"action": "pass"}

    @patch("streamer.curator.shutil.which", return_value=None)
    @patch("streamer.curator.OLLAMA_URL", "http://localhost:11434")
    @patch("streamer.curator.urllib.request.urlopen")
    def test_returns_none_on_error(self, mock_urlopen, mock_which):
        mock_urlopen.side_effect = Exception("Connection refused")
        curator = _make_curator()
        result = curator._ask_ollama("catalog", "history")
        assert result is None


class TestEnsureOllamaRunning:
    @patch("streamer.curator.OLLAMA_URL", "http://localhost:11434")
    @patch("streamer.curator.urllib.request.urlopen")
    def test_returns_true_when_already_running(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        curator = _make_curator()
        assert curator._ensure_ollama_running() is True

    @patch("streamer.curator.OLLAMA_URL", "http://localhost:11434")
    @patch("streamer.curator.shutil.which", return_value=None)
    @patch("streamer.curator.urllib.request.urlopen")
    def test_returns_false_when_not_installed(self, mock_urlopen, mock_which):
        mock_urlopen.side_effect = Exception("Connection refused")
        curator = _make_curator()
        assert curator._ensure_ollama_running() is False

    @patch("streamer.curator.OLLAMA_URL", "http://localhost:11434")
    @patch("streamer.curator.subprocess.Popen")
    @patch("streamer.curator.shutil.which", return_value="/usr/bin/ollama")
    @patch("streamer.curator.urllib.request.urlopen")
    def test_starts_ollama_when_not_running(self, mock_urlopen, mock_which, mock_popen):
        call_count = [0]

        def urlopen_side_effect(req, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 1:
                raise Exception("Connection refused")
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        mock_urlopen.side_effect = urlopen_side_effect

        curator = _make_curator()
        with patch("streamer.curator.time.sleep"):
            result = curator._ensure_ollama_running()

        assert result is True
        mock_popen.assert_called_once()


import urllib.request

from streamer.scanner import Scanner


def _ollama_available():
    try:
        url = "http://localhost:11434/api/tags"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


@pytest.mark.skipif(not _ollama_available(), reason="Ollama not running")
class TestOllamaIntegration:
    def test_real_ollama_returns_valid_response(self, tmp_path):
        from streamer.catalog import build_catalog

        ent = tmp_path / "entertainment" / "Test Show" / "season 01"
        ent.mkdir(parents=True)
        (ent / "01.mp3").write_bytes(b"")
        (ent / "02.mp3").write_bytes(b"")
        (ent / "03.mp3").write_bytes(b"")

        pod = tmp_path / "podcasts" / "Test Podcast"
        pod.mkdir(parents=True)
        (pod / "ep01.mp3").write_bytes(b"")

        scanner = Scanner(roots=[
            tmp_path / "entertainment",
            tmp_path / "podcasts",
        ])
        catalog_text, path_lookup = build_catalog(scanner)
        history_text = "(nothing played yet)"

        curator = _make_curator(scanner=scanner)
        with patch("streamer.curator.OLLAMA_URL", "http://localhost:11434"):
            result = curator._ask_ollama(catalog_text, history_text)

        assert result is not None
        assert isinstance(result, dict)
        assert result.get("action") in ("pass", "queue")
        if result["action"] == "queue":
            assert isinstance(result.get("tracks"), list)
