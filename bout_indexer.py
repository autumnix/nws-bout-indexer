#!/usr/bin/env python3
"""
nws-bout-indexer: Generate YouTube chapter markers from roller derby bout footage.

Scans the scoreboard overlay in a bout video via OCR and produces a chapters file
with jam timestamps, lineups, period clocks, and timeout markers.

See 'Bout Post Processing Specs.txt' for the full specification.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PIL import Image, ImageOps

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Proportional crop regions (relative to video width/height)
# Right scoreboard: P/J, state banners, period clock, jam clock
RIGHT_CROP = {"x": 0.844, "y": 0.0, "w": 0.156, "h": 0.083}
# Left scoreboard: jammer + pivot names (past team name / score columns)
LEFT_CROP = {"x": 0.109, "y": 0.0, "w": 0.547, "h": 0.076}

# Output scale for OCR readability
RIGHT_SCALE = (800, 240)
LEFT_SCALE = (1400, 220)

# Common OCR mis-reads for period numbers
PERIOD_FIXES = {7: 1, 11: 1, 17: 1, 71: 1, 1386: 1, 18: 1, 22: 2}

# State detection keywords, checked in priority order
STATE_KEYWORDS = [
    ("HIGHLIGHT", ["HIGHLIGHT"]),
    ("OFFICIAL TIMEOUT", ["OFFICIAL TIMEOUT", "OFFICIALTIMEOUT", "SOFEIGIAL TIMEOUT"]),
    ("TEAM TIMEOUT", ["TEAM TIMEOUT", "TEAMTIMEOUT"]),
    ("POST TIMEOUT", ["POST TIMEOUT", "POSTTIMEOUT", "POST TIMEOU", "POST TRAEOU"]),
    ("TIMEOUT", ["TIMEOUT"]),
    ("LINEUP", ["LINEUP"]),
    ("COMING UP", ["COMING UP", "COMINGUP"]),
]


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def fmt_ts(seconds: int) -> str:
    """Format seconds as HH:MM:SS."""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def fmt_pc(clock_str: str) -> str:
    """Format period clock MM:SS as NNmNNs."""
    m = re.match(r"(\d+):(\d+)", clock_str)
    if m:
        return f"{m.group(1)}m{m.group(2)}s"
    return clock_str


def log(msg: str):
    """Print a status message."""
    print(f"  {msg}", flush=True)


# ---------------------------------------------------------------------------
# Phase 1: Video probing & frame extraction
# ---------------------------------------------------------------------------

def probe_video(video_path: str) -> dict:
    """Get video metadata via ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration",
        "-show_entries", "format=duration",
        "-of", "json",
        video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(result.stdout)

    stream = data.get("streams", [{}])[0]
    w = int(stream.get("width", 2560))
    h = int(stream.get("height", 1440))
    duration = float(
        stream.get("duration", 0) or data.get("format", {}).get("duration", 0)
    )
    return {"width": w, "height": h, "duration": duration}


def build_crop_filter(video_w: int, video_h: int, crop: dict, scale: tuple) -> str:
    """Build an ffmpeg crop+scale filter string from proportional crop spec."""
    cx = int(video_w * crop["x"])
    cy = int(video_h * crop["y"])
    cw = int(video_w * crop["w"])
    ch = int(video_h * crop["h"])
    sw, sh = scale
    return f"crop={cw}:{ch}:{cx}:{cy},scale={sw}:{sh}"


def extract_frames(video_path: str, ocr_dir: str, fps: float, video_info: dict):
    """Extract right and left scoreboard crops at the given fps."""
    w, h = video_info["width"], video_info["height"]

    right_dir = os.path.join(ocr_dir, "right")
    left_dir = os.path.join(ocr_dir, "left")
    os.makedirs(right_dir, exist_ok=True)
    os.makedirs(left_dir, exist_ok=True)

    right_filter = build_crop_filter(w, h, RIGHT_CROP, RIGHT_SCALE)
    left_filter = build_crop_filter(w, h, LEFT_CROP, LEFT_SCALE)

    # Add contrast boost for right scoreboard
    right_filter += ",eq=contrast=1.5:brightness=0.1"

    log(f"Extracting right scoreboard frames (fps={fps})...")
    subprocess.run(
        [
            "ffmpeg", "-i", video_path,
            "-vf", f"fps={fps},{right_filter}",
            "-q:v", "1",
            os.path.join(right_dir, "frame_%05d.png"),
        ],
        capture_output=True,
    )

    log(f"Extracting left scoreboard frames (fps={fps})...")
    subprocess.run(
        [
            "ffmpeg", "-i", video_path,
            "-vf", f"fps={fps},{left_filter}",
            "-q:v", "1",
            os.path.join(left_dir, "frame_%05d.png"),
        ],
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# Phase 2: OCR
# ---------------------------------------------------------------------------

def ocr_frame_plain(frame_path: str) -> str:
    """Run tesseract on a frame image, return raw text."""
    result = subprocess.run(
        ["tesseract", frame_path, "stdout", "--psm", "6"],
        capture_output=True, timeout=15,
    )
    text = result.stdout.decode("utf-8", errors="replace")
    return text.replace("\n", "~").strip()


def ocr_frame_threshold(frame_path: str) -> str:
    """Threshold the image to isolate text from the white scoreboard box, then OCR."""
    img = Image.open(frame_path).convert("L")
    thresh = img.point(lambda x: 255 if x > 180 else 0)
    thresh = ImageOps.invert(thresh)
    tmp_path = frame_path + ".thresh.png"
    thresh.save(tmp_path)
    result = subprocess.run(
        ["tesseract", tmp_path, "stdout", "--psm", "6"],
        capture_output=True, timeout=15,
    )
    os.unlink(tmp_path)
    text = result.stdout.decode("utf-8", errors="replace")
    return text.replace("\n", "~").strip()


def ocr_frame_names(frame_path: str) -> str:
    """OCR left scoreboard: crop past score area, threshold to isolate white text on dark bg."""
    img = Image.open(frame_path).convert("L")
    w, h = img.size
    # Crop past the score numbers area (~15% of width) to avoid noise
    crop_x = int(w * 0.15)
    cropped = img.crop((crop_x, 0, w, h))
    # Threshold: keep only bright pixels (white text on dark bg)
    thresh = cropped.point(lambda x: 255 if x > 160 else 0)
    tmp_path = frame_path + ".names.png"
    thresh.save(tmp_path)
    result = subprocess.run(
        ["tesseract", tmp_path, "stdout", "--psm", "6"],
        capture_output=True, timeout=15,
    )
    os.unlink(tmp_path)
    text = result.stdout.decode("utf-8", errors="replace")
    return text.replace("\n", "~").strip()


def run_ocr(ocr_dir: str, fps: float, progress_cb=None):
    """Run OCR on all extracted frames and save results."""
    right_dir = os.path.join(ocr_dir, "right")
    left_dir = os.path.join(ocr_dir, "left")

    frames = sorted(
        f for f in os.listdir(right_dir) if f.startswith("frame_") and f.endswith(".png")
    )

    total = len(frames)
    log(f"OCR pass on {total} right scoreboard frames (dual pass)...")
    pj_lines = []
    state_lines = []
    for i, fname in enumerate(frames):
        if (i + 1) % 100 == 0:
            log(f"  ...frame {i + 1}/{total}")
        if progress_cb:
            progress_cb("ocr_right", i + 1, total, f"OCR right scoreboard: {i + 1}/{total}")
        fpath = os.path.join(right_dir, fname)
        frame_num = int(fname.replace("frame_", "").replace(".png", ""))

        pj_text = ocr_frame_plain(fpath)
        state_text = ocr_frame_threshold(fpath)

        pj_lines.append(f"{frame_num:05d}|{pj_text}")
        state_lines.append(f"{frame_num:05d}|{state_text}")

    with open(os.path.join(ocr_dir, "right_pj_ocr.txt"), "w") as f:
        f.write("\n".join(pj_lines) + "\n")
    with open(os.path.join(ocr_dir, "right_state_ocr.txt"), "w") as f:
        f.write("\n".join(state_lines) + "\n")

    # Left scoreboard OCR
    left_frames = sorted(
        f for f in os.listdir(left_dir) if f.startswith("frame_") and f.endswith(".png")
    )
    total_left = len(left_frames)
    log(f"OCR pass on {total_left} left scoreboard frames...")
    left_lines = []
    for i, fname in enumerate(left_frames):
        if (i + 1) % 100 == 0:
            log(f"  ...frame {i + 1}/{total_left}")
        if progress_cb:
            progress_cb("ocr_left", i + 1, total_left, f"OCR left scoreboard: {i + 1}/{total_left}")
        fpath = os.path.join(left_dir, fname)
        frame_num = int(fname.replace("frame_", "").replace(".png", ""))
        text = ocr_frame_names(fpath)
        left_lines.append(f"{frame_num:05d}|{text}")

    with open(os.path.join(ocr_dir, "left_ocr.txt"), "w") as f:
        f.write("\n".join(left_lines) + "\n")


# ---------------------------------------------------------------------------
# Phase 3: Timeline construction
# ---------------------------------------------------------------------------

def fix_period(p: int) -> int:
    """Correct common OCR mis-reads of period numbers."""
    return PERIOD_FIXES.get(p, p)


def detect_state(text: str) -> Optional[str]:
    """Detect scoreboard state from OCR text."""
    upper = text.upper()
    for state_name, keywords in STATE_KEYWORDS:
        for kw in keywords:
            if kw in upper:
                return state_name
    return None


def normalize_pj_text(text: str) -> str:
    """Normalize common OCR substitutions for P/J detection."""
    # Common OCR confusions: l→1, I→1, O→0, S→5, T→7
    # Only apply in the context of P/J patterns
    normalized = text
    # Fix "Pl" -> "P1", "PI" -> "P1"
    normalized = re.sub(r"P[lI]", "P1", normalized)
    # Fix "Jl" -> "J1", "JI" -> "J1", "JT" -> "J1"
    normalized = re.sub(r"J[lIT](?=\s|$|[^a-zA-Z])", "J1", normalized)
    # Fix "Jn" patterns (n misread for a digit)
    normalized = re.sub(r"Jn(?=\s|$)", "J11", normalized)
    return normalized


def extract_pj(text: str):
    """Extract (period, jam) from OCR text, or (None, None)."""
    normalized = normalize_pj_text(text)
    m = re.search(r"P(\d+)\s+J(\d+)", normalized)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def build_timeline(ocr_dir: str, frame_interval: float) -> dict:
    """
    Build the chapter timeline from OCR data.

    Returns a dict with:
      - 'jams': list of jam dicts with seconds, period, jam number
      - 'chapters': list of chapter dicts (jams + timeouts) with timestamps
    """
    # --- Load OCR data ---
    pj_entries = []
    with open(os.path.join(ocr_dir, "right_pj_ocr.txt")) as f:
        for line in f:
            parts = line.strip().split("|", 1)
            if len(parts) < 2:
                continue
            frame = int(parts[0])
            p, j = extract_pj(parts[1])
            if p is not None:
                p = fix_period(p)
                if p in (1, 2):
                    pj_entries.append((frame, p, j, frame * frame_interval))

    state_entries = []
    with open(os.path.join(ocr_dir, "right_state_ocr.txt")) as f:
        for line in f:
            parts = line.strip().split("|", 1)
            if len(parts) < 2:
                continue
            frame = int(parts[0])
            state = detect_state(parts[1])
            if state:
                state_entries.append(
                    {"frame": frame, "state": state, "seconds": frame * frame_interval}
                )

    # --- Find jam transitions (forward-only) ---
    transitions = []
    cur_p, cur_j = 0, 0
    for frame, p, j, sec in pj_entries:
        if p > cur_p:
            cur_p = p
            cur_j = 0
        if p == cur_p:
            # Allow first jam of each period to start at any number
            if cur_j == 0 and j >= 1:
                cur_j = j
                transitions.append({"frame": frame, "p": p, "j": j, "seconds": int(sec)})
            elif j == cur_j + 1 and j < cur_j + 6:
                cur_j = j
                transitions.append({"frame": frame, "p": p, "j": j, "seconds": int(sec)})

    # --- Build chapter list ---
    chapters = []

    # For each jam, find the best LINEUP/POST TIMEOUT preceding it
    for i, jam in enumerate(transitions):
        prev_sec = transitions[i - 1]["seconds"] if i > 0 else 0
        jam_sec = jam["seconds"]

        best = None
        for st in state_entries:
            if st["state"] == "HIGHLIGHT":
                continue
            if prev_sec < st["seconds"] < jam_sec:
                if st["state"] in ("LINEUP", "POST TIMEOUT"):
                    if best is None or st["seconds"] < best["seconds"]:
                        best = st

        ch_sec = int(best["seconds"]) if best else jam_sec
        post_to = best["state"] == "POST TIMEOUT" if best else False

        chapters.append({
            "seconds": ch_sec,
            "p": jam["p"],
            "j": jam["j"],
            "type": "JAM",
            "post_timeout": post_to,
            "jam_start_seconds": jam_sec,
        })

    # --- Find timeout blocks ---
    timeout_blocks = []
    in_timeout = False
    block_start = None
    block_type = None

    for st in state_entries:
        if st["state"] in ("OFFICIAL TIMEOUT", "TEAM TIMEOUT", "TIMEOUT"):
            if not in_timeout:
                in_timeout = True
                block_start = st["seconds"]
                block_type = st["state"]
            if st["state"] != "TIMEOUT":
                block_type = st["state"]
        else:
            if in_timeout:
                timeout_blocks.append({"seconds": int(block_start), "type": block_type})
                in_timeout = False

    if in_timeout:
        timeout_blocks.append({"seconds": int(block_start), "type": block_type})

    # Dedup timeout blocks (merge within 30s)
    deduped = []
    for tb in timeout_blocks:
        if not deduped or tb["seconds"] - deduped[-1]["seconds"] > 30:
            deduped.append(tb)

    # Add timeouts to chapters
    for tb in deduped:
        prev_jam = None
        for jam in transitions:
            if jam["seconds"] <= tb["seconds"]:
                prev_jam = jam
        p = prev_jam["p"] if prev_jam else 1

        detail = tb["type"]
        if detail == "OFFICIAL TIMEOUT":
            detail = "Official Timeout"
        elif detail == "TEAM TIMEOUT":
            detail = "Team Timeout"
        else:
            detail = "Timeout"

        chapters.append({
            "seconds": tb["seconds"],
            "p": p,
            "j": 0,
            "type": "TIMEOUT",
            "detail": detail,
        })

    chapters.sort(key=lambda x: x["seconds"])

    return {"jams": transitions, "chapters": chapters}


# ---------------------------------------------------------------------------
# Phase 4: Period clock reading
# ---------------------------------------------------------------------------

def read_period_clocks(ocr_dir: str, chapters: list, frame_interval: float):
    """Read period clock values for each chapter entry."""
    # Load both OCR passes for clock extraction
    pj_ocr = {}
    with open(os.path.join(ocr_dir, "right_pj_ocr.txt")) as f:
        for line in f:
            parts = line.strip().split("|", 1)
            if len(parts) < 2:
                continue
            frame = int(parts[0])
            pj_ocr[frame] = parts[1]

    state_ocr = {}
    state_path = os.path.join(ocr_dir, "right_state_ocr.txt")
    if os.path.exists(state_path):
        with open(state_path) as f:
            for line in f:
                parts = line.strip().split("|", 1)
                if len(parts) < 2:
                    continue
                frame = int(parts[0])
                state_ocr[frame] = parts[1]

    def find_valid_clock(text: str) -> Optional[str]:
        """Extract a valid period clock (0:00 - 30:00) from OCR text."""
        # tesseract often reads : as °, -, ., ', etc.
        matches = re.findall(r"(\d{1,2})[:.°'\-](\d{2})", text)
        for m_str, s_str in matches:
            m_val, s_val = int(m_str), int(s_str)
            if m_val <= 30 and s_val <= 59:
                return f"{m_val}:{s_str}"
        return None

    for ch in chapters:
        frame = int(ch["seconds"] / frame_interval)
        period_clock = None

        # Try both OCR passes, multiple frame offsets
        for offset in range(0, 10):
            f = frame + offset
            # Try plain OCR first (often has cleaner numbers)
            if f in pj_ocr:
                period_clock = find_valid_clock(pj_ocr[f])
                if period_clock:
                    break
            # Fall back to threshold OCR
            if f in state_ocr:
                period_clock = find_valid_clock(state_ocr[f])
                if period_clock:
                    break

        ch["period_clock"] = period_clock


# ---------------------------------------------------------------------------
# Phase 5: Name reading
# ---------------------------------------------------------------------------

def parse_name_line(text: str) -> list:
    """Parse a single OCR line into a list of player names."""
    # Remove star/noise characters
    clean = re.sub(r"[★*¥%#©®]", "", text).strip()
    # Remove leading score-like patterns (e.g., "eys 50 0")
    clean = re.sub(r"^[a-z]{0,4}\s*\d+\s*\d*\s*", "", clean).strip()
    # Remove trailing noise
    clean = re.sub(r"\s*[|}\]\)]\s*$", "", clean).strip()

    if len(clean) < 2:
        return []

    # Split on clear separators between name columns
    # Names are separated by large whitespace gaps or explicit separators
    parts = re.split(r"\s{3,}|\s*\|\s*", clean)
    names = []
    for p in parts:
        p = p.strip()
        # Filter out noise: too short, pure numbers, or obvious garbage
        if len(p) < 2:
            continue
        if re.match(r"^[\d\s.:,;!?|]+$", p):
            continue
        # Filter out common OCR noise patterns
        if re.match(r"^[=\-_~<>{}()\[\]]+$", p):
            continue
        names.append(p)

    return names


def read_names(ocr_dir: str, timeline: dict, frame_interval: float):
    """Read jammer and pivot names for each jam chapter."""
    left_ocr = {}
    left_path = os.path.join(ocr_dir, "left_ocr.txt")
    if not os.path.exists(left_path):
        return

    with open(left_path) as f:
        for line in f:
            parts = line.strip().split("|", 1)
            if len(parts) < 2:
                continue
            frame = int(parts[0])
            left_ocr[frame] = parts[1]

    jams = timeline["jams"]
    chapters = timeline["chapters"]

    for ch in chapters:
        if ch["type"] != "JAM":
            continue

        jam_start = int(ch["jam_start_seconds"] / frame_interval)

        # Find end of this jam (start of next jam or +60 frames)
        jam_end = jam_start + 60
        for j in jams:
            if j["seconds"] > ch["jam_start_seconds"]:
                jam_end = int(j["seconds"] / frame_interval)
                break

        # Seek through frames to find the best name reading
        best_names = {"team1": [], "team2": []}
        best_count = 0

        for f in range(jam_start, min(jam_end, jam_start + 40)):
            if f not in left_ocr:
                continue

            text = left_ocr[f]
            if "HIGHLIGHT" in text.upper():
                continue

            # Split into lines (~ separator from OCR)
            lines = [l.strip() for l in text.split("~") if l.strip()]

            # Parse each line for names — first valid line is team1, second is team2
            names = {"team1": [], "team2": []}
            for line_text in lines:
                parsed = parse_name_line(line_text)
                if parsed:
                    if not names["team1"]:
                        names["team1"] = parsed
                    elif not names["team2"]:
                        names["team2"] = parsed

            count = len(names["team1"]) + len(names["team2"])
            if count > best_count:
                best_count = count
                best_names = names

        ch["team1_names"] = best_names["team1"]
        ch["team2_names"] = best_names["team2"]


# ---------------------------------------------------------------------------
# Phase 6: Output formatting
# ---------------------------------------------------------------------------

def detect_team_names(video_path: str) -> tuple:
    """Detect team names by OCR-ing the top-left scoreboard area."""
    import tempfile

    # Extract a single frame from ~30s in (past any intro/highlight)
    # Crop the team name area: leftmost ~14% width, top ~8% height
    info = probe_video(video_path)
    w, h = info["width"], info["height"]
    cw = int(w * 0.14)
    ch = int(h * 0.08)

    with tempfile.TemporaryDirectory() as tmpdir:
        # Try a few timestamps to find one with readable names
        for ss in [30, 60, 90, 120]:
            frame_path = os.path.join(tmpdir, f"team_{ss}.png")
            subprocess.run(
                ["ffmpeg", "-ss", str(ss), "-i", video_path,
                 "-vframes", "1", "-vf", f"crop={cw}:{ch}:0:0,scale={cw*2}:{ch*2}",
                 "-q:v", "1", frame_path],
                capture_output=True,
            )
            if not os.path.exists(frame_path):
                continue

            result = subprocess.run(
                ["tesseract", frame_path, "stdout", "--psm", "6"],
                capture_output=True, timeout=10,
            )
            text = result.stdout.decode("utf-8", errors="replace")
            lines = [l.strip() for l in text.splitlines() if l.strip() and len(l.strip()) > 2]
            # Filter out numbers-only lines (scores)
            names = [l for l in lines if not re.match(r"^[\d\s.:]+$", l)]

            if len(names) >= 2:
                log(f"Detected teams: '{names[0]}' vs '{names[1]}'")
                return names[0], names[1]

    log("Could not auto-detect team names, using defaults.")
    return "Team 1", "Team 2"


def format_name_line(team_name: str, names: list) -> str:
    """Format a team lineup line: 'Team: Jammer* | Pivot'."""
    if not names:
        return ""
    parts = []
    for i, name in enumerate(names):
        if i == 0:
            parts.append(f"{name}*")
        else:
            parts.append(name)
    return f"{team_name}: {' | '.join(parts)}"


def write_chapters(
    chapters: list,
    output_path: str,
    team1_name: str = "Team 1",
    team2_name: str = "Team 2",
    include_lineups: bool = True,
    include_period_clock: bool = True,
):
    """Write the final chapters file."""
    lines = []
    cur_period = 0

    for ch in chapters:
        if ch["p"] != cur_period:
            cur_period = ch["p"]
            if lines:
                lines.append("")
            lines.append(f"Period {cur_period}:")

        ts = fmt_ts(ch["seconds"])

        if ch["type"] == "TIMEOUT":
            detail = ch.get("detail", "Timeout")
            if include_period_clock and ch.get("period_clock"):
                pc = fmt_pc(ch["period_clock"])
                lines.append(f"{ts} {detail} [{pc}]")
            else:
                lines.append(f"{ts} {detail}")
        else:
            # Jam entry
            pj = f"P{ch['p']}J{ch['j']:02d}"
            suffix = " (Post Timeout)" if ch.get("post_timeout") else ""

            if include_period_clock and ch.get("period_clock"):
                pc = fmt_pc(ch["period_clock"])
                lines.append(f"{ts} {pj}{suffix} [{pc}]")
            else:
                lines.append(f"{ts} {pj}{suffix}")

            if include_lineups:
                t1 = ch.get("team1_names", [])
                t2 = ch.get("team2_names", [])
                line1 = format_name_line(team1_name, t1)
                line2 = format_name_line(team2_name, t2)
                if line1:
                    lines.append(line1)
                if line2:
                    lines.append(line2)

        lines.append("")

    output = "\n".join(lines).rstrip() + "\n"

    with open(output_path, "w") as f:
        f.write(output)

    return output


# ---------------------------------------------------------------------------
# Pipeline (shared by CLI and web UI)
# ---------------------------------------------------------------------------

def run_pipeline(
    video_path: str,
    output_path: str,
    ocr_dir: str,
    fps: float = 1 / 3,
    include_lineups: bool = True,
    include_period_clock: bool = True,
    team1: str = "Team 1",
    team2: str = "Team 2",
    reprocess: bool = False,
    progress_cb=None,
) -> str:
    """
    Run the full indexing pipeline.

    Args:
        progress_cb: Optional callback(phase, current, total, message).
                     phase is one of: probe, extract, ocr_right, ocr_left,
                     timeline, clocks, names, output, done.
    Returns:
        The generated chapters text.
    """
    frame_interval = 1.0 / fps

    def emit(phase, current, total, msg):
        log(msg)
        if progress_cb:
            progress_cb(phase, current, total, msg)

    # Phase 1: Frame extraction
    emit("probe", 0, 1, "Probing video...")
    video_info = probe_video(video_path)
    emit("probe", 1, 1,
         f"Resolution: {video_info['width']}x{video_info['height']}, "
         f"Duration: {fmt_ts(int(video_info['duration']))}")

    if reprocess and os.path.exists(ocr_dir):
        emit("extract", 1, 1, "Reprocessing: skipping frame extraction.")
    else:
        emit("extract", 0, 1, "Extracting frames...")
        extract_frames(video_path, ocr_dir, fps, video_info)
        emit("extract", 1, 1, "Frame extraction complete.")

    low_res = video_info["height"] < 720
    if low_res and include_lineups:
        emit("extract", 1, 1,
             "WARNING: Resolution below 720p — lineup parsing unsupported, skipping.")
        include_lineups = False

    # Phase 2: OCR
    pj_ocr_path = os.path.join(ocr_dir, "right_pj_ocr.txt")
    if reprocess and os.path.exists(pj_ocr_path):
        emit("ocr_right", 1, 1, "Reprocessing: skipping OCR.")
    else:
        run_ocr(ocr_dir, fps, progress_cb=progress_cb)

    # Phase 3: Timeline
    emit("timeline", 0, 1, "Building timeline...")
    timeline = build_timeline(ocr_dir, frame_interval)
    jam_count = len(timeline["jams"])
    timeout_count = sum(1 for c in timeline["chapters"] if c["type"] == "TIMEOUT")
    emit("timeline", 1, 1, f"Found {jam_count} jams, {timeout_count} timeouts")

    # Phase 4: Period clocks
    if include_period_clock:
        emit("clocks", 0, 1, "Reading period clocks...")
        read_period_clocks(ocr_dir, timeline["chapters"], frame_interval)
        emit("clocks", 1, 1, "Period clocks done.")

    # Phase 5: Names
    if include_lineups:
        emit("names", 0, 1, "Reading player names...")
        read_names(ocr_dir, timeline, frame_interval)
        emit("names", 1, 1, "Player names done.")

    # Auto-detect team names if not provided
    if team1 == "Team 1" or team2 == "Team 2":
        emit("output", 0, 1, "Detecting team names...")
        detected1, detected2 = detect_team_names(video_path)
        if team1 == "Team 1":
            team1 = detected1
        if team2 == "Team 2":
            team2 = detected2

    # Phase 6: Output
    emit("output", 0, 1, f"Writing chapters to {output_path}...")
    result = write_chapters(
        timeline["chapters"],
        output_path,
        team1_name=team1,
        team2_name=team2,
        include_lineups=include_lineups,
        include_period_clock=include_period_clock,
    )

    # Summary
    periods = set(c["p"] for c in timeline["chapters"])
    for p in sorted(periods):
        p_jams = sum(1 for c in timeline["chapters"] if c["p"] == p and c["type"] == "JAM")
        p_tos = sum(1 for c in timeline["chapters"] if c["p"] == p and c["type"] == "TIMEOUT")
        emit("done", 1, 1, f"Period {p}: {p_jams} jams, {p_tos} timeouts")

    emit("done", 1, 1, f"Done! Chapters written to: {output_path}")
    return result


# ---------------------------------------------------------------------------
# CLI Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate YouTube chapter markers from roller derby bout footage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example:\n  python bout_indexer.py game.mkv --output chapters.txt",
    )
    parser.add_argument("video", help="Path to the bout video file")
    parser.add_argument(
        "--fps", type=float, default=1 / 3,
        help="Frame sampling rate in fps (default: 0.333 = 1 frame per 3 seconds)",
    )
    parser.add_argument("--no-lineups", action="store_true", help="Disable lineup output")
    parser.add_argument("--no-period-clock", action="store_true", help="Disable period clock")
    parser.add_argument("--output", type=str, default=None, help="Output chapters file")
    parser.add_argument("--ocr-dir", type=str, default=None, help="OCR data directory")
    parser.add_argument("--reprocess", action="store_true", help="Reuse existing OCR data")
    parser.add_argument("--team1", type=str, default=None, help="Team 1 name (top row)")
    parser.add_argument("--team2", type=str, default=None, help="Team 2 name (bottom row)")

    args = parser.parse_args()

    video_path = os.path.realpath(os.path.abspath(args.video))
    if not os.path.exists(video_path):
        print(f"Error: Video file not found: {video_path}", file=sys.stderr)
        sys.exit(1)

    video_dir = os.path.dirname(video_path)
    video_stem = Path(video_path).stem
    output_path = os.path.realpath(
        args.output or os.path.join(video_dir, f"{video_stem} Chapters.txt")
    )
    ocr_dir = os.path.realpath(
        args.ocr_dir or os.path.join(video_dir, f"{video_stem} OCR")
    )

    print(f"nws-bout-indexer")
    print(f"  Video:  {video_path}")
    print(f"  Output: {output_path}")
    print(f"  OCR:    {ocr_dir}")
    print(f"  FPS:    {args.fps} ({1.0/args.fps:.1f}s per frame)")
    print()

    run_pipeline(
        video_path=video_path,
        output_path=output_path,
        ocr_dir=ocr_dir,
        fps=args.fps,
        include_lineups=not args.no_lineups,
        include_period_clock=not args.no_period_clock,
        team1=args.team1 or "Team 1",
        team2=args.team2 or "Team 2",
        reprocess=args.reprocess,
    )


if __name__ == "__main__":
    main()
