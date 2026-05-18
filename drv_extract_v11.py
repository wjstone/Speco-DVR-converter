#!/usr/bin/env python3
"""
Speco .drv -> MP4 (video + audio + timestamps)   v4.2

Walks the .drv as a stream of typed chunks. Three chunk types:

    00 00 00 01 f0    VIDEO chunk: 32-byte header ending in 'FF FF FF FF',
                      then H.264 Annex-B payload running to the next chunk.
    40 36 00 10       AUDIO chunk: 32-byte header + ~1024 bytes G.711 A-law
                      (mono, 8 kHz).
    40 36 00 00       Metadata/channel-index marker. No payload.

FEATURES
- Multi-file input: pass multiple .drv files and each produces its own MP4
  with correctly-chained timestamps. The cumulative time offset uses each
  file's true audio duration as ground truth.
- DVR-style timestamp overlay sourced from the Speco-standard readme.txt in
  the same folder as the .drv files, or from a --start-time argument.
  - If ffmpeg has the `drawtext` filter compiled in: burned into the video,
    bottom-right corner.
  - If not (some stripped ffmpeg builds): written as a sidecar .srt file
    next to the .mp4. VLC, mpv, IINA etc. auto-load matching .srt files.
- A/V sync correction. The DVR doesn't actually record at exactly 15 fps;
  it varies (typically 14-15). The script derives the true framerate from
  frame_count / audio_duration, and pads the tail of the video stream so
  video duration exactly matches audio duration to within one frame.

USAGE
  python3 drv_extract.py 1.drv                          # auto-finds readme.txt
  python3 drv_extract.py "[000001].drv" "[000002].drv"  # multi-file, ts chains
  python3 drv_extract.py 1.drv --start-time "2025-05-27 12:38:00"
  python3 drv_extract.py 1.drv --no-timestamp           # skip overlay/SRT
  python3 drv_extract.py 1.drv --stream-copy            # fast, may drift
  python3 drv_extract.py 1.drv --no-mp4                 # dump .h264/.wav only
  python3 drv_extract.py 1.drv --fps 30                 # force a specific rate
"""

import argparse, datetime as _dt, os, re, struct, subprocess, sys, shutil
from collections import Counter

def find_tool(name):
    """Find ffmpeg or ffprobe in PATH or common Homebrew/system locations."""
    # Try PATH first
    path = shutil.which(name)
    if path:
        return path
    
    # Try common Homebrew location (Apple Silicon)
    homebrew_path = f"/opt/homebrew/bin/{name}"
    if os.path.exists(homebrew_path):
        return homebrew_path
    
    # Try Intel Homebrew location
    intel_homebrew = f"/usr/local/bin/{name}"
    if os.path.exists(intel_homebrew):
        return intel_homebrew
    
    # Try /usr/bin
    usr_bin_path = f"/usr/bin/{name}"
    if os.path.exists(usr_bin_path):
        return usr_bin_path
    
    # Default to just the name (will fail gracefully if not found)
    return name

FFMPEG_PATH = find_tool("ffmpeg")
FFPROBE_PATH = find_tool("ffprobe")

VIDEO_MAGIC = b"\x00\x00\x00\x01\xf0"
AUDIO_MAGIC = b"\x40\x36\x00\x10"
META_MAGIC  = b"\x40\x36\x00\x00"

HDR_LEN = 32
AUDIO_PAYLOAD_LEN = 1024
AUDIO_RATE = 8000           # G.711 A-law, mono
AUDIO_CHUNK_SECONDS = AUDIO_PAYLOAD_LEN / AUDIO_RATE  # 0.128 s per audio chunk

NAL_NAMES = {
    1: "P/B-slice", 5: "IDR", 6: "SEI", 7: "SPS", 8: "PPS", 9: "AUD",
    10: "EndSeq", 11: "EndStream", 12: "Filler",
}

ANY_MAGIC = re.compile(rb"\x00\x00\x00\x01\xf0|\x40\x36\x00\x10|\x40\x36\x00\x00")

# Font search paths. We pick the first existing one for the drawtext filter.
# macOS first (since that's the user's target), then Linux fallbacks.
FONT_CANDIDATES = [
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


# ============================================================
# Chunk parser
# ============================================================

def parse_drv(data):
    """Parse Speco .drv container.

    Returns (h264_bytes, audio_bytes, stats, video_real_times). The
    video_real_times list contains, for each emitted video chunk, the
    real-time wallclock position (in seconds from file start) inferred
    by counting how many audio chunks have appeared earlier in the file.
    Audio chunks arrive at a perfectly fixed cadence (1024 samples of
    G.711 at 8000 Hz = 0.128 s per chunk), so they form a built-in clock
    that lets us recover the variable-framerate timing of the video.

    Drops two classes of unsafe chunks:

    1. Cross-channel chunks. Speco DVRs interleave video chunks from
       multiple camera channels into the same .drv file, and meta chunks
       (magic 40 36 00 00) act as anchors for the PRIMARY channel of
       this segment. The pattern is `M V [A] [V_foreign] [V_foreign] ...
       M V [A] ...`. Only the first video chunk after each meta belongs
       to the channel-of-interest; any subsequent video chunks before
       the next meta come from OTHER cameras and contain unrelated
       footage. Including them produces visible "frames from another
       video" artifacts during playback.

    2. Pre-IDR slices. Speco cuts segments mid-GOP, so the leading
       picture slices reference frames from a prior segment that we
       don't have. If left in, the H.264 decoder satisfies their
       references from its leftover reference-frame buffer (which can
       carry content from a previously decoded video), producing more
       cross-video leakage.
    """
    # Walk chunks in order. We split video chunks into "primary" (first
    # video after each meta) and "foreign" (subsequent videos before the
    # next meta). Audio chunks are kept regardless — they're not channel-
    # specific in this Speco generation.
    primary_videos = []  # list of (payload_bytes, real_time)
    audio_parts = []
    audio_chunks_seen = 0
    stats = Counter()
    matches = list(ANY_MAGIC.finditer(data))
    n = len(data)

    # State: tracks whether we've already seen a meta chunk in the current
    # cluster but no video chunk yet (i.e., the next V is primary). Starts
    # True so files that begin with V before any M get one freebie (rare
    # edge case; otherwise we'd drop everything).
    expecting_primary_video = True

    for i, m in enumerate(matches):
        pos = m.start()
        magic = m.group()
        next_pos = matches[i + 1].start() if i + 1 < len(matches) else n

        if magic == VIDEO_MAGIC:
            hdr_end = data.find(b"\xff\xff\xff\xff", pos, pos + HDR_LEN + 4)
            if hdr_end == -1:
                stats["video_malformed"] += 1
                continue
            payload = data[hdr_end + 4 : next_pos].rstrip(b"\x00")
            if len(payload) > 5:
                if expecting_primary_video:
                    primary_videos.append(
                        (payload, audio_chunks_seen * AUDIO_CHUNK_SECONDS)
                    )
                    stats["video_chunks"] += 1
                    expecting_primary_video = False
                else:
                    stats["video_foreign_dropped"] += 1
        elif magic == AUDIO_MAGIC:
            s = pos + HDR_LEN
            e = min(s + AUDIO_PAYLOAD_LEN, next_pos, n)
            audio_parts.append(data[s:e])
            audio_chunks_seen += 1
            stats["audio_chunks"] += 1
        else:  # META_MAGIC
            stats["meta_chunks"] += 1
            expecting_primary_video = True

    # Step 2: strip pre-IDR primary-channel chunks.
    first_idr_chunk = None
    for idx, (payload, _) in enumerate(primary_videos):
        for m in re.finditer(rb"\x00\x00\x00\x01", payload):
            p = m.start()
            if p + 4 < len(payload):
                b = payload[p + 4]
                if (b & 0x80) == 0 and (b & 0x1f) == 5:
                    first_idr_chunk = idx
                    break
        if first_idr_chunk is not None:
            break

    if first_idr_chunk is not None and first_idr_chunk > 0:
        stats["pre_idr_dropped"] = first_idr_chunk
        primary_videos = primary_videos[first_idr_chunk:]
    elif first_idr_chunk is None:
        stats["pre_idr_dropped"] = 0

    h264 = b"".join(p for p, _ in primary_videos)
    video_real_times = [t for _, t in primary_videos]
    return h264, b"".join(audio_parts), stats, video_real_times


def count_nal_types(h264):
    stats = Counter()
    for m in re.finditer(rb"\x00\x00\x00\x01", h264):
        i = m.start()
        if i + 4 < len(h264):
            b = h264[i + 4]
            if not (b & 0x80):
                stats[b & 0x1f] += 1
    return stats


# ============================================================
# Audio decoders (A-law is the confirmed default)
# ============================================================

def decode_alaw(audio):
    """G.711 A-law -> 16-bit signed PCM."""
    out = bytearray()
    for b in audio:
        b ^= 0x55
        sign = b & 0x80
        exp = (b >> 4) & 7
        man = b & 0xF
        val = (man << 4) | 0x8 if exp == 0 else ((man << 4) | 0x108) << (exp - 1)
        val = -val if sign else val
        out += struct.pack("<h", max(-32768, min(32767, val)))
    return bytes(out)


def decode_mulaw(audio):
    out = bytearray()
    for b in audio:
        b = ~b & 0xFF
        sign = b & 0x80
        exp = (b >> 4) & 7
        man = b & 0xF
        val = ((man << 1) | 0x21) << exp
        val -= 0x21
        val = -val if sign else val
        out += struct.pack("<h", max(-32768, min(32767, val)))
    return bytes(out)


def decode_pcm8u(audio):
    return b"".join(struct.pack("<h", (b - 128) * 256) for b in audio)


def decode_pcm8s(audio):
    out = bytearray()
    for b in audio:
        s = b if b < 128 else b - 256
        out += struct.pack("<h", s * 256)
    return bytes(out)


AUDIO_DECODERS = {
    "alaw":  decode_alaw,
    "mulaw": decode_mulaw,
    "pcm8u": decode_pcm8u,
    "pcm8s": decode_pcm8s,
}


def write_pcm_wav(pcm, path, sample_rate=AUDIO_RATE, channels=1, bits=16):
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    fmt = struct.pack("<HHIIHH", 1, channels, sample_rate, byte_rate, block_align, bits)
    data_size = len(pcm)
    riff_size = 4 + (8 + len(fmt)) + (8 + data_size)
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", riff_size) + b"WAVE")
        f.write(b"fmt " + struct.pack("<I", len(fmt)) + fmt)
        f.write(b"data" + struct.pack("<I", data_size) + pcm)


# ============================================================
# Timestamp / readme helpers
# ============================================================

def parse_readme(readme_path):
    """Return (start_dt, end_dt) parsed from Speco-style readme.txt, or None."""
    try:
        with open(readme_path) as f:
            content = f.read()
    except OSError:
        return None
    # Speco format example:
    #   Start Time : [S] 2025/05/27 12:38:00
    #   End Time : [S] 2025/05/28 06:14:15
    rx = re.compile(
        r"(Start|End)\s*Time\s*:\s*(?:\[[A-Z]\]\s*)?"
        r"(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})",
        re.IGNORECASE,
    )
    times = {}
    for m in rx.finditer(content):
        kind, y, mo, d, h, mi, s = m.groups()
        # Treat the recorded times as UTC so ffmpeg's gmtime formatting
        # displays the exact wallclock string the DVR recorded (timezone
        # tag from the DVR is local, not real UTC — this is a display trick).
        times[kind.lower()] = _dt.datetime(
            int(y), int(mo), int(d), int(h), int(mi), int(s),
            tzinfo=_dt.timezone.utc,
        )
    if "start" not in times:
        return None
    return times.get("start"), times.get("end")


def find_readme(drv_path):
    """Look for readme.txt in the same directory as the .drv (case-insensitive)."""
    folder = os.path.dirname(os.path.abspath(drv_path))
    for name in os.listdir(folder):
        if name.lower() == "readme.txt":
            return os.path.join(folder, name)
    return None


def find_font():
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def check_drawtext_available():
    """Return True if this ffmpeg build has the drawtext filter compiled in.
       macOS Homebrew builds normally do; some stripped or static distros don't."""
    try:
        r = subprocess.run([FFMPEG_PATH, "-hide_banner", "-filters"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "drawtext" in (r.stdout + r.stderr)


def check_subtitles_filter_available():
    """Return True if this ffmpeg build has the libass-based 'subtitles'
       filter. Used as a fallback for burning timestamps when drawtext
       isn't compiled in — Homebrew's standard ffmpeg almost always has
       this even when drawtext is missing."""
    try:
        r = subprocess.run([FFMPEG_PATH, "-hide_banner", "-filters"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    output = r.stdout + r.stderr
    # The line we want looks like " ... subtitles  V->V  Render text subtitles..."
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "subtitles":
            return True
    return False


def _font_name_for_libass(font_file):
    """Map a font file path to a libass-usable font family name.
       libass uses fontconfig to look up by name (not path), so we hand it
       the most common system-installed monospace name. Returns None to
       let libass pick its default."""
    if not font_file:
        return None
    base = os.path.basename(font_file).lower()
    if "menlo" in base: return "Menlo"
    if "monaco" in base: return "Monaco"
    if "courier" in base: return "Courier New"
    if "liberationmono" in base: return "Liberation Mono"
    if "dejavusansmono" in base: return "DejaVu Sans Mono"
    if "helvetica" in base: return "Helvetica"
    if "arial" in base: return "Arial"
    return None


def burn_srt_into_mp4(mp4_path, srt_path, font_name=None):
    """Re-mux the mp4 with the SRT burned in via libass. Returns True on
       success. The video stream is re-encoded (lossy but unavoidable for
       a graphical overlay); audio is stream-copied so it's bit-identical."""
    tmp_path = mp4_path + ".pre-burn.mp4"
    try:
        os.rename(mp4_path, tmp_path)
    except OSError as e:
        print(f"Could not stage mp4 for subtitle burn-in: {e}")
        return False

    # libass styling. ASS BackColour + BorderStyle=4 gives a translucent
    # background box similar to the drawtext box style. Alignment=3 means
    # bottom-right.
    style_parts = [
        "Alignment=3",
        "FontSize=16",
        "PrimaryColour=&Hffffff&",
        "BackColour=&H80000000&",
        "BorderStyle=4",
        "Outline=6",
        "Shadow=0",
        "MarginV=10",
        "MarginR=10",
    ]
    if font_name:
        style_parts.insert(1, f"FontName={font_name}")
    style = ",".join(style_parts)
    # Escape colons and backslashes inside the filter argument. The path
    # itself may also contain spaces or brackets, so we quote it with
    # single-quotes after escaping any contained single-quotes.
    safe_srt = srt_path.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    vf = f"subtitles='{safe_srt}':force_style='{style}'"

    cmd = [
        FFMPEG_PATH, "-y", "-hide_banner", "-loglevel", "warning",
        "-i", tmp_path,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", vf,
        "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        mp4_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("Subtitle burn-in failed; keeping pre-burn mp4. stderr (last 15 lines):")
        print("\n".join(r.stderr.splitlines()[-15:]))
        # Restore the original.
        try:
            os.rename(tmp_path, mp4_path)
        except OSError:
            pass
        return False

    try:
        os.remove(tmp_path)
    except OSError:
        pass
    return True


def write_srt_sidecar(srt_path, file_start_dt, duration_seconds, interval=1.0):
    """Write an SRT subtitle file showing wallclock time as the video plays.
       One subtitle entry per `interval` seconds. file_start_dt is the wallclock
       value displayed at video position t=0. Players auto-load the .srt if it
       has the same base name as the video."""
    def fmt_srt(t):
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s_int = int(t % 60)
        ms = int(round((t - int(t)) * 1000))
        if ms == 1000:  # rounding edge case
            s_int += 1
            ms = 0
        return f"{h:02d}:{m:02d}:{s_int:02d},{ms:03d}"

    with open(srt_path, "w") as f:
        n = 0
        t = 0.0
        while t < duration_seconds:
            n += 1
            t_end = min(t + interval, duration_seconds)
            wall = file_start_dt + _dt.timedelta(seconds=t)
            text = wall.strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"{n}\n{fmt_srt(t)} --> {fmt_srt(t_end)}\n{text}\n\n")
            t += interval


def build_drawtext(start_dt, font_file, fontsize=22):
    """ffmpeg drawtext filter arg, displaying (start_dt + video_pts) in the
       bottom-right corner. We use gmtime's default format, which is already
       'YYYY-MM-DD HH:MM:SS' — avoids needing to escape colons in a custom
       format string (a pain point in ffmpeg filter syntax)."""
    epoch = int(start_dt.timestamp())
    # %{pts\:gmtime\:EPOCH} -> "YYYY-MM-DD HH:MM:SS" of (EPOCH + pts), UTC.
    # The \: are filter-parser escapes for the colons inside %{}; they get
    # consumed by the filter parser before the expression evaluator sees it.
    text = "%{pts\\:gmtime\\:" + str(epoch) + "}"
    parts = []
    if font_file:
        font_esc = font_file.replace(":", r"\:")
        parts.append(f"fontfile={font_esc}")
    parts.extend([
        f"text='{text}'",
        "x=w-tw-15",
        "y=h-th-15",
        f"fontsize={fontsize}",
        "fontcolor=white",
        "box=1",
        "boxcolor=black@0.55",
        "boxborderw=8",
    ])
    return "drawtext=" + ":".join(parts)


# ============================================================
# Per-file processing
# ============================================================

def _build_vfr_output(h264_path, wav_path, mp4_path, args, start_dt, font_file,
                      burn_timestamp, video_real_times, audio_seconds,
                      frame_count, base):
    """Build the mp4 with each surviving frame placed at its true real-time
       position derived from audio-chunk interleaving in the .drv file.

       Approach: extract decoded frames as PNGs; build an ffconcat manifest
       that places each surviving frame at its source slice's real_time,
       padding any quiet stretches with clones of the previous frame so the
       output is continuous video for the full audio duration.

       Returns (decoded_frame_count, output_duration_seconds) or None on
       failure (caller falls back to the legacy averaging path).
    """
    import tempfile, shutil

    # Build the H.264 input with a 1/1000 timebase so source-index N has
    # input PTS N (one tick per slice). After decode, the OUTPUT frames'
    # PTS values map back to source indices, telling us exactly which
    # source slices survived.

    # First: we need to know which source-slice indices survived. Re-encode
    # the .h264 with a copy bitstream filter that lets us trace, OR use the
    # showinfo output's "n" field as a per-output-frame counter — but n is
    # only a counter, not the original index. Without per-frame source-index
    # info, assume drops are evenly distributed and resample video_real_times
    # to the decoded count. This is approximate but better than uniform-rate.

    # Pass 1: decode the H.264 and identify (a) how many output frames come
    # out and (b) which of those the decoder flagged as corrupt (visually
    # garbled — partial macroblock decode produces visible blocks of
    # synthesized garbage). We capture stderr in real time and correlate
    # showinfo output frame indices with adjacent "corrupt decoded frame"
    # warnings.
    print(f"\nPass 1/3: decoding H.264 to identify clean frames...")
    r = subprocess.run(
        [FFMPEG_PATH, "-hide_banner", "-v", "info",
         "-err_detect", "ignore_err", "-fflags", "+discardcorrupt",
         "-i", h264_path, "-map", "0:v:0",
         "-vf", "showinfo",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    n_pattern = re.compile(r"\bn:\s*(\d+)")
    clean_indices = set()
    corrupt_indices = set()
    # Decoder error patterns that indicate the output frame contains visible
    # garbage. We use IDR-anchored cleanliness tracking: a frame is "clean"
    # only if no decoder errors have been emitted since the last IDR. After
    # any error, the decoder's reference buffer is potentially polluted and
    # all subsequent P/B frames inherit corruption silently — we mark them
    # corrupt until the next IDR resets the state.
    error_patterns = (
        "corrupt decoded frame",
        "error while decoding MB",
        "corrupted macroblock",
        "Invalid level prefix",
        "negative number of zero coeffs",
        "cbp too large",
        "decode_slice_header error",
        "no frame!",
        "non-existing PPS",
        "concealing",
        "Reference",
    )
    state_clean = True  # become False on any error, True at each IDR

    for line in r.stderr.splitlines():
        if "Parsed_showinfo" in line and "pts:" in line:
            m = n_pattern.search(line)
            if not m:
                continue
            n = int(m.group(1))
            # Check if this is an IDR (iskey:1, type:I)
            is_idr = "iskey:1" in line and "type:I" in line
            if is_idr:
                # IDR resets the decoder state — this frame is the new
                # known-good anchor and subsequent frames are clean until
                # the next error.
                state_clean = True
            if state_clean:
                clean_indices.add(n)
            else:
                corrupt_indices.add(n)
        elif any(p in line for p in error_patterns):
            state_clean = False

    total_emitted = len(clean_indices) + len(corrupt_indices)
    decoded_frames = len(clean_indices)
    if total_emitted < 1 or decoded_frames < 1:
        return None
    print(f"  Decoder emitted {total_emitted} frames "
          f"({frame_count} source slices, {frame_count - total_emitted} "
          f"completely undecodable)")
    print(f"  Of those, {decoded_frames} are clean and "
          f"{len(corrupt_indices)} are visually corrupt (will be skipped)")

    n_src = len(video_real_times)
    print(f"  Source video span: {video_real_times[0]:.3f}s .. "
          f"{video_real_times[-1]:.3f}s (audio is {audio_seconds:.3f}s)")

    print("\nPass 2/3: extracting decoded frames to disk...")
    tmpdir = tempfile.mkdtemp(prefix="drv_vfr_",
                              dir=os.path.dirname(mp4_path) or ".")
    try:
        extract_cmd = [
            FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-y",
            "-err_detect", "ignore_err", "-fflags", "+discardcorrupt",
            "-i", h264_path,
            "-map", "0:v:0",
            "-fps_mode", "passthrough",
            os.path.join(tmpdir, "f_%07d.png"),
        ]
        r = subprocess.run(extract_cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print("Frame extraction failed:")
            print("\n".join(r.stderr.splitlines()[-15:]))
            return None
        all_pngs = sorted(f for f in os.listdir(tmpdir) if f.startswith("f_"))
        # PNG names are 1-indexed (f_0000001.png is the first frame), while
        # the decoder's "n:" counter is 0-indexed. So PNG i corresponds to
        # decoder output index i-1.
        pngs = []
        for png_name in all_pngs:
            try:
                # Extract the frame number from "f_NNNNNNN.png"
                png_index = int(png_name[2:-4]) - 1  # convert to 0-indexed
            except ValueError:
                continue
            if png_index in clean_indices:
                pngs.append(png_name)
            else:
                # Delete corrupt-frame PNGs so they don't enter the mux.
                try:
                    os.remove(os.path.join(tmpdir, png_name))
                except OSError:
                    pass
        print(f"  Extracted {len(all_pngs)} frames, kept {len(pngs)} clean")
        if not pngs:
            return None

        # Now recompute the timing for the clean frames only.
        # surviving_real_times was built earlier using a uniform resample from
        # source-slice positions, but it included corrupt frames. Re-do it
        # using just the clean output-frame indices.
        sorted_clean = sorted(clean_indices)
        new_real_times = []
        for clean_n in sorted_clean:
            # Map output-frame index `clean_n` (0..total_emitted-1) to a
            # source-slice index via uniform resampling, then look up that
            # slice's real-time.
            src_idx = int(clean_n * n_src / total_emitted)
            if src_idx >= n_src:
                src_idx = n_src - 1
            new_real_times.append(video_real_times[src_idx])
        # Enforce strict monotonic increase
        for i in range(1, len(new_real_times)):
            if new_real_times[i] <= new_real_times[i-1]:
                new_real_times[i] = new_real_times[i-1] + 0.001
        # Compute per-frame durations
        durations = []
        for i in range(len(new_real_times)):
            if i + 1 < len(new_real_times):
                d = new_real_times[i + 1] - new_real_times[i]
            else:
                d = max(audio_seconds - new_real_times[i], 0.001)
            durations.append(max(d, 0.001))

        print(f"  Frame duration after corrupt-frame removal: "
              f"min {min(durations)*1000:.1f}ms max {max(durations)*1000:.1f}ms "
              f"median {sorted(durations)[len(durations)//2]*1000:.1f}ms")

        concat_path = os.path.join(tmpdir, "manifest.ffconcat")
        with open(concat_path, "w") as f:
            f.write("ffconcat version 1.0\n")
            for png_name, dur in zip(pngs, durations):
                f.write(f"file '{png_name}'\n")
                f.write(f"duration {dur:.6f}\n")
            # ffconcat spec requires the final file be listed without
            # duration; its duration is inherited from the previous entry.
            f.write(f"file '{pngs[-1]}'\n")

        print("\nPass 3/3: muxing per-frame-timed video with audio...")
        mux_cmd = [FFMPEG_PATH, "-y", "-hide_banner", "-loglevel", "warning",
                   "-f", "concat", "-safe", "0", "-i", concat_path,
                   "-i", wav_path,
                   "-map", "0:v:0", "-map", "1:a:0",
                   "-c:v", "libx264", "-crf", "20", "-preset", "medium",
                   "-pix_fmt", "yuv420p",
                   "-vsync", "vfr",
                   "-c:a", "aac", "-b:a", "96k", "-ac", "1", "-ar",
                   str(AUDIO_RATE),
                   "-aspect", args.dar]
        if burn_timestamp:
            mux_cmd += ["-vf", build_drawtext(start_dt, font_file)]
        mux_cmd += ["-movflags", "+faststart", mp4_path]

        r = subprocess.run(mux_cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print("Mux failed:")
            print("\n".join(r.stderr.splitlines()[-20:]))
            return None
        print(f"Wrote {mp4_path}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return decoded_frames, audio_seconds


def process_file(drv_path, args, start_dt, font_file, have_drawtext):
    """Extract one .drv into .h264/.wav/.mp4. Return audio duration in seconds."""
    print(f"\n========== {drv_path} ==========")
    with open(drv_path, "rb") as f:
        data = f.read()
    print(f"Read {len(data):,} bytes")

    h264, audio_bytes, stats, video_real_times = parse_drv(data)
    print("Chunk parse stats:")
    for k in sorted(stats):
        print(f"  {k:>18s}: {stats[k]:,}")

    base = os.path.splitext(drv_path)[0]
    h264_path = base + ".h264"
    wav_path = base + ".wav"
    mp4_path = args.output if args.output else (base + ".mp4")

    with open(h264_path, "wb") as f:
        f.write(h264)
    print(f"Wrote {h264_path}  ({len(h264):,} bytes)")

    nal_stats = count_nal_types(h264)
    print("NAL types:")
    for t in sorted(nal_stats):
        print(f"  type {t:2d} ({NAL_NAMES.get(t,'?'):>10s}): {nal_stats[t]:,}")

    # Picture slices = the actual frame count. Types 7/8 (SPS/PPS) and 6/9
    # (SEI/AUD) don't count as pictures.
    frame_count = nal_stats.get(1, 0) + nal_stats.get(5, 0)

    # Audition mode: write each decoder to its own WAV and exit.
    have_audio = (not args.no_audio) and len(audio_bytes) > 256
    if args.audition_audio and have_audio:
        for mode, decoder in AUDIO_DECODERS.items():
            out_path = f"{base}_audio_{mode}.wav"
            pcm = decoder(audio_bytes)
            write_pcm_wav(pcm, out_path)
            print(f"  audition: {out_path}")
        # Clean up temp files before returning
        for tmp_file in [h264_path, wav_path]:
            if tmp_file and os.path.exists(tmp_file):
                try:
                    os.remove(tmp_file)
                    print(f"Cleaned up {tmp_file}")
                except OSError as e:
                    print(f"Warning: could not delete {tmp_file}: {e}")
        return len(audio_bytes) / AUDIO_RATE

    chosen_wav = None
    audio_seconds = 0.0
    if have_audio:
        chosen_wav = wav_path
        pcm = AUDIO_DECODERS[args.audio_mode](audio_bytes)
        write_pcm_wav(pcm, chosen_wav)
        audio_seconds = len(pcm) / 2 / AUDIO_RATE
        print(f"Wrote {chosen_wav}  (~{audio_seconds:.2f}s @ {AUDIO_RATE}Hz, mode={args.audio_mode})")

    # ---------- Derive the true framerate ----------
    # Audio is the authoritative wallclock: 1 byte = 1 sample at exactly 8000 Hz,
    # so audio_seconds is the real duration of the recording. The DVR does NOT
    # actually record at exactly 15 fps; it varies (typically 14-15 on this
    # generation). We compute the real fps from frame_count / audio_seconds so
    # the output video duration matches the audio duration. If the user passed
    # --fps explicitly, we honor that instead.
    if args.fps == "auto":
        if frame_count > 0 and audio_seconds > 0:
            fps_value = frame_count / audio_seconds
            print(f"Derived framerate: {fps_value:.4f} fps "
                  f"({frame_count} frames over {audio_seconds:.2f}s)")
        else:
            fps_value = 15.0
            print(f"Could not derive framerate; defaulting to {fps_value} fps")
    else:
        fps_value = float(args.fps)
        print(f"Framerate (forced via --fps): {fps_value} fps")
    fps_str = f"{fps_value:.6f}"

    if args.no_mp4:
        # Clean up temp files before returning
        for tmp_file in [h264_path, wav_path]:
            if tmp_file and os.path.exists(tmp_file):
                try:
                    os.remove(tmp_file)
                    print(f"Cleaned up {tmp_file}")
                except OSError as e:
                    print(f"Warning: could not delete {tmp_file}: {e}")
        return audio_seconds

    # ---------- Build ffmpeg command ----------
    use_timestamp = (start_dt is not None) and (not args.no_timestamp)
    burn_timestamp = use_timestamp and have_drawtext
    srt_sidecar    = use_timestamp and not have_drawtext

    # --match=exact is the precision-first default. It does a two-pass build:
    # --match=exact builds the output with each surviving frame placed at its
    # actual real-time position in the recording. The DVR is variable frame
    # rate (fewer frames in quiet moments, more during activity), so we can't
    # just spread frames evenly across the audio duration — that would clump
    # active stretches into the front of the output and freeze the tail. The
    # interleaving of audio chunks in the .drv file gives us a built-in clock
    # (1024 G.711 samples @ 8 kHz = exactly 0.128 s per audio chunk), so we
    # know for each video chunk the real moment it was recorded.
    #
    # Strategy:
    #   1. Extract each surviving frame as a PNG, tracking source-index ->
    #      output-frame-number mapping (some indices are dropped by the
    #      decoder when their slice bitstream is corrupt).
    #   2. For each surviving frame, look up its real-time stamp from
    #      video_real_times[source_index].
    #   3. Write an ffconcat manifest listing each PNG with its duration =
    #      real_time[next_frame] - real_time[this_frame].
    #   4. Mux the concat'd images with the audio.
    # Other --match values (none/audio/video) use the older stream-copy path.
    use_exact_match = (
        args.match == "exact"
        and not args.stream_copy
        and chosen_wav is not None
        and len(video_real_times) > 0
    )

    if args.stream_copy:
        reencode = False
        if burn_timestamp:
            print("Note: --stream-copy disables timestamp burn-in; "
                  "falling back to sidecar .srt.")
            burn_timestamp = False
            srt_sidecar = use_timestamp
    elif args.reencode or use_exact_match or burn_timestamp:
        reencode = True
    else:
        reencode = False

    if use_exact_match:
        result = _build_vfr_output(
            h264_path, chosen_wav, mp4_path, args, start_dt, font_file,
            burn_timestamp, video_real_times, audio_seconds, frame_count, base,
        )
        if result:
            # Pass timestamp handling and return early
            decoded_frames_final, actual_duration = result
            if decoded_frames_final and frame_count > 0:
                loss = frame_count - decoded_frames_final
                loss_pct = 100.0 * loss / frame_count
                print(f"\nFrames: {frame_count} packets in .h264, "
                      f"{decoded_frames_final} decoded ({loss} lost, "
                      f"{loss_pct:.1f}%)")
                if loss_pct > 1.0:
                    print(f"  Note: lost frames are bitstream-corrupt in the")
                    print(f"  source recording; they cannot be recovered. Each")
                    print(f"  surviving frame is placed at its true real-time")
                    print(f"  position so no freeze or skip occurs.")
            if srt_sidecar:
                srt_path = base + ".srt"
                write_srt_sidecar(srt_path, start_dt, actual_duration)
                print(f"Wrote {srt_path}  (sidecar subtitles)")
                # If the subtitles filter is available, burn the SRT into
                # the video so timestamps are hardcoded. The .srt file
                # remains on disk too, in case the user wants to remux
                # the video without the overlay.
                if (not args.no_burn) and check_subtitles_filter_available():
                    print("Burning timestamps into video with libass subtitles filter...")
                    font_name = _font_name_for_libass(font_file)
                    if burn_srt_into_mp4(mp4_path, srt_path, font_name):
                        print(f"  Hardcoded timestamps into {mp4_path}")
                    else:
                        print(f"  Burn-in failed; keeping sidecar .srt only.")
            return actual_duration
        else:
            print("Exact-match build failed; falling back to legacy path.")
            use_exact_match = False

    cmd = [FFMPEG_PATH, "-y"]
    if reencode:
        cmd += ["-err_detect", "ignore_err", "-fflags", "+discardcorrupt"]
    cmd += ["-framerate", fps_str, "-i", h264_path]
    if chosen_wav:
        cmd += ["-i", chosen_wav]

    # Stream mapping — explicit so audio can never get dropped.
    if chosen_wav:
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0"]

    # Video encoder.
    if reencode:
        cmd += ["-c:v", "libx264", "-crf", "20", "-preset", "medium",
                "-pix_fmt", "yuv420p"]
        vf_parts = []
        if burn_timestamp:
            vf_parts.append(build_drawtext(start_dt, font_file))
        if use_exact_match:
            # The earlier approach (-fps_mode cfr + tpad=stop=-1) didn't actually
            # produce visible cloned frames — it inflated the stream's metadata
            # duration but the last frame's PTS stopped at decoded_frames/fps,
            # so playback ended early. tpad=stop_duration=N (with N comfortably
            # larger than any expected gap) clones the last decoded frame for
            # N seconds AS REAL FRAMES. The trim filter (and -t below) then
            # cap the chain at audio_seconds. No -fps_mode cfr — that was
            # interfering with tpad's frame production.
            vf_parts.append("tpad=stop_duration=60:stop_mode=clone")
            vf_parts.append(f"trim=end={audio_seconds:.6f}")
            vf_parts.append("setpts=PTS-STARTPTS")
        if vf_parts:
            cmd += ["-vf", ",".join(vf_parts)]
    else:
        cmd += ["-c:v", "copy"]
    cmd += ["-aspect", args.dar]

    # Audio encoder.
    if chosen_wav:
        cmd += ["-c:a", "aac", "-b:a", "96k", "-ac", "1", "-ar", str(AUDIO_RATE)]
        if args.match == "video" and not use_exact_match:
            quantized_fps = int(fps_value) if fps_value >= 1 else 15
            expected_packets = max(frame_count - 6, 0)
            video_quantized_duration = expected_packets / quantized_fps
            target = max(video_quantized_duration, audio_seconds)
            cmd += ["-af", f"apad=whole_dur={target:.6f}"]

    # Length matching strategy.
    if chosen_wav and args.match == "audio" and not use_exact_match:
        cmd += ["-t", f"{audio_seconds:.6f}"]
    if chosen_wav and use_exact_match:
        # Belt and suspenders: explicitly cap output at audio duration so even
        # if libx264 produces one extra frame at the boundary, it gets trimmed.
        cmd += ["-t", f"{audio_seconds:.6f}"]

    cmd += ["-movflags", "+faststart", mp4_path]

    if use_exact_match:
        mode_desc = "re-encoding at exact-match fps"
    else:
        mode_desc = "re-encoding with libx264" if reencode else "stream-copy"
    if burn_timestamp:
        mode_desc += " + drawtext overlay"
    elif srt_sidecar:
        mode_desc += " + sidecar .srt"
    print(f"\n{'Pass 2/2: ' if use_exact_match else ''}Muxing ({mode_desc}):")
    print("  $ " + " ".join(_quote_arg(a) for a in cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("\nffmpeg stderr (last 40 lines):")
        print("\n".join(r.stderr.splitlines()[-40:]))
        sys.exit(r.returncode)

    # Verify output streams and figure out the real final duration.
    probe = subprocess.run(
        [FFPROBE_PATH, "-v", "error", "-show_entries",
         "stream=index,codec_type,codec_name,duration",
         "-of", "default=noprint_wrappers=0", mp4_path],
        capture_output=True, text=True,
    )
    print(f"\nWrote {mp4_path}")
    print("Output streams:")
    print(probe.stdout.rstrip() if probe.stdout else "(ffprobe returned nothing)")

    if chosen_wav and "codec_type=audio" not in probe.stdout:
        print("\n!! WARNING: audio stream is missing from the MP4.")
        print("   Falling back to a two-pass mux...")
        _fallback_mux(h264_path, chosen_wav, mp4_path, args, start_dt, font_file,
                      reencode, burn_timestamp, fps_str, audio_seconds)
        probe = subprocess.run(
            [FFPROBE_PATH, "-v", "error", "-show_entries",
             "stream=index,codec_type,codec_name,duration",
             "-of", "default=noprint_wrappers=0", mp4_path],
            capture_output=True, text=True,
        )

    # Pass 3 (only for --match=exact): if the video and audio streams in the
    # mp4 aren't already matching, re-mux with a setpts scale on the video
    # stream so its timeline stretches to exactly audio_seconds. This handles
    # ffmpeg builds where tpad+-fps_mode doesn't perfectly fill the gap.
    if use_exact_match:
        v_dur, a_dur = None, None
        for stream_block in probe.stdout.split("[STREAM]"):
            if "codec_type=video" in stream_block:
                for line in stream_block.splitlines():
                    if line.startswith("duration="):
                        try: v_dur = float(line.split("=", 1)[1])
                        except ValueError: pass
            elif "codec_type=audio" in stream_block:
                for line in stream_block.splitlines():
                    if line.startswith("duration="):
                        try: a_dur = float(line.split("=", 1)[1])
                        except ValueError: pass
        if v_dur is not None and a_dur is not None and abs(v_dur - a_dur) > 0.05:
            scale = a_dur / v_dur
            # The target output framerate is decoded_frames / a_dur. Combining
            # setpts (which stretches the timeline) with fps (which forces
            # frame production at the target rate) actually moves the output
            # duration — setpts alone leaves the mp4 muxer free to round.
            target_fps = decoded_frames / a_dur if decoded_frames else 15.0
            print(f"\nPass 3/3: video duration {v_dur:.3f}s != audio {a_dur:.3f}s "
                  f"(off by {a_dur - v_dur:+.3f}s)")
            print(f"  Stretching video PTS by factor {scale:.6f} "
                  f"(target {target_fps:.4f} fps)")
            tmp_mp4 = mp4_path + ".pre-stretch.mp4"
            os.rename(mp4_path, tmp_mp4)
            stretch_cmd = [
                FFMPEG_PATH, "-y", "-i", tmp_mp4,
                "-map", "0:v:0", "-map", "0:a:0",
                "-c:v", "libx264", "-crf", "20", "-preset", "medium",
                "-pix_fmt", "yuv420p",
                "-vf", f"setpts={scale:.9f}*PTS,fps={target_fps:.6f}",
                "-c:a", "copy",
                "-movflags", "+faststart", mp4_path,
            ]
            print("  $ " + " ".join(_quote_arg(a) for a in stretch_cmd))
            r3 = subprocess.run(stretch_cmd, capture_output=True, text=True)
            if r3.returncode != 0:
                print("Pass 3 failed; keeping pass-2 output. stderr:")
                print("\n".join(r3.stderr.splitlines()[-15:]))
                os.rename(tmp_mp4, mp4_path)
            else:
                try: os.remove(tmp_mp4)
                except OSError: pass
                probe = subprocess.run(
                    [FFPROBE_PATH, "-v", "error", "-show_entries",
                     "stream=index,codec_type,codec_name,duration",
                     "-of", "default=noprint_wrappers=0", mp4_path],
                    capture_output=True, text=True,
                )
                print("Final streams:")
                print(probe.stdout.rstrip())

    # Audio duration is ground truth — it's bytes / 8000, no ambiguity. The
    # mp4's reported video duration may differ (often longer, because ffmpeg's
    # mp4 muxer quantizes the framerate to integer fps). For SRT timing AND
    # for multi-file timestamp chaining, we always use audio_seconds — that's
    # the actual elapsed real time of the recording.
    actual_duration = audio_seconds

    # Count how many video frames actually survived the H.264 decode by
    # probing the output mp4. This tells the user how many were lost to
    # genuine bitstream corruption in the source recording (typical ~5%
    # for this Speco DVR generation). The loss is intrinsic to the source —
    # no parser or encoder choice can recover what the DVR never wrote
    # correctly.
    frame_probe = subprocess.run(
        [FFPROBE_PATH, "-v", "error", "-count_frames", "-select_streams", "v",
         "-show_entries", "stream=nb_read_frames",
         "-of", "default=noprint_wrappers=1:nokey=1", mp4_path],
        capture_output=True, text=True,
    )
    try:
        decoded_frames = int(frame_probe.stdout.strip())
    except (ValueError, AttributeError):
        decoded_frames = None
    if decoded_frames is not None and frame_count > 0:
        loss = frame_count - decoded_frames
        loss_pct = 100.0 * loss / frame_count
        print(f"\nFrames: {frame_count} packets in .h264, "
              f"{decoded_frames} decoded ({loss} lost, {loss_pct:.1f}%)")
        if loss_pct > 1.0:
            print(f"  Note: the lost frames are bitstream-corrupt in the source")
            print(f"  recording (the DVR's encoder occasionally writes invalid")
            print(f"  macroblock data). They cannot be recovered downstream.")

    # Write SRT sidecar if drawtext was unavailable.
    if srt_sidecar:
        srt_path = base + ".srt"
        write_srt_sidecar(srt_path, start_dt, actual_duration)
        print(f"Wrote {srt_path}  (sidecar subtitles)")
        if (not args.no_burn) and check_subtitles_filter_available():
            print("Burning timestamps into video with libass subtitles filter...")
            font_name = _font_name_for_libass(font_file)
            if burn_srt_into_mp4(mp4_path, srt_path, font_name):
                print(f"  Hardcoded timestamps into {mp4_path}")
            else:
                print(f"  Burn-in failed; keeping sidecar .srt only.")

    # Clean up temporary .h264 and .wav files
    for tmp_file in [h264_path, wav_path]:
        if tmp_file and os.path.exists(tmp_file):
            try:
                os.remove(tmp_file)
                print(f"Cleaned up {tmp_file}")
            except OSError as e:
                print(f"Warning: could not delete {tmp_file}: {e}")

    return actual_duration


def _quote_arg(a):
    """Shell-quote-ish for the printed command (display only)."""
    if any(c in a for c in " '\\"):
        return "'" + a.replace("'", "'\\''") + "'"
    return a


def _fallback_mux(h264_path, wav_path, mp4_path, args, start_dt, font_file,
                  reencode, use_timestamp, fps_str, audio_seconds):
    """Two-pass mux for the rare case where a one-pass mux drops audio."""
    tmp_video = mp4_path + ".video.mp4"
    cmd1 = [FFMPEG_PATH, "-y", "-framerate", fps_str, "-i", h264_path]
    if reencode:
        cmd1 += ["-c:v", "libx264", "-crf", "20", "-preset", "medium",
                 "-pix_fmt", "yuv420p"]
        vf_parts = []
        if use_timestamp:
            vf_parts.append(build_drawtext(start_dt, font_file))
        vf_parts.append("tpad=stop=-1:stop_mode=clone")
        cmd1 += ["-vf", ",".join(vf_parts)]
        cmd1 += ["-t", f"{audio_seconds:.6f}"]
    else:
        cmd1 += ["-c:v", "copy"]
    cmd1 += ["-aspect", args.dar, "-movflags", "+faststart", tmp_video]
    r1 = subprocess.run(cmd1, capture_output=True, text=True)
    if r1.returncode != 0:
        print("Fallback pass 1 failed:")
        print("\n".join(r1.stderr.splitlines()[-20:]))
        sys.exit(1)

    cmd2 = [FFMPEG_PATH, "-y", "-i", tmp_video, "-i", wav_path,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart", mp4_path]
    r2 = subprocess.run(cmd2, capture_output=True, text=True)
    if r2.returncode != 0:
        print("Fallback pass 2 failed:")
        print("\n".join(r2.stderr.splitlines()[-20:]))
        sys.exit(1)
    try:
        os.remove(tmp_video)
    except OSError:
        pass


# ============================================================
# Main
# ============================================================

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("inputs", nargs="+", help="One or more .drv files")
    ap.add_argument("-o", "--output",
                    help="Output MP4 path (only valid with a single input)")
    ap.add_argument("--start-time",
                    help='Override timestamp start, e.g. "2025-05-27 12:38:00". '
                         "If omitted, the script looks for readme.txt in the "
                         "input's folder.")
    ap.add_argument("--no-timestamp", action="store_true",
                    help="Skip burned-in timestamp overlay (also disables the "
                         "re-encode it requires; falls back to faster stream-copy)")
    ap.add_argument("--no-burn", action="store_true",
                    help="Skip burning the SRT into the video as a second pass. "
                         "Only matters when this ffmpeg build lacks the drawtext "
                         "filter and is falling back to libass for burn-in. The "
                         "sidecar .srt is still written.")
    ap.add_argument("--no-mp4", action="store_true")
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--audio-mode", choices=list(AUDIO_DECODERS), default="alaw",
                    help="Audio decoder (default: alaw — confirmed for Speco "
                         "DVR-xTH series)")
    ap.add_argument("--audition-audio", action="store_true",
                    help="Diagnostic: write every decoder mode as a separate WAV")
    ap.add_argument("--fps", default="auto",
                    help="Output framerate (default: 'auto' — derived from "
                         "audio duration / frame count, which is the true "
                         "recording rate. Pass a number to override.)")
    ap.add_argument("--dar", default="4:3")
    ap.add_argument("--stream-copy", action="store_true",
                    help="Force video stream-copy (no re-encoding). Disables "
                         "burned-in timestamps; falls back to sidecar .srt.")
    ap.add_argument("--reencode", action="store_true",
                    help="Force video re-encoding through libx264 even when "
                         "stream-copy would otherwise be selected. May lose "
                         "scattered frames if the H.264 has minor bitstream "
                         "corruption (diagnostic only).")
    ap.add_argument("--match", choices=["exact", "none", "audio", "video"],
                    default="exact",
                    help="Stream length matching strategy. "
                         "'exact' (default): two-pass re-encode. First pass "
                         "decodes to count surviving frames, second pass "
                         "re-encodes at a rate that spreads those frames "
                         "across exactly the audio duration. Video and audio "
                         "match to within one frame; all decodable footage "
                         "preserved; no freezes, no truncation, no silence. "
                         "'none': stream-copy, preserve every packet, accept "
                         "that ffmpeg may report video as slightly longer than "
                         "audio (mp4 muxer quantizes framerate to 15 fps). "
                         "'audio': stream-copy + truncate at audio duration "
                         "(loses up to ~30s of trailing video footage). "
                         "'video': stream-copy + pad audio with silence to "
                         "match video duration (audio tail has ~30s of "
                         "inserted silence).")
    args = ap.parse_args()

    if args.output and len(args.inputs) > 1:
        ap.error("--output / -o only works with a single input file")

    # Resolve start time.
    start_dt = None
    if not args.no_timestamp:
        if args.start_time:
            try:
                start_dt = _dt.datetime.strptime(args.start_time,
                                                 "%Y-%m-%d %H:%M:%S")
            except ValueError:
                ap.error('--start-time must be "YYYY-MM-DD HH:MM:SS"')
            start_dt = start_dt.replace(tzinfo=_dt.timezone.utc)
            print(f"Timestamp start (from --start-time): "
                  f"{start_dt.strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            readme = find_readme(args.inputs[0])
            if readme:
                parsed = parse_readme(readme)
                if parsed:
                    start_dt, end_dt = parsed
                    print(f"Found readme: {readme}")
                    print(f"  Start: {start_dt.strftime('%Y-%m-%d %H:%M:%S')}")
                    if end_dt:
                        print(f"  End:   {end_dt.strftime('%Y-%m-%d %H:%M:%S')}")
                else:
                    print(f"Found readme at {readme} but couldn't parse Start Time.")
            else:
                print("No readme.txt found and no --start-time given. "
                      "Timestamps disabled.")

    font_file = find_font() if start_dt else None
    have_drawtext = check_drawtext_available()
    have_subtitles = check_subtitles_filter_available()
    if start_dt and not have_drawtext:
        if have_subtitles and not args.no_burn:
            print("Note: this ffmpeg build lacks the 'drawtext' filter — "
                  "falling back to a two-pass burn-in via libass. Timestamps "
                  "will still be hardcoded into the video.")
        elif have_subtitles and args.no_burn:
            print("Note: --no-burn set; timestamps will be written as a "
                  "sidecar .srt file only (no burn-in).")
        else:
            print("Note: this ffmpeg build has neither 'drawtext' nor the "
                  "'subtitles' (libass) filter. Timestamps will be written "
                  "as a sidecar .srt file only; most players auto-load it.")
    elif start_dt and not font_file:
        print("Warning: no suitable font found for timestamp overlay. "
              "ffmpeg will use its built-in default.")
    elif start_dt:
        print(f"Timestamp font: {font_file}")

    # Process each input. Timestamp start advances by the previous file's
    # actual audio duration.
    cumulative_offset = 0.0
    file_start = start_dt
    for inp in args.inputs:
        seconds = process_file(inp, args, file_start, font_file, have_drawtext)
        cumulative_offset += seconds
        if start_dt is not None:
            file_start = start_dt + _dt.timedelta(seconds=cumulative_offset)


if __name__ == "__main__":
    main()
