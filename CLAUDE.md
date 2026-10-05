# zoom-editor — CLAUDE.md

A meeting recording plus its transcript in; clips, trailers, longform best-ofs and 1:1/9:16/16:9 social videos out. One Python service serves REST (`/api/v1`) and MCP (streamable HTTP, `/mcp`) on port 8093. See README.md for the API.

## Layout
- `zoom_editor/ff.py`: ffmpeg paths, encoder selection (a NVENC test encode decides; falls back to libx264), the checked runner.
- `zoom_editor/transcript.py`: VTT/SRT parsing, caption slicing, sentence reconstruction, hook-matched sentence picking, stitched VTT.
- `zoom_editor/social.py`: the social layout (palette, cropdetect, headline fitting, filter graph, caption cards). Ported verbatim from nokemo `scripts/render_social.py`; keep the two visually identical.
- `zoom_editor/compose.py`: cut, trailer, longform, all from one source file.
- `zoom_editor/store.py`: projects and jobs as JSON under `ZE_DATA_DIR`, plus a worker thread.
- `zoom_editor/server.py`: FastAPI routes and FastMCP tools. The MCP app is mounted at `/`, so it must stay the last route.

## Rules
- The service never picks highlights itself and never calls an LLM. The caller chooses the moments.
- Local file access goes only through `store.resolve_local` (confined to `ZE_MEDIA_ROOT`). Job files are served only by name from the job's own list.
- Every encode goes through `ff.video_args()`. Never hardcode `libx264` or `h264_nvenc`.
- `mcp` is pinned to `<2`: the 2.x server API changed.

## Deployment (Jeevan's setup)
Runs on the Windows desktop `jeevan-i9-rtx` (RTX 4090), Docker in WSL2, at `http://100.106.28.1:8093` over Tailscale, alongside video-editor (8090), image-editor (8091) and vector-editor (8092). The OpenClaw gateways on the XPS and nokemo1 connect to `/mcp` directly; no stdio adapter is needed.

## Tests
`pytest -q`. They generate a synthetic video and transcript, so no fixtures are needed. They need an ffmpeg with `drawtext`; some static builds lack it. `ZE_FFMPEG`/`ZE_FFPROBE` override the binaries.
