# book2audio

Convert EPUB books to audiobook (M4B) with chapter markers — free, no API key required.

Uses [Microsoft Edge TTS](https://github.com/rany2/edge-tts) for high-quality neural speech synthesis in 100+ languages.

## Features

- Extracts chapters from EPUB in correct reading order
- Chinese sentence-aware chunking (handles 。！？；boundaries)
- Audio normalization (EBU R128 loudnorm)
- Automatic silence removal from chapter start/end
- M4B output with embedded cover, chapter markers, and metadata
- Resumable —中断后重新运行会跳过已生成的片段

## Prerequisites

- Python 3.14+
- [uv](https://docs.astral.sh/uv/) (package manager)
- [ffmpeg](https://ffmpeg.org/) (audio processing)

## Quick Start

```bash
git clone https://github.com/xzAscC/book2audio.git
cd book2audio
uv sync

uv run python main.py book.epub -o output
```

Output: `output/{book_title}.m4b`

## Usage

```bash
# Full conversion
uv run python main.py book.epub -o output

# Test with first 2 chapters only
uv run python main.py book.epub --sample -o output

# Change voice (see available voices below)
uv run python main.py book.epub --voice zh-TW-YunJheNeural

# Speed up speech by 20%
uv run python main.py book.epub --rate "+20%"
```

## Available Chinese Voices

| Voice | Language | Gender |
|-------|----------|--------|
| `zh-TW-HsiaoChenNeural` | Taiwanese Mandarin | Female (default) |
| `zh-TW-YunJheNeural` | Taiwanese Mandarin | Male |
| `zh-TW-HsiaoYuNeural` | Taiwanese Mandarin | Female |
| `zh-CN-XiaoxiaoNeural` | Simplified Chinese | Female |
| `zh-CN-YunjianNeural` | Simplified Chinese | Male |
| `zh-CN-YunxiNeural` | Simplified Chinese | Male |

For the full list of 400+ voices across 100+ languages, run:

```python
import asyncio, edge_tts
async def list_voices():
    for v in await edge_tts.list_voices():
        print(v['ShortName'], v['Locale'], v['Gender'])
asyncio.run(list_voices())
```

## Pipeline Overview

```
EPUB → Text Extraction → Cleaning → Chunking → TTS → Normalize + Merge → M4B
```

See [PIPELINE.md](./PIPELINE.md) for the full architecture diagram and technical details.

## Output Structure

```
output/
├── {title}.m4b           # Final audiobook
├── cover.jpg              # Book cover (extracted from EPUB)
├── chunks_map.json        # chunk_id → source text mapping
├── progress.json          # Resume state
├── chunks/                # Individual TTS audio segments
└── chapters/              # Per-chapter normalized audio
```

## License

MIT
