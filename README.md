# Speco DRV Extractor

A robust Python tool to extract and convert Speco DVR `.drv` video files to MP4 with proper A/V synchronization, timestamp recovery, and audio decoding.

## Overview

Speco DVRs record video and audio in a proprietary binary container format (`.drv`) with:
- **H.264 Annex-B video** at variable framerate (~14–15 fps, not fixed)
- **G.711 A-law audio** at 8 kHz, mono (or µ-law/PCM variants on some models)
- **Metadata chunks** containing channel index markers

The DVR doesn't record cleanly: frames are often dropped or duplicated, and the muxer quantizes framerates, leading to A/V drift. This tool recovers the true recording framerate from the audio clock (which operates at a fixed 8 kHz cadence) and uses it to correct video duration and A/V sync.

## Features

- **Multi-file batch processing** — Process multiple `.drv` files in one command; timestamps chain correctly across files
- **Accurate A/V sync** — Derives true framerate from audio duration and pads video to match exactly (within one frame)
- **Timestamp recovery** — Burns DVR-style timestamps (bottom-right corner) sourced from:
  - Auto-detected `readme.txt` in the same folder (Speco standard), or
  - Manual `--start-time` argument
- **Flexible audio decoding** — Supports A-law, µ-law, PCM unsigned, PCM signed (auto-detects or override)
- **Audio standardization** — Decodes raw G.711 to PCM, then re-encodes to **AAC 96 kbps** for broad compatibility
- **Frame loss reporting** — Counts H.264 bitstream corruption (intrinsic to the source; no recovery possible)
- **Fallback rendering** — If ffmpeg lacks the `drawtext` filter, falls back to sidecar `.srt` subtitle files (auto-loaded in VLC, mpv, IINA)

## Installation

### Requirements
- **Python 3.8+**
- **ffmpeg** with H.264/H.265 support
- **ffprobe** (ships with ffmpeg)

### Setup

```bash
# Clone the repo
git clone https://github.com/yourusername/speco-drv-extractor.git
cd speco-drv-extractor

# Install ffmpeg (if not already present)
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt-get install ffmpeg

# Windows (via Chocolatey or download from ffmpeg.org)
choco install ffmpeg
```

## Usage

### Command Line

```bash
# Basic — auto-finds readme.txt in the same folder
python3 drv_extract_v7.py 1.drv

# Batch process multiple files with chained timestamps
python3 drv_extract_v7.py "[000001].drv" "[000002].drv" "[000003].drv"

# Manual timestamp start
python3 drv_extract_v7.py 1.drv --start-time "2025-05-27 12:38:00"

# Skip timestamp overlay (faster; no re-encode needed)
python3 drv_extract_v7.py 1.drv --no-timestamp

# Extract raw .h264/.wav only (no MP4 muxing)
python3 drv_extract_v7.py 1.drv --no-mp4

# Force stream-copy mode (faster, may lose A/V sync slightly)
python3 drv_extract_v7.py 1.drv --stream-copy

# Try a different audio decoder
python3 drv_extract_v7.py 1.drv --audio-mode mulaw

# Force a specific output framerate
python3 drv_extract_v7.py 1.drv --fps 30

# Specify output path
python3 drv_extract_v7.py 1.drv -o output.mp4

# See all options
python3 drv_extract_v7.py --help
```

### GUI (macOS / Windows)

A lightweight tkinter GUI is provided for drag-and-drop operation:

```bash
python3 drv_gui.py
```

Or use the pre-built macOS app (see **Building the App** below).

## What It Does

### 1. **Parse the .drv Container**
Walks the binary stream as a sequence of typed chunks:
- `00 00 00 01 f0` — VIDEO chunk (H.264 Annex-B payload)
- `40 36 00 10` — AUDIO chunk (~1024 bytes G.711)
- `40 36 00 00` — Metadata/channel-index marker

### 2. **Recover True Framerate**
Audio chunks arrive at a perfectly fixed cadence: 1024 samples of G.711 at 8 kHz = 0.128 seconds per chunk. By counting audio chunks, the script knows the exact wall-clock time at which each video frame was recorded. This allows recovery of the true (variable) framerate of the DVR, which is typically 14–15 fps, not exactly 15.

### 3. **Decode Audio**
Converts raw G.711 (A-law, µ-law, or PCM variants) to 16-bit signed PCM at 8 kHz, mono, then re-encodes to **AAC 96 kbps** for the final MP4 (widely supported, standardized codec).

### 4. **Correct A/V Sync**
The DVR doesn't record at a fixed framerate, and frames are often dropped or duplicated. The script:
- Counts decodable video frames in the H.264 bitstream
- Calculates the target framerate: `decoded_frames / audio_duration`
- Re-encodes video (or stretches PTS) to match audio duration exactly

Result: no stuttering, no silence, no freezes — video and audio stay in sync to within one frame.

### 5. **Burn Timestamps**
If ffmpeg has the `drawtext` filter, burns a DVR-style timestamp (bottom-right corner) into the video. The timestamp is sourced from:
- **readme.txt** (Speco standard, auto-detected in the same folder), or
- **--start-time** (manual override)

If `drawtext` is unavailable, writes a sidecar `.srt` file instead (auto-loaded in most players).

### 6. **Mux to MP4**
Packages the corrected H.264 video and AAC audio into an MP4 container with correct metadata, duration, and timescale.

## Output

For each input `.drv`:
- **`[name].mp4`** — H.264 video + AAC audio, 96 kbps, 8 kHz mono, with burned-in or sidecar timestamp
- **`[name].srt`** (optional) — Subtitle file if ffmpeg lacks `drawtext` filter
- **`[name].h264`** and **`[name].wav`** (temporary, cleaned up)

## Options Reference

| Option | Default | Description |
|--------|---------|-------------|
| `--start-time STR` | auto-detect | Override timestamp start, format: `"YYYY-MM-DD HH:MM:SS"`. If omitted, looks for `readme.txt`. |
| `--no-timestamp` | — | Skip timestamp overlay; disables re-encode, falls back to stream-copy + optional SRT. |
| `--no-mp4` | — | Extract `.h264` and `.wav` only; skip muxing to MP4. |
| `--no-audio` | — | Extract video only; skip audio decoding and muxing. |
| `--audio-mode MODE` | `alaw` | Audio decoder: `alaw`, `mulaw`, `pcm8u`, `pcm8s`. Default is correct for Speco DVR-xTH series. |
| `--audition-audio` | — | Diagnostic: write every decoder mode as a separate WAV for testing. |
| `--fps RATE` | `auto` | Output framerate. Default (`auto`) derives from `audio_duration / frame_count`. |
| `--dar RATIO` | `4:3` | Display aspect ratio for the MP4 (e.g., `16:9`, `16:10`). |
| `--stream-copy` | — | Force H.264 stream-copy (no re-encode). Faster but may lose A/V sync slightly. Disables burned-in timestamps; falls back to SRT. |
| `--reencode` | — | Force video re-encoding through libx264 even when stream-copy would be selected. Use for diagnostic purposes. |
| `--match STRATEGY` | `exact` | Stream length matching: `exact` (2-pass re-encode, all frames preserved), `none` (stream-copy), `audio` (truncate at audio), `video` (pad audio with silence). |
| `-o, --output PATH` | — | Output MP4 path (only valid with a single input). If omitted, derives from input filename. |

## Troubleshooting

### Audio sounds garbled or distorted
- Your DVR may use a different audio codec than A-law. Try:
  ```bash
  python3 drv_extract_v7.py file.drv --audio-mode mulaw
  python3 drv_extract_v7.py file.drv --audio-mode pcm8u
  python3 drv_extract_v7.py file.drv --audio-mode pcm8s
  ```
- If one of these works, use `--audition-audio` to write all modes as separate WAVs for comparison.

### No timestamp overlay on video
- Your ffmpeg build lacks the `drawtext` filter (common on some stripped builds).
- Check: `ffmpeg -filters | grep drawtext`
- Workaround: The script falls back to a sidecar `.srt` file (auto-loaded in VLC, mpv, IINA).
- To rebuild ffmpeg with `drawtext`: `brew reinstall ffmpeg` (macOS) or rebuild from source.

### Video and audio out of sync
- Use the default settings (the script automatically corrects A/V sync).
- If sync is still off, try `--stream-copy` (faster, but may drift slightly).
- If that doesn't help, your `.drv` file may have intrinsic corruption; no tool can recover it.

### Frame loss reported (e.g., "lost, 5.2%")
- This is **normal** for Speco DVR-xTH series. The DVR's H.264 encoder occasionally writes invalid macroblock data.
- These frames are lost intrinsically in the source recording; the parser cannot recover them.
- The script still preserves all decodable frames with correct A/V sync.

### FFmpeg reports different video/audio duration in the output MP4
- The script uses **audio duration as ground truth** (bytes / 8000 Hz = exact elapsed time).
- FFmpeg's MP4 muxer may quantize the framerate to integer fps, which can cause its reporting to differ slightly.
- The video will play correctly (no stuttering or silence); the metadata difference is cosmetic.

## Building the GUI App

### macOS

Build a standalone `.app` that doesn't require Python or Terminal:

```bash
# Install PyInstaller (in a virtual environment)
python3 -m venv venv
source venv/bin/activate
pip install PyInstaller

# Build the app
pyinstaller drv_extractor.spec

# Your app is at: dist/Speco DRV Extractor.app
```

Or use the provided build script:
```bash
chmod +x build.sh
./build.sh
```

To install in Applications folder:
```bash
cp -r "dist/Speco DRV Extractor.app" /Applications/
```

### Windows

Build a standalone `.exe` (GUI will open in a window):

```bash
# Install PyInstaller
pip install PyInstaller

# Create a spec file for Windows (modify drv_extractor.spec for Windows paths)
# Then build:
pyinstaller drv_extractor_windows.spec

# Your app is at: dist/Speco DRV Extractor.exe
```

**Note:** On Windows, you must have ffmpeg installed and in your PATH:
- Download from [ffmpeg.org](https://ffmpeg.org/download.html)
- Or use Chocolatey: `choco install ffmpeg`

## Script Architecture

### Key Functions

**`parse_drv(data)`**
- Parses the `.drv` binary container into H.264 chunks, audio chunks, and statistics.
- Returns: `(h264_bytes, audio_bytes, stats, video_real_times)`
- `video_real_times` lists the wall-clock position of each video frame, derived from audio chunk count.

**`count_nal_types(h264)`**
- Scans the H.264 bitstream for NAL units and counts by type.
- Diagnostic: identifies IDR frames, SEI messages, etc.

**`decode_alaw(audio)`, `decode_mulaw(audio)`, etc.**
- Converts raw G.711 variants to 16-bit signed PCM.
- Output: 8 kHz, mono, ready for WAV or re-encoding.

**`write_pcm_wav(pcm, path, sample_rate, channels, bits)`**
- Writes PCM data as a standard RIFF WAV file.
- Used as intermediate format before AAC re-encoding.

**`process_file(inp, args, file_start, font_file, have_drawtext)`**
- Main orchestration function.
- Parses the `.drv`, decodes audio, determines framerate, renders video, muxes to MP4.
- Handles multi-pass encoding if needed for A/V sync correction.
- Returns: actual audio duration (for multi-file chaining).

**`main()`**
- CLI argument parsing and orchestration.
- Calls `process_file()` for each input, advancing timestamp start by the previous file's audio duration.

### Constants

```python
AUDIO_RATE = 8000              # G.711 sample rate
AUDIO_PAYLOAD_LEN = 1024       # bytes per audio chunk
AUDIO_CHUNK_SECONDS = 0.128    # seconds per audio chunk
```

## Known Limitations

1. **Frame loss is intrinsic** — The Speco DVR's H.264 encoder occasionally writes invalid macroblock data. These frames cannot be recovered; the script reports how many are lost.

2. **Framerate is derived, not exact** — The DVR records at ~14–15 fps (varies), not exactly 15. The script derives the true framerate from audio duration, which is accurate.

3. **Timestamps are display-only** — If `drawtext` is unavailable, timestamps are written as `.srt` subtitles. This is a fallback; the actual MP4 duration and timescale are correct.

4. **Audio is re-encoded** — The script always re-encodes to AAC 96 kbps for compatibility. If you need lossless audio, use `--no-mp4` to extract the `.wav` and mux manually.

## Performance

On a modern CPU (e.g., M1 Mac, Intel i7):
- **Parsing & decoding**: ~1 minute per 1 GB of `.drv` data
- **Video re-encoding** (if timestamps enabled): ~2–5× real-time (depends on resolution and CPU)
- **Stream-copy mode** (no re-encode): ~30 seconds per 1 GB

For faster processing, use `--stream-copy` (trades A/V sync accuracy for speed).

## Contributing

This tool was born from reverse-engineering Speco DVR recordings and iterative refinement. If you encounter:
- Different DVR models or audio codecs
- Edge cases in timestamp parsing
- Unusual frame rates or aspect ratios

...please open an issue with:
- A small sample `.drv` file (or link to one)
- Your DVR model
- The output of `ffprobe` on the final MP4
- Any error messages from the script

## License

MIT License — feel free to use, modify, and distribute.

## References

- **Speco DVR-xTH series**: H.264 video @ variable fps, G.711 A-law audio @ 8 kHz
- **H.264 Annex-B**: NAL unit format used in `.drv` files
- **G.711 A-law & µ-law**: ITU-T G.711 speech codec (8-bit, 8 kHz standard)
- **RIFF WAV**: Waveform Audio File Format (PCM container)
- **MP4 / QuickTime**: ISO/IEC 14496-12 (muxing standard)

## Changelog

### v7 (Current)
- Multi-file batch processing with timestamp chaining
- A/V sync correction via framerate derivation and PTS stretching
- Flexible audio decoding (A-law, µ-law, PCM variants)
- Fallback to `.srt` subtitles if `drawtext` unavailable
- Frame loss reporting (diagnostic)
- Enhanced error handling and logging

### Earlier Versions
- v1–v6: Iterative refinement of parsing, audio decoding, and muxing logic

---

**Questions or issues?** Open a GitHub issue or contact the maintainer.
