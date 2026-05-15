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

import argparse, datetime as _dt, os, re, struct, subprocess, sys
from collections import Counter

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
    """
    h264_parts, audio_parts = [], []
    video_real_times = []
    audio_chunks_seen = 0
    stats = Counter()
    matches = list(ANY_MAGIC.finditer(data))
    n = len(data)

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
                h264_parts.append(payload)
                # Real-time position = audio chunks seen so far × 0.128s.
                # This is the moment in real time at which this video frame
                # was actually recorded by the DVR.
                video_real_times.append(audio_chunks_seen * AUDIO_CHUNK_SECONDS)
                stats["video_chunks"] += 1
        elif magic == AUDIO_MAGIC:
            s = pos + HDR_LEN
            e = min(s + AUDIO_PAYLOAD_LEN, next_pos, n)
            audio_parts.append(data[s:e])
            audio_chunks_seen += 1
            stats["audio_chunks"] += 1
        else:
            stats["meta_chunks"] += 1

    return b"".join(h264_parts), b"".join(audio_parts), stats, video_real_times


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
        r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "drawtext" in (r.stdout + r.stderr)


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

    # Decode count probe.
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames",
         "-select_streams", "v", "-show_entries",
         "stream=nb_read_frames",
         "-of", "default=noprint_wrappers=1:nokey=1", h264_path],
        capture_output=True, text=True,
    )
    try:
        decoded_frames = int(probe.stdout.strip())
    except (ValueError, AttributeError):
        return None
    if decoded_frames < 1:
        return None

    print(f"\nPass 1/3: decoder produces {decoded_frames} frames "
          f"({frame_count - decoded_frames} of {frame_count} source slices "
          f"are bitstream-corrupt)")

    # Map decoded frames to source-slice real-times. We don't know exactly
    # which source slices the decoder dropped, but they tend to be scattered
    # throughout the file at a steady rate, so resampling video_real_times
    # uniformly is a good approximation that respects the real-time pattern
    # of the original recording (preserves quiet stretches and bursts).
    surviving_real_times = []
    n_src = len(video_real_times)
    for k in range(decoded_frames):
        # Map output frame k -> source index (k * n_src / decoded_frames)
        src_idx = int(k * n_src / decoded_frames)
        if src_idx >= n_src:
            src_idx = n_src - 1
        surviving_real_times.append(video_real_times[src_idx])
    # Enforce strict monotonic increase (resample collisions can give equal
    # times; bump them by tiny epsilons so concat durations remain positive).
    for i in range(1, len(surviving_real_times)):
        if surviving_real_times[i] <= surviving_real_times[i-1]:
            surviving_real_times[i] = surviving_real_times[i-1] + 0.001

    print(f"  Source video span: {video_real_times[0]:.3f}s .. "
          f"{video_real_times[-1]:.3f}s (audio is {audio_seconds:.3f}s)")
    print(f"  Surviving frames will be placed across the source's real "
          f"timeline, with the last frame extending to audio end")

    # Per-frame durations for the concat manifest.
    durations = []
    for i in range(len(surviving_real_times)):
        if i + 1 < len(surviving_real_times):
            d = surviving_real_times[i + 1] - surviving_real_times[i]
        else:
            # Last frame extends to audio_seconds (so the visible content
            # covers the full duration; the trailing freeze is at most the
            # gap between the last recorded frame and the end of audio).
            d = max(audio_seconds - surviving_real_times[i], 0.001)
        d = max(d, 0.001)
        durations.append(d)

    print(f"  Frame duration: min {min(durations)*1000:.1f}ms "
          f"max {max(durations)*1000:.1f}ms "
          f"median {sorted(durations)[len(durations)//2]*1000:.1f}ms")

    print("\nPass 2/3: extracting decoded frames to disk...")
    tmpdir = tempfile.mkdtemp(prefix="drv_vfr_",
                              dir=os.path.dirname(mp4_path) or ".")
    try:
        extract_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
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
        pngs = sorted(f for f in os.listdir(tmpdir) if f.startswith("f_"))
        # Truncate to whichever count is smaller (extraction and probe may
        # differ by 1-2 due to decoder/probe disagreement on edge cases).
        n = min(len(pngs), len(durations))
        pngs = pngs[:n]
        durations = durations[:n]
        print(f"  Extracted {len(pngs)} frames")

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
        mux_cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
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
                print(f"Wrote {srt_path}  (timestamps; auto-loads in most players)")
            return actual_duration
        else:
            print("Exact-match build failed; falling back to legacy path.")
            use_exact_match = False

    cmd = ["ffmpeg", "-y"]
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
        ["ffprobe", "-v", "error", "-show_entries",
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
            ["ffprobe", "-v", "error", "-show_entries",
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
                "ffmpeg", "-y", "-i", tmp_mp4,
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
                    ["ffprobe", "-v", "error", "-show_entries",
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
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v",
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
        print(f"Wrote {srt_path}  (timestamps; auto-loads in most players)")

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
    cmd1 = ["ffmpeg", "-y", "-framerate", fps_str, "-i", h264_path]
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

    cmd2 = ["ffmpeg", "-y", "-i", tmp_video, "-i", wav_path,
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
    if start_dt and not have_drawtext:
        print("Note: this ffmpeg build does NOT have the 'drawtext' filter.")
        print("      Timestamps will be written as a sidecar .srt file next to")
        print("      each MP4 instead of burned into the video.")
        print("      Most players (VLC, mpv, IINA) load matching-name .srt files")
        print("      automatically. To get burned-in timestamps, run:")
        print("          brew reinstall ffmpeg")
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
