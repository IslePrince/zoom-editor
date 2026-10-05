"""Transcript handling: parse VTT/SRT, slice captions for a window, rebuild
whole sentences, pick the sentence that best matches a hook, and stitch a
new VTT for a compilation.

Ported from nokemo scripts/render_social.py + render_compilations.py; the
behaviour (sentence rules, scoring, timing) is kept identical on purpose.
"""

import re
from pathlib import Path

_STOPWORDS = set(
    "the a an and or but of to in on for at by is are was were be been being "
    "it this that these those i we you they he she them our your their his her "
    "its as with from not no so if then than too very can will just about into "
    "out up down over under again more most some such only own same other "
    "were will with you your like just so up out".split()
)


def ts_to_seconds(ts) -> float:
    """'HH:MM:SS.mmm', 'MM:SS.mmm', '12.5' or a number → seconds."""
    if isinstance(ts, (int, float)):
        return float(ts)
    parts = str(ts).strip().replace(",", ".").split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(parts[0])


def seconds_to_vtt_ts(s: float) -> str:
    hrs = int(s // 3600)
    mins = int((s % 3600) // 60)
    secs = s - (hrs * 3600 + mins * 60)
    return f"{hrs:02d}:{mins:02d}:{secs:06.3f}"


def parse_text(raw: str) -> list:
    """(start_s, end_s, text) for every cue. Accepts WebVTT and SRT."""
    lines = raw.splitlines()
    cues = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if "-->" in line:
            head, _, tail = line.partition("-->")
            try:
                start = ts_to_seconds(head)
                end = ts_to_seconds(tail.strip().split(" ", 1)[0])
            except (ValueError, IndexError):
                i += 1
                continue
            i += 1
            text_parts = []
            while i < len(lines) and lines[i].strip():
                text_parts.append(lines[i].strip())
                i += 1
            text = " ".join(text_parts).strip()
            text = re.sub(r"<[^>]+>", "", text)  # strip inline tags
            if text:
                cues.append((start, end, text))
        else:
            i += 1
    return cues


def parse_vtt(path: Path) -> list:
    path = Path(path)
    if not path.is_file():
        return []
    return parse_text(path.read_text(encoding="utf-8", errors="replace"))


_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+|(?<=[.!?…])$")


def split_sentences(text: str) -> list:
    text = text.strip()
    if not text:
        return []
    return [p.strip() for p in _SENT_SPLIT.split(text) if p and p.strip()]


def _split_long_cue(start: float, end: float, text: str) -> list:
    """One display chunk per sentence; time allocated by word count."""
    chunks = [s.split() for s in split_sentences(text) if s.strip()]
    chunks = [c for c in chunks if c]
    if not chunks:
        return []
    total_words = sum(len(c) for c in chunks)
    dur = max(end - start, 0.01)
    out, t = [], start
    for i, c in enumerate(chunks):
        new_t = end if i == len(chunks) - 1 else t + dur * (len(c) / total_words)
        out.append((t, new_t, " ".join(c)))
        t = new_t
    return out


def slice_cues(cues: list, clip_start: float, clip_end: float,
               min_dur: float = 0.6) -> list:
    """Cues overlapping the window, split per sentence, rebased to 0,
    with a minimum display time and no overlap."""
    raw = []
    for cs, ce, text in cues:
        if ce <= clip_start or cs >= clip_end:
            continue
        for ss, se, chunk_text in _split_long_cue(cs, ce, text):
            ns = max(0.0, ss - clip_start)
            ne = min(clip_end - clip_start, se - clip_start)
            if ne - ns < min_dur:
                ne = ns + min_dur
            if ne > ns:
                raw.append([ns, ne, chunk_text])
    for i in range(len(raw) - 1):
        if raw[i][1] > raw[i + 1][0]:
            raw[i][1] = max(raw[i][0] + 0.05, raw[i + 1][0] - 0.02)
    return [(s, e, t) for s, e, t in raw]


def _tokens(text: str) -> set:
    return {t.lower() for t in re.findall(r"[A-Za-z][A-Za-z'-]*", text or "")
            if len(t) > 2 and t.lower() not in _STOPWORDS}


_TERMINAL_PUNCT = tuple(".!?…")


def cues_to_sentences(cues: list, window_start: float, window_end: float) -> list:
    """Whole sentences intersecting the window; per-word timing interpolated
    inside each cue (transcripts give cue-level timing only)."""
    words = []
    for cs, ce, text in cues:
        if ce <= window_start or cs >= window_end:
            continue
        toks = text.split()
        if not toks:
            continue
        per = (ce - cs) / len(toks)
        for i, w in enumerate(toks):
            words.append((w, cs + i * per, cs + (i + 1) * per))
    if not words:
        return []
    sentences, start_i = [], 0
    for i, (w, _ws, _we) in enumerate(words):
        stripped = w.rstrip('"\')]}')
        if stripped and stripped[-1] in _TERMINAL_PUNCT:
            chunk = words[start_i:i + 1]
            sentences.append((chunk[0][1], chunk[-1][2], " ".join(x[0] for x in chunk)))
            start_i = i + 1
    if start_i < len(words):
        chunk = words[start_i:]
        sentences.append((chunk[0][1], chunk[-1][2], " ".join(x[0] for x in chunk)))
    out = []
    for ss, se, text in sentences:
        if se <= window_start or ss >= window_end:
            continue
        out.append((max(ss, window_start), min(se, window_end), text))
    return out


def pick_best_sentence(cues: list, window_start: float, window_end: float,
                       hook_text: str, min_dur: float = 1.5) -> tuple:
    """The whole sentence in the window that best matches the hook. Never
    cuts mid-sentence; falls back to a centred ~4 s slice."""
    sentences = cues_to_sentences(cues, window_start, window_end)
    if not sentences:
        mid = (window_start + window_end) / 2
        return (max(window_start, mid - 2.0), min(window_end, mid + 2.0))
    hook_toks = _tokens(hook_text)
    best, best_score = None, -1.0
    for ss, se, text in sentences:
        dur = se - ss
        if dur < 0.3:
            continue
        score = (len(hook_toks & _tokens(text)) / max(1, len(hook_toks))) if hook_toks else 0.1
        if dur > 10:
            score -= (dur - 10) * 0.02
        elif dur < 2:
            score -= (2 - dur) * 0.05
        score += (ss - window_start) * 0.00005
        if 4 <= dur <= 8:
            score += 0.01
        if score > best_score:
            best_score, best = score, (ss, se)
    if best is None:
        ss, se, _ = max(sentences, key=lambda s: s[1] - s[0])
        best = (ss, se)
    ss, se = best
    ss = max(window_start, ss - 0.12)
    se = min(window_end, se + 0.25)
    if se - ss < min_dur:
        se = min(window_end, ss + min_dur)
    return ss, se


def _slice_text_for_window(text: str, cs: float, ce: float, ws: float, we: float) -> str:
    """Only the words audible in [ws, we] of the cue [cs, ce]."""
    if ws <= cs and we >= ce:
        return text
    cue_dur = ce - cs
    if cue_dur <= 0.01:
        return text
    sentences = split_sentences(text)
    if len(sentences) > 1:
        sent_words = [max(1, len(s.split())) for s in sentences]
        per_word_t = cue_dur / sum(sent_words)
        out, t = [], cs
        for sent, wc in zip(sentences, sent_words):
            s_end = t + wc * per_word_t
            if s_end > ws and t < we:
                out.append(sent)
            t = s_end
        if out:
            return " ".join(out)
    words = text.split()
    if not words:
        return text
    n = len(words)
    i0 = int(max(0.0, (ws - cs) / cue_dur) * n)
    i1 = max(i0 + 1, int(round(min(1.0, (we - cs) / cue_dur) * n)))
    return " ".join(words[i0:i1])


def stitched_vtt(cues: list, segments: list) -> str:
    """segments: [(abs_start, abs_end, offset_in_output)] → WebVTT text for
    the concatenated output."""
    lines = ["WEBVTT", ""]
    idx = 0
    for abs_s, abs_e, offset in segments:
        for cs, ce, text in cues:
            if ce <= abs_s or cs >= abs_e:
                continue
            ws, we = max(cs, abs_s), min(ce, abs_e)
            sliced = _slice_text_for_window(text, cs, ce, ws, we).strip()
            if not sliced:
                continue
            ns, ne = (ws - abs_s) + offset, (we - abs_s) + offset
            if ne - ns < 0.4:  # a boundary sliver just flashes
                continue
            idx += 1
            lines += [str(idx), f"{seconds_to_vtt_ts(ns)} --> {seconds_to_vtt_ts(ne)}", sliced, ""]
    return "\n".join(lines)
