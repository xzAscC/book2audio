#!/usr/bin/env python3
"""EPUB to Audiobook converter using edge-tts."""

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup, NavigableString, Tag
import edge_tts


DEFAULT_VOICE = "zh-TW-HsiaoChenNeural"
DEFAULT_RATE = "+0%"
DEFAULT_CHUNK_CHARS = 3000

SKIP_FILES = frozenset({
    "cover.xhtml", "Titlepage.xhtml", "nav.xhtml", "Copyright.xhtml",
    "Index1.xhtml", "Index2.xhtml", "Footnote.xhtml", "Bibliography.xhtml",
})

IMAGE_ONLY_FILES = frozenset({
    "Part1-1.xhtml", "Part2-1.xhtml", "Part3-1.xhtml",
})

PART_TITLES = {
    "Part1-2.xhtml": "第一部　東亞冷戰下美國政府的臺灣方案",
    "Part2-2.xhtml": "第二部　蔣介石的反共王國",
    "Part3-2.xhtml": "第三部　民主化的進程",
}

AUDIO_FILTERS = (
    "silenceremove=start_periods=1:start_duration=0.5:start_threshold=-30dB,"
    "areverse,"
    "silenceremove=start_periods=1:start_duration=0.5:start_threshold=-30dB,"
    "areverse,"
    "loudnorm=I=-16:TP=-1.5:LRA=11"
)


@dataclass
class Chapter:
    id: str
    filename: str
    title: str
    text: str
    chunks: list[str] = field(default_factory=list)


def parse_spine_order(opf_content: str) -> tuple[list[str], dict[str, str]]:
    ns = {
        'opf': 'http://www.idpf.org/2007/opf',
        'dc': 'http://purl.org/dc/elements/1.1/',
    }
    root = ET.fromstring(opf_content)

    manifest = {}
    for item in root.findall('opf:manifest/opf:item', ns):
        iid = item.get('id')
        href = item.get('href')
        if iid and href:
            manifest[iid] = href

    spine_ids = [ir.get('idref') for ir in root.findall('opf:spine/opf:itemref', ns)]
    return spine_ids, manifest


def extract_text_from_html(html_content: str) -> tuple[str, str]:
    soup = BeautifulSoup(html_content, 'lxml-xml')

    for tag in soup.find_all(['script', 'style']):
        tag.decompose()

    for a in soup.find_all('a', class_='footnote'):
        a.decompose()

    for sup in soup.find_all('sup'):
        if sup.find('a', class_='footnote') or sup.find('a', href=True):
            sup.decompose()

    for div in soup.find_all('div', class_='pic'):
        div.decompose()

    for div in soup.find_all('div', class_='note'):
        div.decompose()

    for svg in soup.find_all('svg'):
        svg.decompose()

    title = ""
    for heading in soup.find_all(['h1', 'h2', 'h3']):
        title = heading.get_text(strip=True)
        break
    if not title:
        title_tag = soup.find('title')
        if title_tag:
            title = title_tag.get_text(strip=True)

    body = soup.find('body')
    if not body:
        return title, ""

    text_parts: list[str] = []

    for element in body.children:
        if isinstance(element, NavigableString):
            text = str(element).strip()
            if text:
                text_parts.append(text)
            continue

        if not isinstance(element, Tag):
            continue

        tag_name = element.name

        if tag_name in ('h1', 'h2', 'h3', 'h4'):
            heading_text = element.get_text(strip=True)
            if heading_text:
                text_parts.append(f"\n{heading_text}\n")
        elif tag_name == 'p':
            para_text = element.get_text(strip=True)
            if para_text:
                text_parts.append(para_text)
        elif tag_name == 'div':
            div_text = element.get_text(separator=' ', strip=True)
            if div_text:
                text_parts.append(div_text)
        elif tag_name in ('ul', 'ol'):
            for li in element.find_all('li'):
                li_text = li.get_text(strip=True)
                if li_text:
                    text_parts.append(li_text)

    text = '\n'.join(text_parts)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'  +', ' ', text)
    return title, text.strip()


def load_chapters_from_epub(epub_path: str) -> list[Chapter]:
    chapters: list[Chapter] = []

    with zipfile.ZipFile(epub_path, 'r') as zf:
        with zf.open('OEBPS/content.opf') as f:
            opf_content = f.read().decode('utf-8')

        spine_ids, manifest = parse_spine_order(opf_content)

        for item_id in spine_ids:
            href = manifest.get(item_id, '')
            filename = href.split('/')[-1]

            if filename in SKIP_FILES or filename in IMAGE_ONLY_FILES:
                continue

            full_path = f"OEBPS/{href}"
            try:
                with zf.open(full_path) as f:
                    html_content = f.read().decode('utf-8')
            except KeyError:
                continue

            title, text = extract_text_from_html(html_content)

            if filename in PART_TITLES:
                title = PART_TITLES[filename]

            if len(text.strip()) < 20:
                continue

            chapter_id = filename.replace('.xhtml', '')
            chapters.append(Chapter(
                id=chapter_id,
                filename=filename,
                title=title or chapter_id,
                text=text,
            ))
            print(f"  {filename:25s} | {title[:40]:40s} | {len(text):6d} chars")

    return chapters


def clean_text_for_tts(text: str) -> str:
    # LLM-assisted cleanup intentionally deferred: text is sourced from
    # structured EPUB XHTML with no OCR artifacts, broken hyphenation,
    # or garbled characters.  Deterministic regex cleaning suffices and
    # avoids hallucination risk.  Revisit if processing OCR'd scans.
    text = re.sub(r'\[\d+\]', '', text)
    text = re.sub(r'[①②③④⑤⑥⑦⑧⑨⑩]', '', text)
    text = re.sub(r'←back', '', text)
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'\n ', '\n', text)
    return text.strip()


def split_into_sentences(text: str) -> list[str]:
    sentences = re.split(r'(?<=[。！？；])|(?<=[。！？][」』】」])', text)
    sentences = [s.strip() for s in sentences if s.strip()]

    if len(sentences) <= 1 and len(text) > 200:
        parts = re.split(r'(?<=[，、])', text)
        sentences = []
        current = ""
        for part in parts:
            if len(current) + len(part) > 500:
                if current:
                    sentences.append(current)
                current = part
            else:
                current += part
        if current:
            sentences.append(current)

    return sentences if sentences else [text]


def chunk_text(text: str, max_chars: int = DEFAULT_CHUNK_CHARS) -> list[str]:
    if len(text) <= max_chars:
        return [text] if text.strip() else []

    paragraphs = [p.strip() for p in re.split(r'\n+', text) if p.strip()]

    chunks: list[str] = []
    current_chunk = ""

    for para in paragraphs:
        if len(current_chunk) + len(para) + 1 > max_chars:
            if current_chunk.strip():
                chunks.append(current_chunk.strip())

            if len(para) > max_chars:
                for sent in split_into_sentences(para):
                    if len(current_chunk) + len(sent) + 1 > max_chars:
                        if current_chunk.strip():
                            chunks.append(current_chunk.strip())
                        current_chunk = sent
                    else:
                        current_chunk = (current_chunk + " " + sent) if current_chunk else sent
            else:
                current_chunk = para
        else:
            current_chunk = (current_chunk + "\n" + para) if current_chunk else para

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    return chunks


async def generate_audio(text: str, output_path: str, voice: str, rate: str) -> bool:
    try:
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        await communicate.save(output_path)
        return os.path.exists(output_path) and os.path.getsize(output_path) > 100
    except Exception as e:
        print(f"    TTS error: {e}")
        return False


def get_duration(audio_path: str) -> float:
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
             '-of', 'csv=p=0', audio_path],
            capture_output=True, text=True, timeout=30,
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def merge_chunks(chunk_paths: list[str], output_path: str) -> bool:
    if not chunk_paths:
        return False
    if len(chunk_paths) == 1:
        r = subprocess.run(
            ['ffmpeg', '-y', '-i', chunk_paths[0],
             '-af', AUDIO_FILTERS,
             '-c:a', 'aac', '-b:a', '64k', '-ar', '44100', '-ac', '1',
             output_path],
            capture_output=True, text=True, timeout=300,
        )
        return r.returncode == 0

    list_path = output_path + '.list.txt'
    with open(list_path, 'w') as f:
        for p in chunk_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")

    try:
        r = subprocess.run(
            ['ffmpeg', '-y', '-f', 'concat', '-safe', '0',
             '-i', list_path,
             '-af', AUDIO_FILTERS,
             '-c:a', 'aac', '-b:a', '64k', '-ar', '44100', '-ac', '1',
             output_path],
            capture_output=True, text=True, timeout=300,
        )
        return r.returncode == 0
    finally:
        if os.path.exists(list_path):
            os.remove(list_path)


def build_m4b(chapters: list[tuple[str, str]], output_path: str,
              title: str, author: str, cover: str | None = None) -> bool:
    chapter_times: list[tuple[str, int]] = []
    current_ms = 0
    for ch_title, audio_path in chapters:
        chapter_times.append((ch_title, int(current_ms)))
        current_ms += get_duration(audio_path) * 1000

    total_s = current_ms / 1000
    print(f"  Duration : {total_s/3600:.1f}h ({total_s/60:.0f}min)")
    print(f"  Chapters : {len(chapters)}")

    concat_path = output_path + '.concat.txt'
    with open(concat_path, 'w') as f:
        for _, audio_path in chapters:
            f.write(f"file '{os.path.abspath(audio_path)}'\n")

    meta_path = output_path + '.ffmetadata'
    with open(meta_path, 'w') as f:
        f.write(';FFMETADATA1\n')
        f.write(f'title={title}\nartist={author}\nlanguage=chi\n\n')
        for i, (ch_title, start_ms) in enumerate(chapter_times):
            end_ms = chapter_times[i + 1][1] if i + 1 < len(chapter_times) else int(current_ms)
            f.write('[CHAPTER]\nTIMEBASE=1/1000\n')
            f.write(f'START={int(start_ms)}\nEND={int(end_ms)}\ntitle={ch_title}\n\n')

    merged_path = output_path + '.merged.m4a'
    r = subprocess.run(
        ['ffmpeg', '-y', '-f', 'concat', '-safe', '0',
         '-i', concat_path, '-c', 'copy', merged_path],
        capture_output=True, text=True, timeout=600,
    )
    if r.returncode != 0:
        print(f"  Merge failed: {r.stderr[-300:]}")
        _cleanup(concat_path, meta_path, merged_path)
        return False

    cmd = ['ffmpeg', '-y', '-i', merged_path, '-i', meta_path,
           '-map_metadata', '1', '-c', 'copy']
    if cover and os.path.exists(cover):
        cmd = ['ffmpeg', '-y',
               '-i', merged_path, '-i', meta_path, '-i', cover,
               '-map', '0:a', '-map', '2:v', '-map_metadata', '1',
               '-c:a', 'copy', '-c:v', 'mjpeg',
               '-disposition:v', 'attached_pic']
    cmd += ['-movflags', '+faststart', output_path]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    _cleanup(concat_path, meta_path, merged_path)

    if r.returncode != 0:
        print(f"  Metadata inject failed: {r.stderr[-300:]}")
        return False

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    print(f"  → {output_path}  ({size_mb:.1f} MB, {total_s/3600:.1f}h)")
    return True


def build_mp3(chapters: list[tuple[str, str]], output_path: str,
              title: str, author: str) -> bool:
    concat_path = output_path + '.concat.txt'
    with open(concat_path, 'w') as f:
        for _, audio_path in chapters:
            f.write(f"file '{os.path.abspath(audio_path)}'\n")

    r = subprocess.run(
        ['ffmpeg', '-y', '-f', 'concat', '-safe', '0',
         '-i', concat_path, '-c:a', 'libmp3lame', '-b:a', '64k',
         '-ar', '44100', '-ac', '1',
         '-metadata', f'title={title}',
         '-metadata', f'artist={author}',
         '-metadata', 'language=chi', output_path],
        capture_output=True, text=True, timeout=600,
    )
    os.remove(concat_path)
    return r.returncode == 0


def _cleanup(*paths: str) -> None:
    for p in paths:
        if os.path.exists(p):
            os.remove(p)


PROGRESS_FILENAME = "progress.json"


def save_progress(output_dir: str, done: set[str]) -> None:
    with open(os.path.join(output_dir, PROGRESS_FILENAME), 'w') as f:
        json.dump(sorted(done), f)


def load_progress(output_dir: str) -> set[str]:
    p = os.path.join(output_dir, PROGRESS_FILENAME)
    if os.path.exists(p):
        with open(p) as f:
            return set(json.load(f))
    return set()


async def run(epub_path: str, output_dir: str, voice: str, rate: str,
              sample: bool = False) -> None:
    os.makedirs(output_dir, exist_ok=True)
    chunks_dir = os.path.join(output_dir, "chunks")
    chapters_dir = os.path.join(output_dir, "chapters")
    os.makedirs(chunks_dir, exist_ok=True)
    os.makedirs(chapters_dir, exist_ok=True)

    cover_path: str | None = None
    with zipfile.ZipFile(epub_path, 'r') as zf:
        try:
            data = zf.read('OEBPS/Images/cover.jpg')
            cover_path = os.path.join(output_dir, "cover.jpg")
            with open(cover_path, 'wb') as f:
                f.write(data)
        except KeyError:
            pass

    print("=" * 50)
    print("Step 1: Extract text")
    print("=" * 50)
    all_chapters = load_chapters_from_epub(epub_path)
    print(f"  {len(all_chapters)} chapters loaded\n")

    if sample:
        all_chapters = all_chapters[:2]
        print(f"  SAMPLE MODE: {len(all_chapters)} chapters\n")

    print("=" * 50)
    print("Step 2: Clean & chunk")
    print("=" * 50)
    total_chunks = 0
    for ch in all_chapters:
        ch.text = clean_text_for_tts(ch.text)
        ch.chunks = chunk_text(ch.text)
        total_chunks += len(ch.chunks)
        print(f"  {ch.id:25s} | {ch.title[:35]:35s} | {len(ch.text):6d}c → {len(ch.chunks):3d} chunks")
    print(f"  Total: {total_chunks} chunks\n")

    print("=" * 50)
    print("Step 3: TTS generation")
    print("=" * 50)
    done = load_progress(output_dir)
    idx = 0

    for ch in all_chapters:
        for i, txt in enumerate(ch.chunks):
            chunk_id = f"{ch.id}_c{i:03d}"
            chunk_path = os.path.join(chunks_dir, f"{chunk_id}.mp3")
            idx += 1

            if chunk_id in done and os.path.exists(chunk_path):
                dur = get_duration(chunk_path)
                print(f"  [{idx}/{total_chunks}] skip {chunk_id} ({dur:.1f}s)")
                continue

            print(f"  [{idx}/{total_chunks}] {chunk_id} ({len(txt)}c)…", end="", flush=True)
            ok = await generate_audio(txt, chunk_path, voice, rate)
            if not ok:
                print(" retry…", end="", flush=True)
                ok = await generate_audio(txt, chunk_path, voice, rate)

            if ok:
                dur = get_duration(chunk_path)
                print(f" ✓ {dur:.1f}s")
                done.add(chunk_id)
                if len(done) % 10 == 0:
                    save_progress(output_dir, done)
            else:
                print(" ✗")

    save_progress(output_dir, done)

    print(f"\n  Saving chunk→text mapping…")
    mapping: dict[str, str] = {}
    for ch in all_chapters:
        for i, txt in enumerate(ch.chunks):
            chunk_id = f"{ch.id}_c{i:03d}"
            mapping[chunk_id] = txt
    mapping_path = os.path.join(output_dir, "chunks_map.json")
    with open(mapping_path, 'w', encoding='utf-8') as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)
    print(f"  → {mapping_path} ({len(mapping)} entries)\n")

    print(f"{'=' * 50}")
    print("Step 4: Merge chapters")
    print("=" * 50)

    chapter_audio: list[tuple[str, str]] = []
    for ch in all_chapters:
        paths = []
        for i in range(len(ch.chunks)):
            p = os.path.join(chunks_dir, f"{ch.id}_c{i:03d}.mp3")
            if os.path.exists(p):
                paths.append(p)
        if not paths:
            continue

        merged = os.path.join(chapters_dir, f"{ch.id}.m4a")
        if merge_chunks(paths, merged):
            dur = get_duration(merged)
            print(f"  ✓ {ch.id:25s} {dur/60:.1f}min ({len(paths)} chunks)")
            chapter_audio.append((ch.title, merged))

    print(f"\n  {len(chapter_audio)} chapters merged\n")

    print("=" * 50)
    print("Step 5: Build audiobook")
    print("=" * 50)

    if not chapter_audio:
        print("  No audio to export!")
        return

    book_title = "重探戰後臺灣政治史：美國、國民黨政府與臺灣社會的三方角力"
    book_author = "陳翠蓮"

    m4b_path = os.path.join(output_dir, f"{book_title}.m4b")
    if not build_m4b(chapter_audio, m4b_path, book_title, book_author, cover_path):
        print("  M4B failed → falling back to MP3")
        mp3_path = os.path.join(output_dir, f"{book_title}.mp3")
        build_mp3(chapter_audio, mp3_path, book_title, book_author)


def main() -> None:
    parser = argparse.ArgumentParser(description="EPUB → Audiobook (M4B)")
    parser.add_argument("epub", help="Path to EPUB file")
    parser.add_argument("-o", "--output", default="output", help="Output directory")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="edge-tts voice name")
    parser.add_argument("--rate", default=DEFAULT_RATE, help="Speech rate (e.g. +10%%)")
    parser.add_argument("--sample", action="store_true", help="Only first 2 chapters")
    args = parser.parse_args()

    asyncio.run(run(args.epub, args.output, args.voice, args.rate, args.sample))


if __name__ == "__main__":
    main()
