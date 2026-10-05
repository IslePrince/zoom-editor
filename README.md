# zoom-editor

Turn a meeting recording and its transcript into short, captioned videos ready for social media: a single **clip**, a **trailer** of about 30 seconds, or a **longform** best-of. Each comes as a plain cut and, optionally, as 1:1, 9:16 and 16:9 versions with burned-in captions, a waveform, spectrum bars and a headline.

It runs as one service on port 8093, serving an **MCP server** (streamable HTTP at `/mcp`) for AI agents and a **REST API** (`/api/v1`) for scripts. It encodes on the GPU with NVIDIA's encoder (`h264_nvenc`) when one is available and falls back to the CPU (`libx264`) otherwise.

Zoom is the motivating case because Zoom cloud recordings come with an `audio_transcript_*.vtt`. Any video with a WebVTT or SRT transcript works.

## How an agent uses it

1. `create_project`: point it at the video (URL, or a path under the media root) and the transcript.
2. `get_transcript`: read the cues and decide which moments are worth sharing. The service doesn't pick highlights itself; the calling agent (or person) does, so no LLM key is needed here.
3. Render one of three ways:
   - `render_clip`: one moment.
   - `render_trailer`: one *whole sentence* from each moment, the one that best matches its `hook`, played in recording order.
   - `render_longform`: every moment in full, with the headline changing for each.
   - `render_social`: no cut; the social sizes of the whole video, for a clip you already have. `caption_start` says where it sits on the transcript's timeline, and `headline_timeline` can change the headline over time.
4. `get_job` until the job's `status` is `done`, then download the file URLs.

Times can be given in seconds or as `HH:MM:SS.mmm`, measured on the source video's timeline. Captions never cut a sentence mid-way, and trailer snippets always start and end on sentence boundaries.

## Run it

```bash
docker compose up -d --build        # GPU passthrough requested; works without one too
curl http://localhost:8093/api/v1/health
```

`/api/v1/health` reports which encoder was chosen and why. The choice comes from a short test encode at startup, not from checking `nvidia-smi`. For NVENC inside Docker, the NVIDIA container runtime must pass the `video` capability, which the compose file requests.

Connect an MCP client to `http://<host>:8093/mcp` (streamable HTTP).

| Variable | Default | |
|---|---|---|
| `ZE_PORT` | 8093 | |
| `ZE_DATA_DIR` | `./data` | projects and job outputs |
| `ZE_MEDIA_ROOT` | (empty, off) | local files callers may reference; nothing outside it |
| `ZE_ENCODER` | `auto` | `auto`, `nvenc` (fail if unavailable) or `x264` |
| `ZE_API_TOKEN` | (empty) | if set, required as `Authorization: Bearer …` on REST and MCP |
| `ZE_PUBLIC_URL` | request host | base URL for file links in MCP results |
| `ZE_WORKERS` | 1 | concurrent jobs (one GPU encodes one job best) |
| `ZE_RETENTION_DAYS` | 14 | finished jobs are deleted after this; projects stay |
| `ZE_MAX_DOWNLOAD_GB` | 8 | cap on downloads from `video_url` |

## REST

```
GET    /api/v1/health
POST   /api/v1/projects                 {video_url|video_path, transcript_url|transcript_path|transcript_text, name}
POST   /api/v1/projects/upload          multipart: video, transcript?, name?
GET    /api/v1/projects[/{id}]          DELETE /api/v1/projects/{id}
GET    /api/v1/projects/{id}/transcript ?start=&end=
POST   /api/v1/jobs                     {project_id, type: clip|trailer|longform|social, moments:[{start,end,label,hook?}],
                                         sizes:["1x1","9x16","16x9"], subtitles, headline, accent, bg, quality,
                                         caption_start, headline_timeline:[{t0,t1,text}]}   (last two: social)
                                        (a clip may instead give start/end/headline at the top level)
GET    /api/v1/jobs[/{id}]              GET /api/v1/jobs/{id}/files/{name}
```

## Develop

```bash
pip install -e '.[test]'
pytest -q        # generates its own test video; needs an ffmpeg that has drawtext
```

The layout and transcript rules were ported from the nokemo.com Zoom-to-social pipeline (`render_social.py`, `render_compilations.py`) and behave the same.
