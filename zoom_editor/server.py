"""HTTP server: REST API under /api/v1 and an MCP endpoint (streamable HTTP)
at /mcp, on one port. Optional bearer token (ZE_API_TOKEN) guards both.
"""

import contextlib
import os
import shutil
import tempfile
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from mcp.server.fastmcp import FastMCP

from . import __version__, ff, social, store

TOKEN = os.environ.get("ZE_API_TOKEN", "")
PUBLIC_URL = os.environ.get("ZE_PUBLIC_URL", "").rstrip("/")


def _file_urls(job: dict, base: str = "") -> dict:
    base = (base or PUBLIC_URL).rstrip("/")
    for f in job.get("files", []):
        f["url"] = f"{base}/api/v1/jobs/{job['id']}/files/{f['name']}"
    job.pop("trace", None)
    return job


def health() -> dict:
    return {"status": "ok", "service": "zoom-editor", "version": __version__,
            "ffmpeg": ff.version(), **ff.encoder_info(),
            "sizes": list(social.SIZES), "local_paths": bool(store.MEDIA_ROOT)}


# ── MCP ─────────────────────────────────────────────────────────────────
mcp = FastMCP(
    "zoom-editor", host="0.0.0.0", stateless_http=True, json_response=True,
    instructions=(
        "Turn a meeting recording (Zoom or any video) plus its transcript into a clip, a "
        "~30 s trailer or a longform best-of, each optionally as 1:1 / 9:16 / 16:9 social "
        "videos with burned-in captions, waveform and headline. Flow: create_project → "
        "get_transcript (read it and choose the moments yourself) → render_clip / "
        "render_trailer / render_longform → get_job until status is done → download the "
        "file URLs. Times are seconds or HH:MM:SS.mmm on the source timeline. Renders are "
        "async and can take minutes."))


@mcp.tool()
def health_check() -> dict:
    """Service status, ffmpeg version and which encoder (GPU h264_nvenc or CPU libx264) is in use."""
    return health()


@mcp.tool()
def create_project(video_url: str = "", video_path: str = "", transcript_url: str = "",
                   transcript_path: str = "", transcript_text: str = "", name: str = "") -> dict:
    """Register a source video (+ optional WebVTT/SRT transcript, e.g. Zoom's audio_transcript .vtt).
    video_url: http(s) URL to download. video_path: a file under the server's media root.
    Transcript via URL, path or pasted text. Returns the project with its duration."""
    return store.create_project(name=name, video_path=video_path, video_url=video_url,
                                transcript_path=transcript_path, transcript_url=transcript_url,
                                transcript_text=transcript_text)


@mcp.tool()
def list_projects() -> list:
    """All projects, newest first."""
    return store.list_projects()


@mcp.tool()
def get_transcript(project_id: str, start: str = "", end: str = "", max_cues: int = 2000) -> dict:
    """Transcript cues [start_s, end_s, text], optionally limited to a window. Read this to pick moments."""
    from .transcript import ts_to_seconds
    cues = store.project_cues(project_id)
    s = ts_to_seconds(start) if start else 0.0
    e = ts_to_seconds(end) if end else float("inf")
    sel = [[round(a, 2), round(b, 2), t] for a, b, t in cues if b > s and a < e]
    return {"project_id": project_id, "total": len(sel), "cues": sel[:max_cues],
            "truncated": len(sel) > max_cues}


@mcp.tool()
def render_clip(project_id: str, start: str, end: str, headline: str = "",
                sizes: list[str] | None = None, subtitles: bool = True,
                accent: str = "", bg: str = "") -> dict:
    """Cut one moment. With sizes (any of 1x1, 9x16, 16x9; default all three) also render
    social variants with `headline`. Pass sizes=[] for the plain cut only. accent/bg: hex
    colours to override the auto palette (both or neither). Returns a job; poll get_job."""
    return _file_urls(store.submit(project_id, "clip",
                                   [{"start": start, "end": end, "label": headline}],
                                   sizes=social.SIZES.keys() if sizes is None else sizes,
                                   subtitles=subtitles, headline=headline, accent=accent, bg=bg))


@mcp.tool()
def render_trailer(project_id: str, moments: list[dict], sizes: list[str] | None = None,
                   subtitles: bool = True, accent: str = "", bg: str = "") -> dict:
    """~30 s teaser: ONE whole sentence from each moment (the one best matching its `hook`),
    in recording order. moments: [{start, end, label, hook?}] — label is shown while it plays."""
    return _file_urls(store.submit(project_id, "trailer", moments,
                                   sizes=social.SIZES.keys() if sizes is None else sizes,
                                   subtitles=subtitles, accent=accent, bg=bg))


@mcp.tool()
def render_longform(project_id: str, moments: list[dict], sizes: list[str] | None = None,
                    subtitles: bool = True, accent: str = "", bg: str = "") -> dict:
    """Best-of: every moment in full, in recording order, headline cycling per moment.
    moments: [{start, end, label}]."""
    return _file_urls(store.submit(project_id, "longform", moments,
                                   sizes=social.SIZES.keys() if sizes is None else sizes,
                                   subtitles=subtitles, accent=accent, bg=bg))


@mcp.tool()
def get_job(job_id: str) -> dict:
    """Job status (queued/running/done/failed), progress 0–1, and output file URLs when done."""
    return _file_urls(store.get_job(job_id))


@mcp.tool()
def list_jobs(project_id: str = "", limit: int = 20) -> list:
    """Recent jobs, optionally for one project."""
    return [_file_urls(j) for j in store.list_jobs(project_id, limit)]


# ── REST ────────────────────────────────────────────────────────────────
@contextlib.asynccontextmanager
async def lifespan(_app):
    store.start_workers()
    ff.encoder()  # decide GPU vs CPU at startup, logged in /health
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="zoom-editor", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def auth(request: Request, call_next):
    if TOKEN and request.url.path not in ("/api/v1/health",):
        if request.headers.get("authorization", "") != f"Bearer {TOKEN}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


def _base(request: Request) -> str:
    return PUBLIC_URL or str(request.base_url).rstrip("/")


def _err(e: Exception):
    if isinstance(e, KeyError):
        raise HTTPException(404, str(e).strip("'"))
    if isinstance(e, PermissionError):
        raise HTTPException(403, str(e))
    if isinstance(e, (ValueError, FileNotFoundError, RuntimeError)):
        raise HTTPException(400, str(e))
    raise e


@app.get("/api/v1/health")
def rest_health():
    return health()


@app.post("/api/v1/projects")
def rest_create_project(body: dict = Body(...)):
    try:
        return store.create_project(**{k: body.get(k, "") for k in (
            "name", "video_path", "video_url", "transcript_path", "transcript_url", "transcript_text")})
    except Exception as e:
        _err(e)


@app.post("/api/v1/projects/upload")
def rest_upload_project(video: UploadFile = File(...), transcript: UploadFile | None = File(None),
                        name: str = Form("")):
    tmpdir = store.DATA / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=tmpdir, suffix=Path(video.filename or "").suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            shutil.copyfileobj(video.file, f, 1 << 20)
        text = transcript.file.read().decode("utf-8", "replace") if transcript else ""
        return store.create_project(name=name, video_upload=Path(tmp),
                                    video_filename=video.filename or "", transcript_text=text)
    except Exception as e:
        _err(e)
    finally:
        Path(tmp).unlink(missing_ok=True)


@app.get("/api/v1/projects")
def rest_list_projects():
    return store.list_projects()


@app.get("/api/v1/projects/{pid}")
def rest_get_project(pid: str):
    try:
        return store.get_project(pid)
    except Exception as e:
        _err(e)


@app.delete("/api/v1/projects/{pid}")
def rest_delete_project(pid: str):
    try:
        store.delete_project(pid)
        return {"deleted": pid}
    except Exception as e:
        _err(e)


@app.get("/api/v1/projects/{pid}/transcript")
def rest_transcript(pid: str, start: str = "", end: str = ""):
    try:
        store.get_project(pid)
        return get_transcript(pid, start, end, max_cues=100_000)
    except Exception as e:
        _err(e)


@app.post("/api/v1/jobs")
def rest_submit(request: Request, body: dict = Body(...)):
    try:
        moments = body.get("moments")
        if moments is None and "start" in body:
            moments = [{"start": body["start"], "end": body.get("end"), "label": body.get("headline", "")}]
        job = store.submit(body.get("project_id", ""), body.get("type", ""), moments or [],
                           sizes=body.get("sizes", list(social.SIZES)),
                           subtitles=bool(body.get("subtitles", True)),
                           headline=body.get("headline", ""), accent=body.get("accent", ""),
                           bg=body.get("bg", ""), quality=int(body.get("quality", 20)))
        return _file_urls(job, _base(request))
    except Exception as e:
        _err(e)


@app.get("/api/v1/jobs")
def rest_list_jobs(request: Request, project_id: str = "", limit: int = 50):
    return [_file_urls(j, _base(request)) for j in store.list_jobs(project_id, limit)]


@app.get("/api/v1/jobs/{jid}")
def rest_get_job(request: Request, jid: str):
    try:
        return _file_urls(store.get_job(jid), _base(request))
    except Exception as e:
        _err(e)


@app.get("/api/v1/jobs/{jid}/files/{name}")
def rest_job_file(jid: str, name: str):
    try:
        return FileResponse(store.job_file(jid, name), filename=name)
    except Exception as e:
        _err(e)


app.mount("/", mcp.streamable_http_app())


def main():
    import uvicorn
    uvicorn.run(app, host=os.environ.get("ZE_HOST", "0.0.0.0"),
                port=int(os.environ.get("ZE_PORT", "8093")), log_level="info")


if __name__ == "__main__":
    main()
