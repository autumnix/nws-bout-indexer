# OCR Engine Comparison Report

Tested on Game 1 scoreboard frames at 2560x1440 (native resolution).

## Summary

| Engine | Name Accuracy | P/J Accuracy | Speed (per frame) | Preprocessing Needed | Dependencies |
|--------|--------------|-------------|-------------------|---------------------|-------------|
| **PaddleOCR** | **Excellent** | **Perfect** | ~1.3s | **None** | paddlepaddle, paddleocr (~500MB) |
| EasyOCR | Fair | Good | ~0.5s | None | torch, easyocr (~800MB) |
| Tesseract | Poor | Fair | ~0.1s | Heavy (crop+threshold) | tesseract (system) |

## Recommendation: PaddleOCR

PaddleOCR is the clear winner. It reads scoreboard text nearly perfectly on raw
frames with zero preprocessing needed. This eliminates the entire crop+threshold
pipeline and all the OCR error correction code.

## Detailed Results

### Frame: P1 J1 names (Impact Play | Olivia Shootin' John / Spread Eagle | Lelith)

| Engine | Output |
|--------|--------|
| **PaddleOCR** | `Impact Play`, `Olivia Shootin' John`, `Spread Eagle`, `Lelith` |
| EasyOCR | `Impaet Ploy`, `Olivia Sheoti' Jehn`, `Spread Eagb`, `Lelmh` |
| Tesseract | `inpactPhay Olvia Shoot' John`, `Spread Eagle Lolth` |

### Frame: P1 J13 names (Hauss the Boss | Olivia Shootin' John / Truckstop Trixie | StoneHer)

| Engine | Output |
|--------|--------|
| **PaddleOCR** | `Hauss the Boss`, `Olivia Shootin' John`, `Truckstop Trixie`, `StoneHer` |
| EasyOCR | `Hauss the Boss`, `Olivia Sheotin' John`, `Truckstop Tnudio`, `StoneHet` |
| Tesseract | `Haues the Boss % —Olvia Shoot' John`, `——_ Truckstop Trixie StoneHer` |

### Frame: P2 J15 names (Hauss the Boss | Nine Lives / Spread Eagle | Lelith)

| Engine | Output |
|--------|--------|
| **PaddleOCR** | `Hauss the Boss`, `Nine Lives`, `Spread Eagle`, `Lelith` |
| EasyOCR | `Hauss tho Bess`, `NneLivos`, `SproadEagb`, `Lelmh` |
| Tesseract | `SL Husa Naive : —_`, `eo Lat` |

### Frame: P/J detection (P1 J1)

| Engine | Output |
|--------|--------|
| **PaddleOCR** | `P1`, `J1` |
| EasyOCR | `P1`, `J1` |
| Tesseract | `Pt ean onl` |

### Frame: P/J + state (P1 J10 LINEUP)

| Engine | Output |
|--------|--------|
| **PaddleOCR** | `P1`, `J11`, `14:59`, `2:00` |
| EasyOCR | `P1`, `JII`, `1A.5S 9.00` |
| Tesseract | `P1 Ju / AAERE ON` |

## Key Observations

1. **PaddleOCR reads names perfectly** — even "Olivia Shootin' John" with the
   apostrophe, and "StoneHer" with the mixed case. No preprocessing needed.

2. **PaddleOCR separates text elements** — returns each name as a separate
   detected text region, making parsing trivial.

3. **PaddleOCR reads clocks** — `14:59` and `2:00` are returned directly,
   eliminating the need for special clock parsing.

4. **EasyOCR is middle-ground** — better than tesseract but still makes frequent
   errors (Ploy, Sheoti, Tnudio, Lelmh). Would still need fuzzy matching.

5. **Tesseract requires heavy preprocessing** — and still produces garbage on
   many frames. The crop+threshold pipeline adds complexity for mediocre results.

6. **Speed tradeoff** — PaddleOCR is ~10x slower per frame than tesseract, but
   the total pipeline time is dominated by frame extraction (ffmpeg), not OCR.
   For a 90-min bout at 1 frame/3s = ~1800 frames, PaddleOCR adds ~40 min vs
   ~3 min for tesseract. This could be mitigated by only running PaddleOCR on
   key frames (jam transitions) rather than every frame.

## Migration Path

To switch from tesseract to PaddleOCR:
1. Add `paddlepaddle` and `paddleocr` to requirements.txt
2. Replace `ocr_frame_plain`, `ocr_frame_threshold`, `ocr_frame_names` with a
   single PaddleOCR call on the raw (un-preprocessed) frame
3. Remove all the crop+threshold preprocessing code
4. Simplify name parsing — PaddleOCR returns names as separate text regions
5. Simplify P/J parsing — PaddleOCR returns clean "P1", "J11" etc.
6. Consider: run PaddleOCR only on frames near jam transitions to save time
