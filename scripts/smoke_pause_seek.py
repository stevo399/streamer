import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen


def _wait_for_http(url: str, timeout: float = 20.0) -> None:
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urlopen(url, timeout=2):
                return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for {url}")


def _post_json(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(url: str) -> dict:
    req = Request(url, data=b"", method="POST")
    with urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str) -> dict:
    with urlopen(url, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    tmp = Path(__file__).resolve().parent.parent / "tmp_media_smoke"
    if tmp.exists():
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    try:
        media_dir = tmp / "entertainment" / "Smoke Show" / "season 01"
        media_dir.mkdir(parents=True, exist_ok=True)

        track1 = media_dir / "01.mp3"
        track2 = media_dir / "02.mp3"

        ffmpeg_cmd1 = [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
            "-acodec", "libmp3lame", "-b:a", "128k", str(track1),
        ]
        ffmpeg_cmd2 = [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=550:duration=30",
            "-acodec", "libmp3lame", "-b:a", "128k", str(track2),
        ]
        subprocess.run(ffmpeg_cmd1, check=True, capture_output=True)
        subprocess.run(ffmpeg_cmd2, check=True, capture_output=True)

        env = os.environ.copy()
        env["MEDIA_ROOTS"] = str(tmp / "entertainment")
        env["HOST"] = "127.0.0.1"
        env["PORT"] = "8065"

        proc = subprocess.Popen(
            ["uv", "run", "streamer"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )

        try:
            _wait_for_http("http://127.0.0.1:8065/api/state")

            state1 = _get_json("http://127.0.0.1:8065/api/state")
            assert "paused" in state1
            assert state1["paused"] is False

            seek_res = _post_json("http://127.0.0.1:8065/api/tracks/seek", {"position": 5.0})
            assert seek_res["ok"] is True

            pause_res = _post("http://127.0.0.1:8065/api/playback/pause")
            assert pause_res["ok"] is True
            time.sleep(0.5)
            state2 = _get_json("http://127.0.0.1:8065/api/state")
            assert state2["paused"] is True

            resume_res = _post("http://127.0.0.1:8065/api/playback/resume")
            assert resume_res["ok"] is True
            time.sleep(0.5)
            state3 = _get_json("http://127.0.0.1:8065/api/state")
            assert state3["paused"] is False

            print("smoke_pause_seek: PASS")
            return 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=8)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, RuntimeError, URLError, HTTPError) as exc:
        print(f"smoke_pause_seek: FAIL: {exc}")
        raise SystemExit(1)
