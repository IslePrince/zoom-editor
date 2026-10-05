"""Projects (a source video + optional transcript) and render jobs, kept as
JSON on disk under ZE_DATA_DIR so they survive a restart. One worker thread
by default: a single GPU encodes one job at a time best.
"""

import json
import os
import queue
import shutil
import threading
import time
import traceback
import urllib.request
import uuid
from pathlib import Path

from . import compose, social, transcript

DATA = Path(os.environ.get("ZE_DATA_DIR", "./data")).resolve()
MEDIA_ROOT = os.environ.get("ZE_MEDIA_ROOT", "")  # local paths allowed only under here
MAX_DOWNLOAD = int(float(os.environ.get("ZE_MAX_DOWNLOAD_GB", "8")) * 1e9)
RETENTION_DAYS = float(os.environ.get("ZE_RETENTION_DAYS", "14"))
WORKERS = int(os.environ.get("ZE_WORKERS", "1"))

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".m4v", ".webm"}
_lock = threading.RLock()


def _now() -> float:
    return round(time.time(), 3)


def _write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _safe_id(i: str) -> str:
    if not i or not all(c in "0123456789abcdef" for c in i) or len(i) != 12:
        raise KeyError(f"bad id: {i!r}")
    return i


# ── Inputs ──────────────────────────────────────────────────────────────
def resolve_local(path: str) -> Path:
    """A local file, allowed only inside ZE_MEDIA_ROOT."""
    if not MEDIA_ROOT:
        raise PermissionError("local paths are disabled (set ZE_MEDIA_ROOT)")
    root = Path(MEDIA_ROOT).resolve()
    p = (root / path).resolve() if not os.path.isabs(path) else Path(path).resolve()
    if p != root and root not in p.parents:
        raise PermissionError(f"{path} is outside ZE_MEDIA_ROOT")
    if not p.is_file():
        raise FileNotFoundError(path)
    return p


def download(url: str, dest: Path) -> Path:
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("only http(s) URLs are supported")
    req = urllib.request.Request(url, headers={"User-Agent": "zoom-editor/1"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = 0
        while chunk := r.read(1 << 20):
            total += len(chunk)
            if total > MAX_DOWNLOAD:
                raise ValueError(f"download exceeds ZE_MAX_DOWNLOAD_GB ({MAX_DOWNLOAD / 1e9:g} GB)")
            f.write(chunk)
    return dest


# ── Projects ────────────────────────────────────────────────────────────
def _pdir(pid: str) -> Path:
    return DATA / "projects" / _safe_id(pid)


def create_project(name: str = "", video_path: str = "", video_url: str = "",
                   video_upload: Path | None = None, video_filename: str = "",
                   transcript_path: str = "", transcript_url: str = "",
                   transcript_text: str = "") -> dict:
    pid = _new_id()
    d = _pdir(pid)
    d.mkdir(parents=True)
    try:
        if video_upload is not None:
            ext = Path(video_filename or "source.mp4").suffix.lower() or ".mp4"
            src = d / f"source{ext}"
            shutil.move(str(video_upload), src)
            origin = f"upload:{video_filename}"
        elif video_path:
            src = resolve_local(video_path)  # referenced in place, not copied
            origin = f"path:{src}"
        elif video_url:
            ext = Path(video_url.split("?")[0]).suffix.lower()
            src = download(video_url, d / f"source{ext if ext in VIDEO_EXT else '.mp4'}")
            origin = f"url:{video_url.split('?')[0]}"
        else:
            raise ValueError("give one of video_path, video_url or an upload")
        social.probe_duration(src)  # fail now, not mid-render, on a bad file

        tpath = d / "transcript.vtt"
        if transcript_text:
            tpath.write_text(transcript_text, encoding="utf-8")
        elif transcript_path:
            shutil.copyfile(resolve_local(transcript_path), tpath)
        elif transcript_url:
            download(transcript_url, tpath)
        cues = transcript.parse_vtt(tpath) if tpath.is_file() else []
        if tpath.is_file() and not cues:
            raise ValueError("transcript has no cues (expected WebVTT or SRT)")

        meta = {"id": pid, "name": name or Path(origin.split(":", 1)[1]).stem,
                "source": str(src), "origin": origin,
                "duration": round(social.probe_duration(src), 3),
                "has_transcript": bool(cues), "cues": len(cues), "created": _now()}
        _write_json(d / "project.json", meta)
        return meta
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise


def get_project(pid: str) -> dict:
    f = _pdir(pid) / "project.json"
    if not f.is_file():
        raise KeyError(f"no project {pid}")
    return json.loads(f.read_text(encoding="utf-8"))


def list_projects() -> list:
    root = DATA / "projects"
    out = []
    for f in sorted(root.glob("*/project.json")) if root.is_dir() else []:
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda p: p.get("created", 0), reverse=True)


def delete_project(pid: str) -> None:
    get_project(pid)
    shutil.rmtree(_pdir(pid))


def project_cues(pid: str) -> list:
    t = _pdir(pid) / "transcript.vtt"
    return transcript.parse_vtt(t) if t.is_file() else []


# ── Jobs ────────────────────────────────────────────────────────────────
JOB_TYPES = ("clip", "trailer", "longform")
_q: "queue.Queue[str]" = queue.Queue()


def _jdir(jid: str) -> Path:
    return DATA / "jobs" / _safe_id(jid)


def get_job(jid: str) -> dict:
    f = _jdir(jid) / "job.json"
    if not f.is_file():
        raise KeyError(f"no job {jid}")
    return json.loads(f.read_text(encoding="utf-8"))


def _save_job(job: dict) -> None:
    with _lock:
        _write_json(_jdir(job["id"]) / "job.json", job)


def list_jobs(project_id: str = "", limit: int = 50) -> list:
    root = DATA / "jobs"
    jobs = []
    for f in root.glob("*/job.json") if root.is_dir() else []:
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not project_id or j.get("project_id") == project_id:
            jobs.append(j)
    return sorted(jobs, key=lambda j: j.get("created", 0), reverse=True)[:limit]


def submit(project_id: str, type: str, moments: list, sizes=("1x1", "9x16", "16x9"),
           subtitles: bool = True, headline: str = "", accent: str = "", bg: str = "",
           quality: int = 20) -> dict:
    proj = get_project(project_id)
    if type not in JOB_TYPES:
        raise ValueError(f"type must be one of {JOB_TYPES}")
    norm = compose.normalize_moments(moments)  # validates
    if not norm:
        raise ValueError("at least one moment is required")
    if type == "clip" and len(norm) != 1:
        raise ValueError("a clip job takes exactly one moment")
    dur = proj["duration"]
    for m in norm:
        if m["start"] >= dur:
            raise ValueError(f"moment at {m['start']:.1f}s starts after the end of the video ({dur:.1f}s)")
    sizes = list(sizes or [])
    for s in sizes:
        if s not in social.SIZES:
            raise ValueError(f"unknown size {s!r}; options: {list(social.SIZES)}")
    if subtitles and not proj["has_transcript"]:
        subtitles = False
    if (accent or bg) and not (accent and bg):
        raise ValueError("give both accent and bg, or neither")
    jid = _new_id()
    _jdir(jid).mkdir(parents=True)
    job = {"id": jid, "project_id": project_id, "type": type, "status": "queued",
           "progress": 0.0, "message": "queued", "created": _now(),
           "params": {"moments": moments, "sizes": sizes, "subtitles": subtitles,
                      "headline": headline, "accent": accent, "bg": bg, "quality": int(quality)},
           "files": [], "error": None}
    _save_job(job)
    _q.put(jid)
    return job


def _run(job: dict) -> None:
    proj = get_project(job["project_id"])
    p = job["params"]
    src, out = Path(proj["source"]), _jdir(job["id"])
    cues = project_cues(proj["id"])
    moments = compose.normalize_moments(p["moments"])
    q = p["quality"]
    stages = 1 + (1 if p["sizes"] else 0)

    def prog(stage):
        def f(frac, msg):
            job["progress"] = round((stage + frac) / stages, 3)
            job["message"] = msg
            _save_job(job)
        return f

    timeline = None
    if job["type"] == "clip":
        m = moments[0]
        prog(0)(0.0, "cutting clip")
        raw = compose.cut(src, m["start"], m["end"], out / "clip.mp4", q)
        headline = p["headline"] or m["label"]
        caption_start = m["start"]
        files = [raw]
    else:
        build = compose.build_trailer if job["type"] == "trailer" else compose.build_longform
        res = build(src, cues, moments, out, q, on_progress=prog(0))
        raw, timeline = res["mp4"], [(t["t0"], t["t1"], t["text"]) for t in res["timeline"]]
        headline = p["headline"] or (moments[0]["label"] if moments else "")
        caption_start = 0.0
        cues = transcript.parse_vtt(res["vtt"])  # captions follow the stitched timeline
        files = [raw, res["vtt"]]
        (out / f"{job['type']}_timeline.json").write_text(json.dumps(res["timeline"], indent=2))
        files.append(out / f"{job['type']}_timeline.json")
        job["segments"] = res["segments"]

    if p["sizes"]:
        if not (headline or timeline):
            raise ValueError("social sizes need a headline (or moment labels)")
        r = social.render_variants(raw, out, raw.stem, headline or " ", p["sizes"],
                                   cues=cues if p["subtitles"] else None,
                                   clip_start=caption_start,
                                   headline_timeline=timeline if timeline and any(t[2] for t in timeline) else None,
                                   accent=p["accent"] or None, bg=p["bg"] or None,
                                   quality=q, on_progress=prog(1))
        files += list(r["files"].values())
        job["palette"], job["crop"] = r["palette"], r["crop"]
    job["files"] = [{"name": f.name, "bytes": f.stat().st_size} for f in files]


def _worker() -> None:
    while True:
        jid = _q.get()
        try:
            job = get_job(jid)
        except KeyError:
            continue
        if job["status"] not in ("queued",):
            continue
        job.update(status="running", started=_now(), message="starting")
        _save_job(job)
        t0 = time.time()
        try:
            _run(job)
            job.update(status="done", progress=1.0, message="done")
        except Exception as e:
            job.update(status="failed", error=f"{type(e).__name__}: {e}",
                       trace=traceback.format_exc()[-2000:], message="failed")
        job["finished"] = _now()
        job["seconds"] = round(time.time() - t0, 1)
        _save_job(job)
        cleanup()


def cleanup() -> int:
    """Delete finished jobs older than ZE_RETENTION_DAYS. Projects stay."""
    if RETENTION_DAYS <= 0:
        return 0
    cutoff, n = time.time() - RETENTION_DAYS * 86400, 0
    for j in list_jobs(limit=10_000):
        if j["status"] in ("done", "failed") and (j.get("finished") or 0) < cutoff:
            shutil.rmtree(_jdir(j["id"]), ignore_errors=True)
            n += 1
    return n


_started = False


def start_workers() -> None:
    """Start workers; requeue jobs a restart interrupted."""
    global _started
    if _started:
        return
    _started = True
    (DATA / "projects").mkdir(parents=True, exist_ok=True)
    (DATA / "jobs").mkdir(parents=True, exist_ok=True)
    for j in sorted(list_jobs(limit=10_000), key=lambda j: j["created"]):
        if j["status"] in ("queued", "running"):
            j.update(status="queued", message="requeued after restart", progress=0.0)
            _save_job(j)
            _q.put(j["id"])
    for _ in range(max(1, WORKERS)):
        threading.Thread(target=_worker, daemon=True).start()


def job_file(jid: str, name: str) -> Path:
    if "/" in name or "\\" in name or name.startswith("."):
        raise KeyError(name)
    job = get_job(jid)
    if name not in {f["name"] for f in job["files"]}:
        raise KeyError(name)
    return _jdir(jid) / name
