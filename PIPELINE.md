# EPUB to Audiobook Pipeline

Convert EPUB books to audiobook (M4B) with chapter markers, using Microsoft Edge TTS (free, no API key required).

## Architecture

```
EPUB (ZIP)
  │
  ├─ OEBPS/content.opf ─── spine order + manifest
  ├─ OEBPS/Text/*.xhtml ── chapter HTML
  └─ OEBPS/Images/ ─────── cover.jpg
         │
         ▼
┌─────────────────────────────────────────────────────┐
│  Step 1: Text Extraction                            │
│  parse_spine_order() → load_chapters_from_epub()    │
│  - Read content.opf for reading order (spine)       │
│  - Extract text from each XHTML via BeautifulSoup   │
│  - Strip: footnotes, images, SVGs, boilerplate      │
│  - Preserve: headings (h1-h4), paragraphs, divs     │
│  - Skip: cover, nav, copyright, index, bibliography │
│  Output: 17 Chapter objects (id, title, text)        │
└──────────────────────┬──────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────┐
│  Step 2: Clean & Chunk                              │
│  clean_text_for_tts() → chunk_text()                │
│  - Remove citation markers: [1], ①②③               │
│  - Normalize whitespace                             │
│  - Split by paragraph → sentence boundaries         │
│  - Chinese sentence endings: 。！？；                │
│  - Max 3000 chars per chunk (edge-tts limit)        │
│  Output: 80 text chunks across 17 chapters          │
└──────────────────────┬──────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────┐
│  Step 3: TTS Generation                             │
│  generate_audio() via edge-tts                      │
│  - Voice: zh-TW-HsiaoChenNeural (Taiwanese)         │
│  - Async HTTP → Microsoft Edge TTS service          │
│  - Each chunk → MP3 file                            │
│  - Retry once on failure                            │
│  - Progress tracking in progress.json (resumable)   │
│  - Chunk→text mapping in chunks_map.json            │
│  Output: output/chunks/*.mp3 (80 files)             │
└──────────────────────┬──────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────┐
│  Step 4: Merge & Post-process                       │
│  merge_chunks() via ffmpeg                          │
│  Per chapter:                                       │
│  - Concatenate chunk MP3s                           │
│  - Apply ffmpeg audio filters:                      │
│    • silenceremove (start + end, >0.5s, <-30dB)     │
│    • loudnorm (I=-16, TP=-1.5, LRA=11)              │
│  - Re-encode: AAC 64kbps, 44.1kHz, mono             │
│  Output: output/chapters/*.m4a (17 files)           │
└──────────────────────┬──────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────┐
│  Step 5: Build Audiobook                            │
│  build_m4b() via ffmpeg                             │
│  - Concatenate all chapter M4As (stream copy)       │
│  - Generate FFMETADATA with chapter timestamps      │
│  - Inject metadata: title, author, language         │
│  - Embed cover.jpg as attached picture              │
│  - movflags +faststart for streaming                │
│  Output: *.m4b (322 MB, 11.6h)                     │
└─────────────────────────────────────────────────────┘
```

## Audio Processing Chain

Each chunk goes through:

```
edge-tts (MP3)
  → ffmpeg concat (merge chapter chunks)
    → silenceremove (trim leading silence)
    → areverse
    → silenceremove (trim trailing silence, was leading after reverse)
    → areverse
    → loudnorm I=-16 TP=-1.5 LRA=11 (EBU R128 normalization)
  → AAC 64kbps mono 44.1kHz
```

- **loudnorm**: Broadcast-standard loudness normalization. Target -16 LUFS keeps speech clear without dynamic range compression artifacts.
- **silenceremove**: Removes silence >0.5s below -30dB at both start and end of each chapter. Eliminates edge-tts pause artifacts.

## Output Structure

```
output/
├── {book_title}.m4b          # Final audiobook
├── cover.jpg                 # Extracted from EPUB
├── chunks_map.json           # chunk_id → source text (debugging)
├── progress.json             # Resume state (set of completed chunk IDs)
├── chunks/                   # 80 TTS-generated MP3 files
│   ├── Prologue_c000.mp3
│   ├── Prologue_c001.mp3
│   ├── Ch1_c000.mp3
│   └── ...
└── chapters/                 # 17 normalized M4A chapter files
    ├── Prologue.m4a
    ├── Ch1.m4a
    └── ...
```

## Usage

```bash
# Install dependencies
uv sync

# Full conversion
uv run python main.py book.epub -o output

# Test with first 2 chapters only
uv run python main.py book.epub --sample -o output

# Custom voice
uv run python main.py book.epub --voice zh-TW-YunJheNeural -o output

# Speed up speech
uv run python main.py book.epub --rate "+20%" -o output
```

## Resume Support

The pipeline saves progress to `progress.json`. If interrupted (timeout, Ctrl+C, network error), re-running the same command skips already-generated chunks and continues from where it left off.

## Chapter Title Resolution

Titles are resolved in order:
1. NCX TOC entries (for part dividers)
2. First `<h1>`–`<h4>` heading in the XHTML
3. `<title>` tag fallback
4. Filename (e.g. `Ch1`) as last resort

## Dependencies

| Package | Purpose |
|---------|---------|
| edge-tts | Microsoft Edge TTS (free, high-quality Chinese voices) |
| beautifulsoup4 + lxml | HTML/XHTML parsing for text extraction |
| mutagen | Audio metadata (available for future use) |
| html2text | HTML-to-text conversion (available for future use) |
| ffmpeg (system) | Audio concat, filters, M4B muxing |

## Known Limitations

- **edge-tts outputs MP3**, not WAV. The intermediate files are lossy. Since the TTS source is already lossy, converting to WAV mid-pipeline would not improve quality.
- **LLM cleanup deferred**. For structured EPUB XHTML sources (not OCR'd scans), regex-based cleaning is sufficient. LLM cleanup adds cost and hallucination risk for minimal benefit. Revisit if processing OCR'd content.
- **No parallel TTS**. Chunks are generated sequentially to avoid rate-limiting by the edge-tts service.
