import os
import subprocess
import tempfile
from pathlib import Path

import pytest

# Isolated data + media dirs for the whole session (set before zoom_editor imports).
_ROOT = Path(tempfile.mkdtemp(prefix="ze_test_"))
os.environ.setdefault("ZE_DATA_DIR", str(_ROOT / "data"))
os.environ.setdefault("ZE_MEDIA_ROOT", str(_ROOT / "media"))
os.environ.setdefault("ZE_RETENTION_DAYS", "0")
(_ROOT / "media").mkdir(parents=True, exist_ok=True)

VTT = """WEBVTT

1
00:00:00.500 --> 00:00:04.000
Paul Adams: Welcome everyone to the meeting. Today we talk about growth.

2
00:00:04.000 --> 00:00:09.000
William Coleman: Trust and community drive this industry. That is the whole point.

3
00:00:09.000 --> 00:00:14.000
Paul Adams: Simplicity is the hardest thing to build. We keep relearning it.

4
00:00:14.000 --> 00:00:19.500
William Coleman: Distributors want the sale done for them. So we make that easy.
"""


@pytest.fixture(scope="session")
def media() -> Path:
    """A 20 s 1280x720 test-pattern video with a tone, plus a matching VTT."""
    m = _ROOT / "media"
    video = m / "meeting.mp4"
    if not video.exists():
        ffmpeg = os.environ.get("ZE_FFMPEG", "ffmpeg")
        subprocess.run([ffmpeg, "-y", "-loglevel", "error",
                        "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=25:d=20",
                        "-f", "lavfi", "-i", "sine=f=330:d=20",
                        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                        "-shortest", str(video)], check=True)
        (m / "meeting.vtt").write_text(VTT, encoding="utf-8")
    return m


def probe(path: Path) -> dict:
    ffprobe = os.environ.get("ZE_FFPROBE", "ffprobe")
    out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height:format=duration",
                          "-of", "default=nw=1", str(path)],
                         capture_output=True, text=True, check=True).stdout
    d = dict(l.split("=", 1) for l in out.split())
    return {"w": int(d["width"]), "h": int(d["height"]), "dur": float(d["duration"])}
