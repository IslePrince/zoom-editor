"""ffmpeg plumbing: binary paths, encoder selection, a checked runner.

The encoder is chosen once per process. With ZE_ENCODER=auto (default) a
0.2 s test encode decides: h264_nvenc when the GPU encoder actually works
(driver, container capabilities and ffmpeg build all line up), otherwise
libx264. "Works" is tested, never inferred from nvidia-smi — a GPU that can
run CUDA is not proof that NVENC is exposed to the container.
"""

import os
import subprocess
import threading

FFMPEG = os.environ.get("ZE_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("ZE_FFPROBE", "ffprobe")

_lock = threading.Lock()
_encoder: str | None = None
_encoder_note = ""


def _nvenc_works() -> tuple[bool, str]:
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", "color=c=black:s=256x256:r=25:d=0.2",
           "-c:v", "h264_nvenc", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if r.returncode == 0:
        return True, ""
    lines = (r.stderr or "").strip().splitlines()
    # The cause ("Cannot load libnvidia-encode.so.1", "No capable devices found")
    # comes before ffmpeg's generic closing lines.
    cause = [l for l in lines if any(k in l.lower() for k in ("nvenc", "cuda", "cannot", "no capable", "driver"))]
    return False, (cause or lines or [f"exit {r.returncode}"])[0].strip()


def encoder() -> str:
    """'h264_nvenc' or 'libx264'."""
    global _encoder, _encoder_note
    with _lock:
        if _encoder is None:
            want = os.environ.get("ZE_ENCODER", "auto").lower()
            if want in ("x264", "libx264", "cpu"):
                _encoder, _encoder_note = "libx264", "forced by ZE_ENCODER"
            else:
                ok, why = _nvenc_works()
                if ok:
                    _encoder, _encoder_note = "h264_nvenc", "nvenc test encode passed"
                elif want in ("nvenc", "gpu"):
                    raise RuntimeError(f"ZE_ENCODER={want} but NVENC test encode failed: {why}")
                else:
                    _encoder, _encoder_note = "libx264", f"nvenc unavailable: {why}"
        return _encoder


def encoder_info() -> dict:
    enc = encoder()
    return {"encoder": enc, "note": _encoder_note}


def video_args(quality: int = 20) -> list:
    """Video codec args. `quality` is on the x264 CRF scale (lower = better);
    NVENC's constant-quality value is mapped to look about the same."""
    if encoder() == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq",
                "-rc", "vbr", "-cq", str(quality + 2), "-b:v", "0",
                "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "fast", "-crf", str(quality),
            "-pix_fmt", "yuv420p"]


AUDIO_ARGS = ["-c:a", "aac", "-b:a", "128k", "-ar", "48000"]


def run(cmd: list, timeout: int = 7200) -> subprocess.CompletedProcess:
    """Run ffmpeg/ffprobe; raise with the stderr tail on failure."""
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        tail = " | ".join((r.stderr or "").strip().splitlines()[-4:])
        raise RuntimeError(f"{os.path.basename(cmd[0])} failed: {tail}")
    return r


def version() -> str:
    try:
        out = subprocess.run([FFMPEG, "-version"], capture_output=True, text=True, timeout=10).stdout
        return out.splitlines()[0] if out else "unknown"
    except OSError:
        return "missing"
