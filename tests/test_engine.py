from conftest import VTT, probe

from zoom_editor import compose, social, transcript


def test_parse_and_srt():
    cues = transcript.parse_text(VTT)
    assert len(cues) == 4 and cues[1][0] == 4.0
    srt = "1\n00:00:01,000 --> 00:00:02,500\nHello there.\n"
    assert transcript.parse_text(srt) == [(1.0, 2.5, "Hello there.")]


def test_slice_cues_rebases_and_splits_sentences():
    cues = transcript.parse_text(VTT)
    sl = transcript.slice_cues(cues, 4.0, 9.0)
    assert sl[0][0] == 0.0
    assert [t for _, _, t in sl] == ["William Coleman: Trust and community drive this industry.",
                                     "That is the whole point."]


def test_pick_best_sentence_matches_hook_and_keeps_whole_sentence():
    cues = transcript.parse_text(VTT)
    s, e = transcript.pick_best_sentence(cues, 9.0, 19.5, "simplicity hardest build")
    assert 9.0 <= s < 10.0 and 11.0 < e <= 14.5   # the 'Simplicity…' sentence, not the next cue


def test_normalize_moments_validates():
    ms = compose.normalize_moments([{"start": "00:00:09", "end": 14, "label": "B"},
                                    {"start": 4, "end": 9, "label": "A"}])
    assert [m["label"] for m in ms] == ["A", "B"]
    try:
        compose.normalize_moments([{"start": 5, "end": 5}])
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_cut_trailer_longform(media, tmp_path):
    src = media / "meeting.mp4"
    cues = transcript.parse_vtt(media / "meeting.vtt")
    clip = compose.cut(src, 4.0, 9.0, tmp_path / "c.mp4")
    assert abs(probe(clip)["dur"] - 5.0) < 0.3

    moments = [{"start": 4, "end": 9, "label": "Trust"}, {"start": 9, "end": 14, "label": "Simplicity"}]
    lf = compose.build_longform(src, cues, moments, tmp_path)
    assert abs(probe(lf["mp4"])["dur"] - 10.0) < 0.5
    assert [t["text"] for t in lf["timeline"]] == ["Trust", "Simplicity"]
    assert "Simplicity is the hardest" in lf["vtt"].read_text()

    tr = compose.build_trailer(src, cues, moments, tmp_path)
    assert 2.0 < tr["duration"] < 10.0
    assert tr["vtt"].read_text().startswith("WEBVTT")


def test_social_variants(media, tmp_path):
    src = compose.cut(media / "meeting.mp4", 4.0, 8.0, tmp_path / "c.mp4")
    cues = transcript.parse_vtt(media / "meeting.vtt")
    r = social.render_variants(src, tmp_path, "c", "Trust and community", cues=cues,
                               clip_start=4.0)
    dims = {k: (probe(v)["w"], probe(v)["h"]) for k, v in r["files"].items()}
    assert dims == {"1x1": (1080, 1080), "9x16": (1080, 1920), "16x9": (1920, 1080)}
    assert r["captions"] >= 1
