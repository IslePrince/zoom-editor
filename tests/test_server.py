import time

import pytest
from fastapi.testclient import TestClient

from zoom_editor.server import app


@pytest.fixture(scope="module")
def c():
    # One lifespan per process: the MCP session manager can only start once.
    with TestClient(app) as client:
        yield client


def _wait(c, jid, timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = c.get(f"/api/v1/jobs/{jid}").json()
        if j["status"] in ("done", "failed"):
            return j
        time.sleep(0.5)
    raise AssertionError("job timed out")


def test_rest_end_to_end(media, c):
    h = c.get("/api/v1/health").json()
    assert h["status"] == "ok" and h["encoder"] in ("h264_nvenc", "libx264")

    p = c.post("/api/v1/projects", json={"video_path": "meeting.mp4",
                                         "transcript_path": "meeting.vtt"}).json()
    assert p["has_transcript"] and abs(p["duration"] - 20) < 0.5

    assert c.post("/api/v1/projects", json={"video_path": "/etc/passwd"}).status_code == 403

    t = c.get(f"/api/v1/projects/{p['id']}/transcript", params={"start": 9, "end": 14}).json()
    assert t["total"] == 1 and t["cues"][0][2].startswith("Paul Adams: Simplicity")

    j = c.post("/api/v1/jobs", json={"project_id": p["id"], "type": "clip", "start": 4,
                                     "end": 8, "headline": "Trust", "sizes": ["9x16"]}).json()
    j = _wait(c, j["id"])
    assert j["status"] == "done", j.get("error")
    names = [f["name"] for f in j["files"]]
    assert names == ["clip.mp4", "clip_9x16.mp4"]
    r = c.get(f"/api/v1/jobs/{j['id']}/files/clip_9x16.mp4")
    assert r.status_code == 200 and len(r.content) > 10_000
    assert c.get(f"/api/v1/jobs/{j['id']}/files/..%2Fjob.json").status_code == 404

    j = c.post("/api/v1/jobs", json={"project_id": p["id"], "type": "trailer", "sizes": [],
                                     "moments": [{"start": 4, "end": 9, "label": "Trust"},
                                                 {"start": 14, "end": 19.5, "label": "Sales"}]}).json()
    j = _wait(c, j["id"])
    assert j["status"] == "done", j.get("error")
    assert {"trailer.mp4", "trailer.vtt", "trailer_timeline.json"} <= {f["name"] for f in j["files"]}

    # social: variants of the whole source, captions from 4 s, cycling headline
    j = c.post("/api/v1/jobs", json={"project_id": p["id"], "type": "social", "sizes": ["1x1"],
                                     "headline": "x", "caption_start": 4,
                                     "headline_timeline": [{"t0": 0, "t1": 10, "text": "First"},
                                                           {"t0": 10, "t1": 20, "text": "Second"}]}).json()
    j = _wait(c, j["id"])
    assert j["status"] == "done", j.get("error")
    assert [f["name"] for f in j["files"]] == ["social_1x1.mp4"]

    bad = c.post("/api/v1/jobs", json={"project_id": p["id"], "type": "clip", "start": 30, "end": 40})
    assert bad.status_code == 400


def test_mcp_tools(media, c):
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    init = c.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "1"}}})
    assert init.status_code == 200, init.text
    tools = c.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()
    names = {t["name"] for t in tools["result"]["tools"]}
    assert {"create_project", "get_transcript", "render_clip", "render_trailer",
            "render_longform", "get_job"} <= names
    res = c.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "health_check", "arguments": {}}}).json()
    assert not res["result"].get("isError"), res
