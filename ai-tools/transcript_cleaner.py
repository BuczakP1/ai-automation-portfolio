"""
Transcript Cleaner v2
=====================
Drop raw transcripts into D:\Desktop\TEXT and run this script.
It processes every .txt file (flat or in subfolders), cleans it
with Claude, and saves the cleaned version right next to the original.

Skips files that already have a cleaned version.

Usage:
    py -3.12 transcript_cleaner.py
    py -3.12 transcript_cleaner.py --single "D:\Desktop\TEXT\episode1.txt"
    py -3.12 transcript_cleaner.py --input "D:\Desktop\some_other_folder"

Requirements:
    pip install anthropic
    API key in D:\Desktop\api_key.txt
"""

import sys
import time
import argparse
from pathlib import Path
from datetime import datetime

# ─── SETTINGS ────────────────────────────────────────────────────────────────

API_KEY_PATH = r"D:\Desktop\api_key.txt"
INPUT_FOLDER = r"D:\Desktop\Transcripts\output"
WORDS_PER_CHUNK = 5000

# ─── FUNCTIONS ───────────────────────────────────────────────────────────────

def load_api_key() -> str:
    key_path = Path(API_KEY_PATH)
    if not key_path.exists():
        print(f"\n  ERROR: API key file not found at {API_KEY_PATH}")
        sys.exit(1)
    return key_path.read_text().strip()


def find_transcripts(folder: str) -> list[Path]:
    """Find all .txt files that haven't been cleaned yet.
    Searches both flat files and files inside subfolders."""
    folder_path = Path(folder)
    transcripts = []

    if not folder_path.exists():
        print(f"\n  ERROR: Folder not found: {folder}")
        sys.exit(1)

    # Find all .txt files recursively (flat + subfolders)
    for file_path in sorted(folder_path.rglob("*.txt")):
        # Skip cleaned files
        if "- CLEANED" in file_path.name:
            continue

        # Skip tiny files
        if file_path.stat().st_size < 100:
            continue

        # Check if cleaned version already exists in same folder
        cleaned_name = file_path.stem + " - CLEANED.txt"
        cleaned_path = file_path.parent / cleaned_name
        if cleaned_path.exists():
            continue

        transcripts.append(file_path)

    return transcripts


def chunk_text(text: str) -> list[str]:
    """Split text into chunks of roughly WORDS_PER_CHUNK words."""
    words = text.split()
    chunks = []

    for i in range(0, len(words), WORDS_PER_CHUNK):
        chunk = " ".join(words[i:i + WORDS_PER_CHUNK])
        chunks.append(chunk)

    return chunks


def clean_chunk(client, chunk_text: str, chunk_num: int, total_chunks: int) -> str:
    """Send one chunk to Claude for cleaning."""

    prompt = f"""You are an expert transcript editor. Transform this conversational podcast transcript into polished, readable content while preserving the speaker's authentic voice and message.

WHAT TO DO:
1. Remove excessive filler words: 'you know', 'like', 'um', 'uh', 'so', 'right', 'yeah' (keep occasional ones for natural flow)
2. Fix repetitive phrases and false starts
3. Break run-on sentences into clear, digestible sentences
4. Add paragraph breaks for readability (every 3-5 sentences)
5. Fix obvious grammar issues while keeping the conversational tone
6. Make speaker transitions clear
7. Add brief context for platform-specific references (e.g., 'For You page' becomes 'TikTok For You page')
8. Clarify vague pronouns and references when context is available

WHAT NOT TO DO:
- Do NOT change the meaning or opinions expressed
- Do NOT add your own commentary or introduction
- Do NOT remove interesting tangents or personality
- Do NOT make it sound corporate or robotic
- Keep any spiritual, faith, or theological language exactly as spoken

This is chunk {chunk_num} of {total_chunks}.

TRANSCRIPT:
{chunk_text}

Return ONLY the cleaned transcript. No preamble, no commentary."""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}]
    )

    return response.content[0].text


def clean_transcript(client, file_path: Path) -> str:
    """Clean an entire transcript file, chunking if needed."""

    text = file_path.read_text(encoding="utf-8", errors="ignore")
    word_count = len(text.split())

    print(f"    Words: {word_count:,}")

    # Chunk the text
    chunks = chunk_text(text)
    print(f"    Chunks: {len(chunks)}")

    # Clean each chunk
    cleaned_chunks = []

    for i, chunk in enumerate(chunks, 1):
        print(f"    Cleaning {i}/{len(chunks)}...", end=" ", flush=True)

        retries = 0
        max_retries = 3

        while retries < max_retries:
            try:
                cleaned = clean_chunk(client, chunk, i, len(chunks))
                cleaned_chunks.append(cleaned)
                print("done")
                break
            except Exception as e:
                retries += 1
                if retries < max_retries:
                    wait_time = 5 * retries
                    print(f"error, retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    print(f"FAILED after {max_retries} attempts")
                    print(f"    Using original chunk instead")
                    cleaned_chunks.append(chunk)

        # Wait between chunks to avoid rate limits
        if i < len(chunks):
            time.sleep(1)

    # Merge all cleaned chunks
    return "\n\n".join(cleaned_chunks)


def save_cleaned(file_path: Path, cleaned_text: str) -> Path:
    """Save cleaned transcript next to the original with ' - CLEANED' in name."""
    cleaned_name = file_path.stem + " - CLEANED.txt"
    cleaned_path = file_path.parent / cleaned_name

    cleaned_path.write_text(cleaned_text, encoding="utf-8")

    original_words = len(file_path.read_text(encoding="utf-8", errors="ignore").split())
    cleaned_words = len(cleaned_text.split())
    size_kb = cleaned_path.stat().st_size / 1024

    print(f"    Saved: {cleaned_name}")
    print(f"    Location: {cleaned_path.parent}")
    print(f"    Words: {original_words:,} -> {cleaned_words:,} ({cleaned_words - original_words:+,})")
    print(f"    Size: {size_kb:.1f}KB")

    return cleaned_path


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Clean podcast transcripts with Claude")
    parser.add_argument("--input", default=INPUT_FOLDER, help="Folder with .txt transcripts")
    parser.add_argument("--single", default=None, help="Clean a single file")
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("  TRANSCRIPT CLEANER v2")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Input: {args.input}")
    print(f"  Chunks: {WORDS_PER_CHUNK:,} words each")
    print("=" * 60)

    # Load API key and connect
    api_key = load_api_key()

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
        print("\n  Claude API connected.")
    except ImportError:
        print("\n  ERROR: pip install anthropic")
        sys.exit(1)

    # Find transcripts
    if args.single:
        single_path = Path(args.single)
        if not single_path.exists():
            print(f"\n  ERROR: File not found: {args.single}")
            sys.exit(1)
        transcripts = [single_path]
    else:
        transcripts = find_transcripts(args.input)

    if not transcripts:
        print("\n  No transcripts to clean!")
        print(f"  (Files with ' - CLEANED' versions are skipped)")
        sys.exit(0)

    # Show queue
    print(f"\n  Queue: {len(transcripts)} transcript(s)")
    print(f"  {'-' * 50}")
    for i, t in enumerate(transcripts, 1):
        # Show relative path from input folder for clarity
        try:
            rel = t.relative_to(args.input)
        except ValueError:
            rel = t.name
        size_kb = t.stat().st_size / 1024
        print(f"  {i:>3}. {rel} ({size_kb:.1f}KB)")
    print(f"  {'-' * 50}")

    total_words = sum(len(t.read_text(encoding="utf-8", errors="ignore").split()) for t in transcripts)
    total_chunks = sum(max(1, len(t.read_text(encoding="utf-8", errors="ignore").split()) // WORDS_PER_CHUNK) for t in transcripts)
    est_cost = total_chunks * 0.02

    print(f"  Total words: {total_words:,}")
    print(f"  Total chunks: ~{total_chunks}")
    print(f"  Estimated cost: ~${est_cost:.2f}")

    input("\n  Press ENTER to start (or Ctrl+C to cancel)...")

    # Process queue
    results = []
    failed = []

    for i, file_path in enumerate(transcripts, 1):
        try:
            rel = file_path.relative_to(args.input)
        except ValueError:
            rel = file_path.name

        print(f"\n  [{i}/{len(transcripts)}] {rel}")

        try:
            cleaned_text = clean_transcript(client, file_path)
            saved_path = save_cleaned(file_path, cleaned_text)

            results.append({
                "original": str(rel),
                "saved": saved_path.name,
            })
        except Exception as e:
            print(f"    FAILED: {e}")
            failed.append(str(rel))

        if i < len(transcripts):
            print("    Next file in 2 seconds...")
            time.sleep(2)

    # Summary
    print(f"\n  {'=' * 60}")
    print(f"  TRANSCRIPT CLEANER — COMPLETE")
    print(f"  {'=' * 60}")
    print(f"  Cleaned: {len(results)}")
    if failed:
        print(f"  Failed: {len(failed)}")
        for f in failed:
            print(f"    - {f}")
    print()
    for r in results:
        print(f"    {r['original']} -> {r['saved']}")
    print(f"  {'=' * 60}\n")


if __name__ == "__main__":
    main()
