# nws-bout-indexer

Generate YouTube chapter markers from roller derby bout footage.

Scans the scoreboard overlay in a bout video via OCR and produces a chapters file with jam timestamps, lineups, period clocks, and timeout markers.

## Requirements

- Python 3.9+
- [ffmpeg](https://ffmpeg.org/) (system install)
- [tesseract](https://github.com/tesseract-ocr/tesseract) (system install)
- Pillow (Python package)

### Install

```bash
# macOS
brew install ffmpeg tesseract

# Python deps
pip install -r requirements.txt
```

## Usage

```bash
python bout_indexer.py <video_file> [options]
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--fps FLOAT` | `0.333` | Frame sampling rate (1/3 = one frame every 3 seconds) |
| `--no-lineups` | off | Disable jam lineup output |
| `--no-period-clock` | off | Disable period clock output |
| `--output FILE` | `<video> Chapters.txt` | Output chapters file path |
| `--ocr-dir DIR` | `<video> OCR/` | OCR data directory |
| `--reprocess` | off | Skip extraction, reuse existing OCR data |
| `--team1 NAME` | `Team 1` | Name of team 1 (top row on scoreboard) |
| `--team2 NAME` | `Team 2` | Name of team 2 (bottom row on scoreboard) |

### Examples

```bash
# Basic usage
python bout_indexer.py "Game 1.mkv"

# With team names
python bout_indexer.py "Game 1.mkv" \
  --team1 "Hotrod Honeys" --team2 "Hustlers"

# Higher sampling rate (every 2 seconds)
python bout_indexer.py "Game 1.mkv" --fps 0.5

# Chapters only (no lineups or period clock)
python bout_indexer.py "Game 1.mkv" --no-lineups --no-period-clock

# Reprocess from existing OCR data (skip frame extraction)
python bout_indexer.py "Game 1.mkv" --reprocess
```

## Output Format

```
Period 1:
00:03:39 P1J01 [29m57s]
Hotrod Honeys: Impact Play* | Nine Lives
Hustlers: Spread Eagle* | Lelith

00:05:03 P1J02 [28m33s]
Hotrod Honeys: Miso Thorny* | Olivia Shootin' John
Hustlers: Truckstop Trixie* | StoneHer

00:14:42 Official Timeout [12m48s]

00:15:48 P1J08 (Post Timeout) [12m08s]
...
```

See [Bout Post Processing Specs.txt](Bout%20Post%20Processing%20Specs.txt) for the full specification.

## How It Works

1. **Frame extraction** — ffmpeg crops the right (P/J, clocks, state) and left (names) scoreboard regions at configurable intervals
2. **Dual OCR** — tesseract runs two passes on each right frame: plain (for P/J numbers) and threshold-based (for state banners like LINEUP, TIMEOUT)
3. **Timeline construction** — a state machine identifies jam transitions, timeouts, and their correct chapter timestamps
4. **Name reading** — left scoreboard frames are OCR'd to extract jammer and pivot names, seeking forward when names load late
5. **Output** — formatted per spec with optional lineups and period clocks

## License

MIT
