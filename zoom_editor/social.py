"""Social variants (1:1, 9:16, 16:9) of a clip: cover-fit video, waveform
across the seam, spectrum bars, auto palette, headline text zone (optionally
cycling per segment) and burned-in caption cards.

Ported from nokemo scripts/render_social.py (WaveNetworks/nokemo). The layout
code is unchanged; only ffmpeg paths and the encoder are pluggable here.
"""

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .ff import AUDIO_ARGS, FFMPEG, FFPROBE, run, video_args
from .transcript import slice_cues

# ── Target sizes ────────────────────────────────────────────────────────
# Each layout: (W, H, content_h, text_h, bars_h, wave_overlay_h). Stack
# order top→bottom is content → text → bars. The waveform is NOT a
# stacked strip — it's a translucent full-width overlay centered on the
# seam between content and text, so it straddles that edge and the
# headline gets the full text zone to breathe.
#
# Spectrum bars remain a distinct bottom strip so they read as a
# separate visualizer from the waveform.
SIZES = {
    # Instagram / Facebook square feed
    "1x1":  dict(W=1080, H=1080, content_h=756, text_h=250, bars_h=74,
                 wave_overlay_h=180),
    # Instagram Reels / Stories / Facebook vertical
    "9x16": dict(W=1080, H=1920, content_h=1152, text_h=668, bars_h=100,
                 wave_overlay_h=340),
    # Facebook / YouTube landscape feed
    "16x9": dict(W=1920, H=1080, content_h=840, text_h=180, bars_h=60,
                 wave_overlay_h=140),
}

DEFAULT_FONT = os.environ.get("ZE_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

# ── Subtitles ───────────────────────────────────────────────────────────
# Fontsize per layout width. Captions need to be bold and large enough
# to read on a phone held at arm's length. All layouts use the same
# bold font as the headline.
SUBTITLE_FS = {"1x1": 52, "9x16": 58, "16x9": 46}

def _wrap_pil(text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list:
    words = text.split()
    if not words:
        return []
    lines, cur = [], []
    for w in words:
        candidate = " ".join(cur + [w])
        if font.getlength(candidate) <= max_w or not cur:
            cur.append(w)
        else:
            lines.append(" ".join(cur))
            cur = [w]
    if cur:
        lines.append(" ".join(cur))
    return lines


def render_caption_png(text: str, font_path: str, fontsize: int,
                       max_text_w: int, out_path: Path,
                       bg_color=(0, 0, 0, 210),
                       text_color=(255, 255, 255, 255),
                       radius: int = 22, pad_x: int = 32,
                       pad_y: int = 18) -> tuple:
    """Transparent PNG: rounded-rect bg + centered bold text."""
    font = ImageFont.truetype(font_path, fontsize)
    lines = _wrap_pil(text, font, max_text_w) or [text]
    line_h = int(fontsize * 1.22)
    widths = [int(font.getlength(l)) for l in lines]
    text_w = max(widths)
    text_h = line_h * len(lines)
    img_w = text_w + 2 * pad_x
    img_h = text_h + 2 * pad_y
    img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([(0, 0), (img_w - 1, img_h - 1)],
                           radius=radius, fill=bg_color)
    y = pad_y
    for line, lw in zip(lines, widths):
        x = (img_w - lw) // 2
        draw.text((x, y), line, font=font, fill=text_color)
        y += line_h
    img.save(out_path)
    return img_w, img_h


@dataclass
class Palette:
    accent: tuple  # (r,g,b) bright + saturated → waveform
    bg:     tuple  # (r,g,b) dark                → strip + letterbox

    def hex(self, which):
        r, g, b = getattr(self, which)
        return f"{r:02x}{g:02x}{b:02x}"


# ── Palette extraction ──────────────────────────────────────────────────
def brightness(rgb):
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def saturation(rgb):
    mx = max(rgb); mn = min(rgb)
    return 0.0 if mx == 0 else (mx - mn) / mx


def _sample_frame(src: Path, seek_s: float, tmp: Path) -> Image.Image:
    png = tmp / f"frame_{int(seek_s * 1000)}.png"
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error",
         "-ss", f"{seek_s:.3f}", "-i", str(src),
         "-frames:v", "1", "-vf", "scale=240:-1", str(png)],
        check=True,
    )
    return Image.open(png).convert("RGB")


def pick_palette(src: Path, seek_s: float) -> Palette:
    """Sample 3 frames, quantize into 12 buckets, pick a visually usable
    accent (mid-brightness AND saturated) and a non-black/non-white bg."""
    duration = probe_duration(src)
    offsets = sorted({max(0.2, duration * f) for f in (0.25, 0.5, 0.75)})

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        frames = [_sample_frame(src, t, tmp_path) for t in offsets]
    w, h = frames[0].size
    merged = Image.new("RGB", (w, h * len(frames)))
    for i, fr in enumerate(frames):
        if fr.size != (w, h):
            fr = fr.resize((w, h))
        merged.paste(fr, (0, h * i))

    paletted = merged.quantize(colors=12, method=Image.Quantize.FASTOCTREE)
    pal = paletted.getpalette()[: 12 * 3]
    counts = np.bincount(np.asarray(paletted).flatten(), minlength=12)

    entries = []
    for i in range(12):
        rgb = (pal[i * 3], pal[i * 3 + 1], pal[i * 3 + 2])
        entries.append({"rgb": rgb, "count": int(counts[i]),
                        "bright": brightness(rgb), "sat": saturation(rgb)})
    entries.sort(key=lambda e: e["count"], reverse=True)

    # Warm teal/plum fallback — punchy but not electric. Used when the
    # source is mostly monochrome (dark gallery view, over-exposed screen
    # share) and we can't find a natural accent.
    FALLBACK_ACCENT = (64, 190, 180)   # warm teal
    FALLBACK_BG     = (40, 28, 56)     # deep plum

    # Accent: mid-brightness + saturated. Reject near-black AND near-white.
    # Floor is tight on the dark side — an accent below ~110 brightness
    # shows up as a near-black seam bar once it's a thin strip, defeating
    # the whole point of the visualizer. Better to fall through to the
    # teal fallback than to pick a muddy dark.
    accent_pool = [e for e in entries
                   if 110 <= e["bright"] <= 210 and e["sat"] >= 0.40]
    if not accent_pool:
        # Relax sat once — keep brightness bar
        accent_pool = [e for e in entries
                       if 110 <= e["bright"] <= 210 and e["sat"] >= 0.28]
    if accent_pool:
        # Of the qualifying buckets, pick the most-saturated — most visually
        # striking, not just the most common (which tends toward beige).
        accent_pool.sort(key=lambda e: (e["sat"], e["count"]), reverse=True)
        accent = accent_pool[0]["rgb"]
    else:
        accent = FALLBACK_ACCENT

    # Bg: mid-dark, avoid pure black and pure grey. Prefer buckets with
    # some tint over neutral grey.
    bg_pool = [e for e in entries if 35 <= e["bright"] <= 130]
    if bg_pool:
        bg_pool.sort(key=lambda e: (e["sat"] * 2 + (e["count"] / 1000.0)),
                     reverse=True)
        bg = bg_pool[0]["rgb"]
        if saturation(bg) < 0.10 and brightness(bg) < 60:
            bg = FALLBACK_BG
    else:
        bg = FALLBACK_BG

    # Last check: accent and bg must be distinguishable.
    if abs(brightness(accent) - brightness(bg)) < 30:
        bg = FALLBACK_BG

    return Palette(accent=accent, bg=bg)


# ── FFmpeg filter graph ─────────────────────────────────────────────────
def detect_source_crop(src: Path, duration: float) -> str | None:
    """Run cropdetect on a few samples of the clip. Return a 'W:H:X:Y'
    crop spec that strips any top/bottom/side black bars baked into the
    source (Zoom shared-screen exports are often pillarboxed or have a
    UI strip). Returns None if the video is already full-frame.

    We take the mode of the reported crops so one flash of bright light
    doesn't skew the detection. If the detected crop covers ≥99% of the
    source dimensions we treat it as 'no bars' and return None so we
    don't run a superfluous crop filter.
    """
    from collections import Counter
    # Probe source dims so we can early-out if cropdetect matches the frame.
    probe = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-of", "default=nw=1:nk=1", str(src)],
        capture_output=True, text=True,
    ).stdout.split()
    if len(probe) < 2:
        return None
    sw, sh = int(probe[0]), int(probe[1])

    # Sample at 25/50/75% — skip the first/last seconds where a Zoom
    # clip might still be bumpering.
    samples = [max(1.0, duration * f) for f in (0.25, 0.50, 0.75)]
    crops = []
    for t in samples:
        r = subprocess.run(
            [FFMPEG, "-ss", f"{t:.2f}", "-i", str(src),
             "-frames:v", "24",
             "-vf", "cropdetect=24:2:0",
             "-f", "null", "-"],
            capture_output=True, text=True,
        )
        for line in (r.stderr or "").splitlines():
            if "[Parsed_cropdetect" in line and " crop=" in line:
                idx = line.find(" crop=")
                crops.append(line[idx + 6:].strip())

    if not crops:
        return None
    best, _ = Counter(crops).most_common(1)[0]
    try:
        w, h, x, y = (int(v) for v in best.split(":"))
    except ValueError:
        return None
    # If the detection covers essentially the whole source, skip it.
    if w >= int(sw * 0.99) and h >= int(sh * 0.99) and x <= 2 and y <= 2:
        return None
    # Ensure even dimensions for H.264 encoding.
    w -= w & 1
    h -= h & 1
    return f"{w}:{h}:{x}:{y}"


def probe_duration(src: Path) -> float:
    """Return duration in seconds. Tries format then video stream — some
    container/mp4 combos report format=duration=N/A but have a per-stream
    value (the lotus demo clip in the POC was one of these)."""
    for spec in ("format=duration", "stream=duration"):
        out = subprocess.run(
            [FFPROBE, "-v", "error",
             "-select_streams", "v:0",
             "-show_entries", spec,
             "-of", "default=nw=1:nk=1", str(src)],
            capture_output=True, text=True,
        ).stdout.strip()
        # May be multiple lines (one per matching field) — take first numeric.
        for line in out.splitlines():
            line = line.strip()
            if line and line != "N/A":
                try:
                    return float(line)
                except ValueError:
                    continue
    # Last resort: decode the file end-to-end with -count_packets which
    # works on containers that have no duration in metadata (e.g. some
    # transcoded mp4s).
    out = subprocess.run(
        [FFPROBE, "-v", "error",
         "-select_streams", "v:0",
         "-count_packets",
         "-show_entries", "stream=nb_read_packets,r_frame_rate,avg_frame_rate",
         "-of", "json", str(src)],
        capture_output=True, text=True,
    ).stdout
    try:
        obj = json.loads(out)
        st = obj["streams"][0]
        n = int(st["nb_read_packets"])
        rate_str = st.get("avg_frame_rate") or st.get("r_frame_rate") or "25/1"
        num, den = rate_str.split("/")
        fps = float(num) / float(den) if float(den) else 25.0
        if n > 0 and fps > 0:
            return n / fps
    except Exception:
        pass
    raise RuntimeError(f"Could not determine duration of {src}")


def escape_drawtext(s: str) -> str:
    """ffmpeg drawtext needs colons, backslashes, apostrophes, percent signs
    escaped inside a filtergraph string."""
    return (s.replace("\\", "\\\\")
             .replace(":", "\\:")
             .replace("'", "’")   # curly apostrophe — safer than escaping
             .replace("%", "\\%")
             .replace(",", "\\,"))


def _text_width(text: str, font_path: str, fontsize: int) -> int:
    """Measured width of text at this fontsize, using the same TTF ffmpeg
    will use. Close enough for layout decisions."""
    try:
        f = ImageFont.truetype(font_path, fontsize)
        # Pillow 10+ bbox-based measurement
        bbox = f.getbbox(text)
        return bbox[2] - bbox[0]
    except Exception:
        # Rough estimate as last resort: 0.55 * fontsize per char
        return int(len(text) * fontsize * 0.55)


def _wrap_greedy(text: str, font_path: str, fontsize: int, max_w: int,
                 max_lines: int) -> list | None:
    """Greedy word-wrap up to max_lines lines each fitting max_w.
    Returns list of line strings or None if it couldn't fit."""
    words = text.split()
    if not words:
        return None
    lines = []
    cur = []
    for w in words:
        candidate = " ".join(cur + [w])
        if _text_width(candidate, font_path, fontsize) <= max_w:
            cur.append(w)
        else:
            if not cur:
                # Single word wider than max_w — can't split further.
                return None
            lines.append(" ".join(cur))
            if len(lines) >= max_lines:
                return None
            cur = [w]
    if cur:
        if len(lines) >= max_lines:
            return None
        lines.append(" ".join(cur))
    return lines if len(lines) <= max_lines else None


def plan_headline(text: str, font_path: str, text_w: int, text_h: int,
                  fill: bool = True):
    """Pick (lines[], fontsize) so the headline fills the text zone as
    much as possible without overflowing. Allows up to 3 wrapped lines.

    fill=True biases toward using more vertical space (larger font or
    more lines) so big text zones (9:16 layout) don't look half-empty.
    """
    max_w = int(text_w * 0.90)
    # Bigger headline budget — now up to ~42% of text-zone height when
    # text zone is roomy. Floor bumped so short headlines stay readable.
    max_fs = max(34, min(96, int(text_h * 0.42)))
    min_fs = 26

    best = None  # (score, lines, fs)

    for fs in range(max_fs, min_fs - 1, -2):
        line_h = int(fs * 1.15)
        for nlines in (1, 2, 3):
            # Need the total block to fit vertically too.
            if line_h * nlines > text_h * 0.90:
                continue
            lines = _wrap_greedy(text, font_path, fs, max_w, nlines)
            if lines is None or len(lines) > nlines:
                continue
            # Score: prefer larger fontsize + more complete fill of the box.
            v_fill = (line_h * len(lines)) / text_h
            h_fill_max = max(_text_width(l, font_path, fs) for l in lines) / max_w
            score = fs * 2 + v_fill * 100 + h_fill_max * 20
            if best is None or score > best[0]:
                best = (score, lines, fs)
            # If we're not filling, first valid = good enough.
            if not fill:
                return lines, fs

    if best:
        return best[1], best[2]
    # Last resort — single line at min, may overflow.
    return [text], min_fs


def build_filter(size_key: str, palette: Palette, headline: str,
                 font: str, caption_specs: list | None = None,
                 source_crop: str | None = None,
                 headline_timeline: list | None = None) -> str:
    """caption_specs: list of (png_path, start_s, end_s). Captions overlay
    on the lower third of the clip zone with per-cue `enable` timing.

    source_crop: optional 'W:H:X:Y' applied before cover-fit scale to
    strip black bars from the source (Zoom recordings often have them).

    headline_timeline: optional list of (t0, t1, text). When present the
    text zone cycles labels — each segment's headline is wrapped and
    drawn with enable='between(t,t0,t1)', so a compilation can show a
    different headline for each snippet it contains."""
    sz = SIZES[size_key]
    W, H = sz["W"], sz["H"]
    CW, CH = W, sz["content_h"]
    TW, TH = W, sz["text_h"]
    BarsH = sz["bars_h"]
    WaveOH = sz["wave_overlay_h"]
    bg = palette.hex("bg")
    accent = palette.hex("accent")

    # The waveform overlay straddles the seam, so ~half of it bleeds
    # down into the top of the text zone. Keep the headline clear of
    # that bleed (plus a few px buffer) so the waveform never sits on
    # the text.
    wave_bleed = WaveOH // 2 + 8
    effective_TH = max(TH - wave_bleed, 80)

    # Assemble the drawtext chain(s). If a headline_timeline is provided
    # each entry becomes its own drawtext set gated by `enable`; otherwise
    # we draw a single static headline.
    segments = headline_timeline if headline_timeline else [(None, None, headline)]
    prev = "txtbg"
    text_chain = ""
    label_n = 0
    for seg_idx, (t0, t1, seg_text) in enumerate(segments):
        lines, fs = plan_headline(seg_text, font, TW, effective_TH, fill=True)
        line_h = int(fs * 1.15)
        block_h = line_h * len(lines)
        text_y0 = max(wave_bleed, (TH - block_h) // 2)
        text_y0 = min(text_y0, max(wave_bleed, TH - block_h - 4))
        enable_clause = (f":enable='between(t,{t0:.3f},{t1:.3f})'"
                         if (t0 is not None and t1 is not None) else "")
        for i, ln in enumerate(lines):
            t = escape_drawtext(ln)
            lbl = f"txt{label_n}"
            label_n += 1
            y = text_y0 + i * line_h
            text_chain += (
                f"[{prev}]drawtext=fontfile={font}:text='{t}':"
                f"fontcolor=white:fontsize={fs}:"
                f"x=(w-tw)/2:y={y}:"
                f"shadowcolor=black@0.55:shadowx=2:shadowy=2"
                f"{enable_clause}[{lbl}];"
            )
            prev = lbl
    text_final = prev

    # Waveform overlay sits centered on the content/text seam.
    overlay_y = CH - WaveOH // 2

    # Optionally strip source black bars before cover-fit. Keeps Zoom
    # letterbox/pillarbox out of the taller 1x1 and 9x16 variants.
    src_crop_prefix = f"crop={source_crop}," if source_crop else ""

    base = (
        # Content: (optional bar-strip crop) → cover-fit scale → crop to
        # target content box. No letterbox, clip meets the side borders.
        f"[0:v]{src_crop_prefix}"
        f"scale={CW}:{CH}:force_original_aspect_ratio=increase,"
        f"crop={CW}:{CH},setsar=1[content];"

        # Split audio so each viz filter has its own stream
        f"[0:a]asplit=3[a_wave][a_bars][a_out];"

        # Waveform overlay sprite: showwaves renders the line on an
        # implicit black bg. Key out the black so only the accent-colored
        # line survives — no visible rectangle at the seam. blend=0.2
        # keeps the cline antialiasing soft so edges don't jag out.
        #
        # A solid accent bar is drawn through the vertical center of the
        # sprite BEFORE the colorkey: this becomes the continuous seam
        # cover so the dark edge between clip and text zone is hidden
        # even during silent passages when the cline line is thin.
        f"[a_wave]showwaves=s={W}x{WaveOH}:mode=cline:"
        f"colors=0x{accent}|0x{accent}:rate=25:draw=full,"
        f"drawbox=x=0:y={WaveOH // 2 - 3}:w={W}:h=6:color=0x{accent}:t=fill,"
        f"colorkey=color=0x000000:similarity=0.30:blend=0.20,"
        f"format=yuva420p[wave];"

        # Bars strip: full-width spectrum bars anchored to the bottom of
        # the video. Fully opaque so it reads as a distinct element from
        # the translucent waveform at the seam.
        f"[a_bars]showfreqs=s={W}x{BarsH}:mode=bar:"
        f"fscale=log:ascale=log:cmode=combined:"
        f"colors=0x{accent}|0x{accent}:win_size=2048,"
        f"format=rgba,colorchannelmixer=aa=0.95[bars_fg];"
        f"color=c=0x{bg}:s={W}x{BarsH}:r=25[bars_bg];"
        f"[bars_bg][bars_fg]overlay=0:0:format=auto[bars];"

        # Text zone: bg + multi-line drawtext
        f"color=c=0x{bg}:s={TW}x{TH}:r=25[txtbg];"
        f"{text_chain}"

        # Stack: content → text → bars. Waveform overlays the seam.
        f"[content][{text_final}][bars]vstack=inputs=3[stacked];"
    )

    # Waveform overlay output label depends on whether captions follow.
    wave_tail = "pre_sub" if caption_specs else "outv"
    graph = base + (
        f"[stacked][wave]overlay=0:{overlay_y}:format=auto[{wave_tail}]"
    )

    if not caption_specs:
        return graph

    # Caption band: bottom of caption anchors ~8% above the clip bottom,
    # keeping the text inside the video portion and off the waveform.
    sub_anchor_y = int(CH * 0.92)

    prev = "pre_sub"
    parts = [graph]
    for i, (png, start_s, end_s) in enumerate(caption_specs):
        last = (i == len(caption_specs) - 1)
        out_lbl = "outv" if last else f"sub{i}"
        safe = str(png).replace("\\", "\\\\").replace(":", "\\:")
        parts.append(
            f";movie='{safe}'[cap{i}];"
            f"[{prev}][cap{i}]overlay=x=(main_w-overlay_w)/2:"
            f"y={sub_anchor_y}-overlay_h:"
            f"enable='between(t,{start_s:.3f},{end_s:.3f})'[{out_lbl}]"
        )
        prev = out_lbl
    return "".join(parts)


def render_one(src: Path, out: Path, size_key: str, palette: Palette,
               headline: str, font: str, duration: float,
               caption_cues: list | None = None,
               caption_dir: Path | None = None,
               source_crop: str | None = None,
               headline_timeline: list | None = None,
               crf: int = 20) -> None:
    sz = SIZES[size_key]
    caption_specs = []
    if caption_cues and caption_dir is not None:
        caption_dir.mkdir(parents=True, exist_ok=True)
        fs = SUBTITLE_FS[size_key]
        # Caption width cap so cards don't touch the side borders; the
        # rounded rect grows to fit text and the overlay centers it.
        max_text_w = int(sz["W"] * 0.82) - 64  # minus pad_x*2
        for i, (s, e, text) in enumerate(caption_cues):
            png = caption_dir / f"{size_key}_{i:04d}.png"
            render_caption_png(text, font, fs, max_text_w, png)
            caption_specs.append((png, s, e))

    flt = build_filter(size_key, palette, headline, font, caption_specs,
                       source_crop=source_crop,
                       headline_timeline=headline_timeline)
    cmd = [
        FFMPEG, "-y", "-loglevel", "error",
        "-i", str(src),
        "-filter_complex", flt,
        "-map", "[outv]", "-map", "[a_out]",
        *video_args(crf),
        *AUDIO_ARGS,
        "-movflags", "+faststart",
        "-t", f"{duration:.3f}",
        str(out),
    ]
    run(cmd)


def _hex_rgb(s: str) -> tuple:
    s = s.lstrip("#")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def render_variants(src: Path, out_dir: Path, stem: str, headline: str,
                    sizes=("1x1", "9x16", "16x9"), cues: list | None = None,
                    clip_start: float = 0.0, headline_timeline: list | None = None,
                    accent: str | None = None, bg: str | None = None,
                    cropdetect: bool = True, font: str = DEFAULT_FONT,
                    quality: int = 20, on_progress=None) -> dict:
    """Render each size of `src` to out_dir/<stem>_<size>.mp4.

    cues: full-transcript cues; the window [clip_start, clip_start+duration]
    is burned in as captions. headline_timeline: [(t0, t1, text)] cycles the
    headline. Returns {"files": {size: path}, "palette": {...}, "crop": ...}.
    """
    src, out_dir = Path(src), Path(out_dir)
    for s in sizes:
        if s not in SIZES:
            raise ValueError(f"unknown size {s!r}; options: {list(SIZES)}")
    if not Path(font).is_file():
        raise FileNotFoundError(f"font not found: {font}")
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = probe_duration(src)
    if accent and bg:
        palette = Palette(accent=_hex_rgb(accent), bg=_hex_rgb(bg))
    else:
        palette = pick_palette(src, duration / 2.0)
    crop = detect_source_crop(src, duration) if cropdetect else None
    caption_cues = slice_cues(cues, clip_start, clip_start + duration) if cues else None
    files = {}
    with tempfile.TemporaryDirectory(prefix="ze_cap_") as capdir:
        for i, size_key in enumerate(sizes):
            if on_progress:
                on_progress(i / len(sizes), f"rendering {size_key}")
            out = out_dir / f"{stem}_{size_key}.mp4"
            render_one(src, out, size_key, palette, headline, font, duration,
                       caption_cues=caption_cues or None,
                       caption_dir=Path(capdir) if caption_cues else None,
                       source_crop=crop, headline_timeline=headline_timeline,
                       crf=quality)
            files[size_key] = out
    return {"files": files, "duration": duration, "crop": crop,
            "palette": {"accent": palette.hex("accent"), "bg": palette.hex("bg")},
            "captions": len(caption_cues or [])}
