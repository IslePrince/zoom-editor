"""Cuts and compilations from one source recording.

A *moment* is {"start", "end", "label"?, "hook"?}: times in seconds or
HH:MM:SS.mmm on the source timeline. `label` is the headline shown while the
moment plays; `hook` is the line the trailer tries to land on (defaults to
the label).

- clip:     one moment, precisely re-encoded.
- trailer:  ONE whole sentence per moment (the one that best matches its
            hook), in recording order. Never cuts mid-sentence.
- longform: every moment in full, in recording order.

Compilations also return a stitched VTT and a headline timeline so the
social renderer can caption them and cycle each moment's label.
"""

import os
import tempfile
from pathlib import Path

from .ff import AUDIO_ARGS, FFMPEG, run, video_args
from .transcript import pick_best_sentence, stitched_vtt, ts_to_seconds


def normalize_moments(moments: list) -> list:
    out = []
    for i, m in enumerate(moments):
        s, e = ts_to_seconds(m["start"]), ts_to_seconds(m["end"])
        if e <= s:
            raise ValueError(f"moment {i}: end ({e:.3f}s) is not after start ({s:.3f}s)")
        label = (m.get("label") or m.get("headline") or "").strip()
        out.append({"start": s, "end": e, "label": label,
                    "hook": (m.get("hook") or label).strip()})
    return sorted(out, key=lambda m: m["start"])


def cut(src: Path, start: float, end: float, out: Path, quality: int = 20) -> Path:
    """Frame-accurate re-encoded cut. Uniform codec params, so cuts can be
    concatenated with stream copy afterwards."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".partial" + out.suffix)
    run([FFMPEG, "-y", "-loglevel", "error",
         "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{end - start:.3f}",
         *video_args(quality), *AUDIO_ARGS,
         "-movflags", "+faststart", "-avoid_negative_ts", "make_zero", str(tmp)])
    if not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg produced empty output")
    tmp.replace(out)
    return out


def concat(files: list, out: Path) -> Path:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as lst:
        for f in files:
            lst.write(f"file '{Path(f).as_posix()}'\n")
        lst_path = lst.name
    try:
        run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", lst_path, "-c", "copy", "-movflags", "+faststart", str(out)])
    finally:
        os.unlink(lst_path)
    return out


def _compile(src: Path, cues: list, windows: list, out_dir: Path, name: str,
             quality: int, on_progress=None) -> dict:
    """windows: [(abs_start, abs_end, label)] in output order."""
    out_dir.mkdir(parents=True, exist_ok=True)
    segments, timeline, files = [], [], []
    offset = 0.0
    with tempfile.TemporaryDirectory(prefix=f"ze_{name}_") as tmp:
        for i, (s, e, label) in enumerate(windows):
            if on_progress:
                on_progress(i / max(1, len(windows)), f"cutting {i + 1}/{len(windows)}")
            f = Path(tmp) / f"seg_{i:03d}.mp4"
            cut(src, s, e, f, quality)
            files.append(f)
            segments.append((s, e, offset))
            timeline.append({"t0": round(offset, 3), "t1": round(offset + (e - s), 3), "text": label})
            offset += e - s
        mp4 = concat(files, out_dir / f"{name}.mp4")
    vtt = out_dir / f"{name}.vtt"
    vtt.write_text(stitched_vtt(cues, segments), encoding="utf-8")
    return {"mp4": mp4, "vtt": vtt, "timeline": timeline, "duration": offset,
            "segments": [{"source_start": s, "source_end": e, "at": o} for s, e, o in segments]}


def build_trailer(src: Path, cues: list, moments: list, out_dir: Path,
                  quality: int = 20, on_progress=None) -> dict:
    windows = []
    for m in normalize_moments(moments):
        s, e = pick_best_sentence(cues, m["start"], m["end"], m["hook"])
        windows.append((s, e, m["label"]))
    return _compile(src, cues, windows, out_dir, "trailer", quality, on_progress)


def build_longform(src: Path, cues: list, moments: list, out_dir: Path,
                   quality: int = 20, on_progress=None) -> dict:
    windows = [(m["start"], m["end"], m["label"]) for m in normalize_moments(moments)]
    return _compile(src, cues, windows, out_dir, "longform", quality, on_progress)
