"""
LinkedIn Code Content Generator
================================
Reads Python scripts and JSON workflows from a folder and generates
28 LinkedIn posts per file. Each post is standalone, written for
normal people, and shows the value of what you built.

Usage:
    py -3.12 linkedin_code_content.py
    py -3.12 linkedin_code_content.py --input "D:\Desktop\All Codes"
    py -3.12 linkedin_code_content.py --single "D:\Desktop\transcriber.py"

Requirements:
    pip install anthropic
    API key in D:\Desktop\api_key.txt
"""

import os
import sys
import json
import time
import argparse
from pathlib import Path
from datetime import datetime

# ─── SETTINGS ────────────────────────────────────────────────────────────────

API_KEY_PATH = r"D:\Desktop\api_key.txt"
DEFAULT_INPUT_FOLDER = r"D:\Desktop\All Codes"
DEFAULT_OUTPUT_FOLDER = r"D:\Desktop\LinkedIn_Posts"

SKIP_FILES = {
    "linkedin_code_content.py",
    "test.py",
    "setup.py",
    "__init__.py",
    "config.py",
}

ALLOWED_EXTENSIONS = {".py", ".json"}

# ─── THEMES ──────────────────────────────────────────────────────────────────
# Simple list of themes to mix across posts. NOT per-post instructions.
THEMES = [
    "time saved", "money saved", "scale", "accuracy", "who benefits",
    "small business angle", "simple explanation", "bold opinion",
    "industry trend", "analogy", "lesson learned", "question to audience",
    "ownership/local/privacy", "future of this space", "what could go wrong manually",
]

# ─── FUNCTIONS ───────────────────────────────────────────────────────────────

def load_api_key() -> str:
    key_path = Path(API_KEY_PATH)
    if not key_path.exists():
        print(f"\n  ERROR: API key file not found at {API_KEY_PATH}")
        print(f"  Create the file with your Anthropic API key (just the key, nothing else)")
        sys.exit(1)
    key = key_path.read_text().strip()
    if not key.startswith("sk-"):
        print(f"\n  WARNING: API key doesn't start with 'sk-' - might be wrong")
    return key


def find_scripts(folder: str) -> list[dict]:
    folder_path = Path(folder)
    scripts = []
    for file_path in sorted(folder_path.iterdir()):
        if file_path.is_dir():
            continue
        if file_path.name.startswith(".") or file_path.name.startswith("_"):
            continue
        if file_path.name.lower() in SKIP_FILES:
            continue
        if file_path.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
            if len(content.strip()) < 50:
                continue
            scripts.append({
                "name": file_path.name,
                "path": str(file_path),
                "content": content,
                "size_kb": round(file_path.stat().st_size / 1024, 1),
            })
        except Exception as e:
            print(f"  Could not read {file_path.name}: {e}")
    return scripts


def get_already_done(output_folder: str) -> set:
    output_path = Path(output_folder)
    done = set()
    if not output_path.exists():
        return done
    for file_path in output_path.iterdir():
        if file_path.name.endswith("_28_posts.txt"):
            base_name = file_path.name.replace("_28_posts.txt", "")
            done.add(f"{base_name}.py")
            done.add(f"{base_name}.json")
    return done


def get_script_summary(client, script: dict) -> str:
    content = script['content']
    chunk_size = 50000

    if len(content) <= chunk_size:
        prompt = f"""You are analyzing a script/file to create LinkedIn content.

FILE NAME: {script['name']}
FILE CONTENT:
```
{content}
```

Summarize in 4-6 sentences:
1. What this script does (in plain English, no jargon)
2. ALL the key features and capabilities — be specific about what it actually does
3. Who would benefit from it
4. What problem it solves
5. How it works at a high level (manual activation? downloads data? scrapes websites?)

Be ACCURATE. Do not exaggerate or assume features that aren't in the code."""

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text

    chunks = []
    for i in range(0, len(content), chunk_size):
        chunks.append(content[i:i + chunk_size])

    print(f"    Large file ({len(content):,} chars) — reading in {len(chunks)} parts...")

    chunk_summaries = []
    for i, chunk in enumerate(chunks, 1):
        print(f"    Reading part {i}/{len(chunks)}...")
        prompt = f"""You are analyzing PART {i} of {len(chunks)} of a file called {script['name']}.

FILE CONTENT (PART {i}):
```
{chunk}
```

Summarize what THIS PART does in 3-4 sentences. Be specific and accurate."""

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        chunk_summaries.append(response.content[0].text)
        time.sleep(0.5)

    all_summaries = "\n\n".join([f"PART {i+1}: {s}" for i, s in enumerate(chunk_summaries)])

    merge_prompt = f"""You analyzed a large file called {script['name']} in {len(chunks)} parts:

{all_summaries}

Combine into ONE summary (4-6 sentences):
1. What this file does overall (plain English)
2. ALL key features
3. Who benefits
4. What problem it solves
5. How it works (manual vs automatic, data sources, etc.)

Be accurate. Do not exaggerate."""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        messages=[{"role": "user", "content": merge_prompt}]
    )
    return response.content[0].text


def generate_posts(client, script: dict, angles: list[str]) -> list[dict]:
    posts = []

    try:
        script_summary = get_script_summary(client, script)
    except Exception as e:
        print(f"    ERROR getting script summary: {e}")
        return []

    print(f"    Script understood. Generating 28 posts...")

    themes_str = ", ".join(THEMES)

    for batch_start in range(0, 28, 7):
        batch_end = min(batch_start + 7, 28)
        batch_size = batch_end - batch_start

        batch_prompt = f"""Write {batch_size} LinkedIn posts about a tool I built.

WHAT THE TOOL DOES:
{script_summary}

RULES:
- Each post is COMPLETELY STANDALONE. Someone reading only that one post understands it fully.
- No two posts should read the same way. Mix up the themes: {themes_str}.
- No two posts should open the same way. Vary the first line every time.
- No two posts should have the same structure. Some should be short observations, some should be stories, some should be questions, some should be bold claims, some should be analogies.
- 100-200 words per post. Short paragraphs with line breaks.
- Strong opening line that makes people click "see more."
- THE FOLD: LinkedIn only shows the first 2-3 lines before cutting off with "see more." Those first lines are EVERYTHING. They must hook the reader — a bold claim, a surprising number, a question, a contradiction. The rest of the post lives or dies on whether the fold is good enough to click. Write the fold FIRST, then the body.
- 2-3 hashtags at the end.
- Written for normal people. NEVER mention Python, API, scripts, code, functions, algorithms, JSON.
- Say "tool", "system", "automated process", "software I built."
- DO NOT list what the tool outputs. Show one angle per post, not a feature dump.
- DO NOT invent personal experiences. Don't say "I spent hours doing X" or "A friend asked me." Only describe what the tool does.
- For problems, say "This process typically takes businesses hours" NOT "I was frustrated by this."
- Voice: confident, understated, real. No corporate buzzwords. No "DM me" or pitches.
- Minimal emojis. Professional LinkedIn.
- 2-3 posts out of the full 28 can mention it runs locally, no cloud, no subscription — but only when natural. Don't force it.

{"This is batch " + str(batch_start // 7 + 1) + " of 4. Make sure these " + str(batch_size) + " posts are each different from each other." if batch_start > 0 else ""}

FORMAT — follow this exactly:
===POST {batch_start + 1}===
[post content]

===POST {batch_start + 2}===
[post content]

(continue for all {batch_size} posts)"""

        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4000,
                messages=[{"role": "user", "content": batch_prompt}]
            )

            response_text = response.content[0].text

            for i in range(batch_start, batch_end):
                post_num = i + 1
                marker = f"===POST {post_num}==="
                next_marker = f"===POST {post_num + 1}==="

                if marker in response_text:
                    start = response_text.index(marker) + len(marker)
                    end = response_text.index(next_marker) if next_marker in response_text else len(response_text)
                    post_text = response_text[start:end].strip()

                    if post_text:
                        posts.append({
                            "post_number": post_num,
                            "angle": f"Mixed {post_num}",
                            "content": post_text,
                        })

            time.sleep(1)

        except Exception as e:
            print(f"    ERROR generating batch {batch_start+1}-{batch_end}: {e}")
            time.sleep(3)

    return posts


def save_posts(script_name: str, posts: list[dict], output_folder: str):
    output_path = Path(output_folder)
    output_path.mkdir(parents=True, exist_ok=True)

    clean_name = script_name.replace(".py", "").replace(".json", "").replace(" ", "_")
    file_path = output_path / f"{clean_name}_28_posts.txt"

    lines = []
    lines.append(f"{'='*60}")
    lines.append(f"LINKEDIN POSTS FOR: {script_name}")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"Total posts: {len(posts)}")
    lines.append(f"{'='*60}\n")

    for post in posts:
        lines.append(f"--- DAY {post['post_number']} | Angle: {post['angle']} ---\n")
        lines.append(post['content'])
        lines.append(f"\n{'─'*40}\n")

    file_path.write_text("\n".join(lines), encoding="utf-8")
    return str(file_path)


def save_summary(all_results: list[dict], output_folder: str):
    output_path = Path(output_folder) / "_SUMMARY.txt"

    lines = []
    lines.append(f"{'='*60}")
    lines.append(f"LINKEDIN CONTENT GENERATION SUMMARY")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"{'='*60}\n")

    total_posts = 0
    for result in all_results:
        total_posts += result['post_count']
        lines.append(f"  {result['script']}: {result['post_count']} posts -> {result['file']}")

    lines.append(f"\n  TOTAL: {total_posts} posts from {len(all_results)} scripts")
    lines.append(f"  That's {total_posts} days of LinkedIn content!")
    lines.append(f"\n  HOW TO USE:")
    lines.append(f"  1. Open any _28_posts.txt file")
    lines.append(f"  2. Copy one post per day")
    lines.append(f"  3. Paste into LinkedIn")
    lines.append(f"  4. Add a relevant image if you have one")
    lines.append(f"  5. Post and engage with comments")

    output_path.write_text("\n".join(lines), encoding="utf-8")


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate 28 LinkedIn posts per script")
    parser.add_argument("--input", default=DEFAULT_INPUT_FOLDER, help="Folder with scripts")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_FOLDER, help="Folder to save posts")
    parser.add_argument("--single", default=None, help="Process a single file")
    args = parser.parse_args()

    print("\n" + "="*60)
    print("  LINKEDIN CODE CONTENT GENERATOR")
    print("  28 standalone posts per script")
    print("  1 post per day = years of content")
    print("="*60)

    print("\n  Loading API key...")
    api_key = load_api_key()
    print("  API key loaded.")

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
        print("  Claude API connected.")
    except ImportError:
        print("\n  ERROR: 'anthropic' package not installed.")
        print("  Run: pip install anthropic")
        sys.exit(1)

    if args.single:
        single_path = Path(args.single)
        if not single_path.exists():
            print(f"\n  ERROR: File not found: {args.single}")
            sys.exit(1)
        content = single_path.read_text(encoding="utf-8", errors="ignore")
        scripts = [{
            "name": single_path.name,
            "path": str(single_path),
            "content": content,
            "size_kb": round(single_path.stat().st_size / 1024, 1),
        }]
    else:
        print(f"\n  Scanning: {args.input}")
        scripts = find_scripts(args.input)

    if not scripts:
        print("\n  No scripts found! Check your input folder.")
        sys.exit(1)

    already_done = get_already_done(args.output)
    before_count = len(scripts)
    scripts = [s for s in scripts if s['name'] not in already_done]
    skipped = before_count - len(scripts)

    if skipped > 0:
        print(f"\n  Skipping {skipped} scripts (posts already exist)")

    if not scripts:
        print("\n  All scripts already have posts! Delete .txt files to regenerate.")
        sys.exit(0)

    print(f"\n  {len(scripts)} scripts to process:")
    for s in scripts:
        print(f"    - {s['name']} ({s['size_kb']}KB)")

    print(f"\n  Will generate {len(scripts) * 28} LinkedIn posts.")
    print(f"  Estimated API cost: ~${len(scripts) * 0.04:.2f}")
    print(f"  Output: {args.output}")

    input("\n  Press ENTER to start (or Ctrl+C to cancel)...")

    all_results = []

    for i, script in enumerate(scripts, 1):
        print(f"\n  [{i}/{len(scripts)}] Processing: {script['name']}")

        posts = generate_posts(client, script, THEMES)

        if posts:
            saved_path = save_posts(script['name'], posts, args.output)
            print(f"    Saved {len(posts)} posts to {saved_path}")
            all_results.append({
                "script": script['name'],
                "post_count": len(posts),
                "file": saved_path,
            })
        else:
            print(f"    WARNING: No posts generated for {script['name']}")

        if i < len(scripts):
            print("    Waiting 2 seconds...")
            time.sleep(2)

    if all_results:
        save_summary(all_results, args.output)

    total = sum(r['post_count'] for r in all_results)
    print(f"\n  {'='*60}")
    print(f"  DONE!")
    print(f"  Generated {total} LinkedIn posts from {len(all_results)} scripts")
    print(f"  That's {total} days of content ({total // 30} months!)")
    print(f"  Files saved to: {args.output}")
    print(f"  {'='*60}\n")


if __name__ == "__main__":
    main()
