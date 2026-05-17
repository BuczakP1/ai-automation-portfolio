"""
build_knowledge_base.py
=======================
Single pipeline to build a deep visual + transcript knowledge base
from trading YouTube channels.

For each video:
  1. Download video (480p) + transcript via yt-dlp
  2. Extract 1 frame every 2 seconds via ffmpeg
  3. Analyse every frame with Claude vision — describe charts, code, numbers on screen
  4. Combine transcript + visual analysis into one rich .md file
  5. Delete video + frames — only the .md stays forever

OUTPUT: Knowledge Base/<Channel>/<Video Title>.md

CHANNELS:
  @moondevonyt       — Moon Dev        (~271 videos)
  @DataTraders       — Data Trader     (~54 videos)
  @parttimelarry     — Part Time Larry (~200 videos)
  @tradealgorithm    — Trade Algorithm (~35 videos)
  @TradingLabOfficial — TradingLab     (~95 videos)

USAGE:
  # List all videos + status
  python build_knowledge_base.py --list

  # Process one video (test first)
  python build_knowledge_base.py --match "liquidation bot"

  # Process one full channel
  python build_knowledge_base.py --channel moondev

  # Process everything (run overnight)
  python build_knowledge_base.py --all
"""

import os
import sys
import glob
import json
import time
import shutil
import argparse
import subprocess
import base64
from pathlib import Path
from datetime import datetime

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

BASE_DIR    = "D:/Desktop/Trading Folder"
KB_DIR      = f"{BASE_DIR}/Knowledge Base"
TEMP_DIR    = f"{BASE_DIR}/_kb_temp"

CHANNELS = {
    "moondev":    ("https://www.youtube.com/@moondevonyt",        "Moon Dev"),
    "datatrader": ("https://www.youtube.com/@DataTraders",        "Data Trader"),
    "larry":      ("https://www.youtube.com/@parttimelarry",      "Part Time Larry"),
    "tradealgo":  ("https://www.youtube.com/@tradealgorithm",     "Trade Algorithm"),
    "tradinglab": ("https://www.youtube.com/@TradingLabOfficial", "TradingLab"),
}

FRAME_INTERVAL   = 2      # seconds between frames
MAX_FRAMES       = 1200   # cap per video (~40 min at 2s intervals)
VIDEO_HEIGHT     = "480"  # download quality — enough for reading screens


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def sanitize(title: str, maxlen: int = 100) -> str:
    keep = set(' abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_()')
    return ''.join(c if c in keep else '_' for c in title)[:maxlen].strip()


def get_client():
    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        # Try config.py
        try:
            sys.path.insert(0, BASE_DIR)
            import config
            key = getattr(config, 'ANTHROPIC_API_KEY', None)
        except ImportError:
            pass
    if not key:
        raise ValueError("ANTHROPIC_API_KEY not found in env or config.py")
    return anthropic.Anthropic(api_key=key)


def output_path(channel_name: str, title: str) -> str:
    channel_dir = f"{KB_DIR}/{channel_name}"
    os.makedirs(channel_dir, exist_ok=True)
    return f"{channel_dir}/{sanitize(title)}.md"


def is_done(channel_name: str, title: str) -> bool:
    return os.path.exists(output_path(channel_name, title))


# ─────────────────────────────────────────────
# STEP 1: FETCH VIDEO LIST
# ─────────────────────────────────────────────

def fetch_video_list(channel_url: str, channel_name: str) -> list[dict]:
    """Get list of all videos from a channel without downloading."""
    print(f"\n[FETCH] Getting video list for {channel_name}...")
    cmd = [
        'yt-dlp',
        '--flat-playlist',
        '--print', '%(id)s\t%(title)s',
        '--no-warnings',
        channel_url
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[ERROR] Could not fetch video list: {result.stderr[:200]}")
        return []

    videos = []
    for line in result.stdout.strip().split('\n'):
        if '\t' not in line:
            continue
        vid_id, title = line.split('\t', 1)
        videos.append({
            'id':           vid_id,
            'title':        title.strip(),
            'url':          f"https://www.youtube.com/watch?v={vid_id}",
            'channel_name': channel_name,
        })

    print(f"[FETCH] Found {len(videos)} videos in {channel_name}")
    return videos


# ─────────────────────────────────────────────
# STEP 2: DOWNLOAD VIDEO + TRANSCRIPT
# ─────────────────────────────────────────────

def download_video(video: dict, work_dir: str) -> tuple[str | None, str]:
    """Download video file and transcript. Returns (video_path, transcript_text)."""
    url   = video['url']
    title = video['title']
    safe  = sanitize(title)

    os.makedirs(work_dir, exist_ok=True)

    # Download video + auto-subtitles/transcript
    cmd = [
        'yt-dlp',
        '-f', f'bestvideo[height<={VIDEO_HEIGHT}][ext=mp4]+bestaudio[ext=m4a]/best[height<={VIDEO_HEIGHT}][ext=mp4]/best[height<={VIDEO_HEIGHT}]',
        '--merge-output-format', 'mp4',
        '--write-auto-sub', '--sub-lang', 'en',
        '--convert-subs', 'srt',
        '--no-playlist',
        '-o', f'{work_dir}/{safe}.%(ext)s',
        '--quiet', '--no-warnings',
        url
    ]
    print(f"  [DOWNLOAD] {title[:65]}...")
    result = subprocess.run(cmd, capture_output=True, text=True)

    # Find video file
    video_path = None
    for ext in ['mp4', 'mkv', 'webm']:
        p = f"{work_dir}/{safe}.{ext}"
        if os.path.exists(p):
            video_path = p
            break
    if not video_path:
        matches = glob.glob(f"{work_dir}/{safe}.*")
        for m in matches:
            if any(m.endswith(e) for e in ['.mp4', '.mkv', '.webm']):
                video_path = m
                break

    if not video_path:
        print(f"  [ERROR] Video file not found after download")
        return None, ""

    # Read transcript/subtitles if downloaded
    transcript = ""
    for srt in glob.glob(f"{work_dir}/*.srt") + glob.glob(f"{work_dir}/*.vtt"):
        try:
            with open(srt, encoding='utf-8') as f:
                raw = f.read()
            # Strip SRT timestamps/numbers — keep text only
            import re
            lines = raw.split('\n')
            text_lines = [l.strip() for l in lines
                          if l.strip()
                          and not re.match(r'^\d+$', l.strip())
                          and not re.match(r'[\d:,\s]+-->', l)]
            transcript = ' '.join(text_lines)
            break
        except:
            pass

    size_mb = os.path.getsize(video_path) / 1024 / 1024
    print(f"  [DOWNLOAD] OK — {size_mb:.0f}MB | transcript: {'yes' if transcript else 'no'}")
    return video_path, transcript


# ─────────────────────────────────────────────
# STEP 3: EXTRACT FRAMES
# ─────────────────────────────────────────────

def extract_frames(video_path: str, frames_dir: str) -> list[str]:
    """Extract 1 frame every FRAME_INTERVAL seconds."""
    os.makedirs(frames_dir, exist_ok=True)
    fps = 1.0 / FRAME_INTERVAL

    cmd = [
        'ffmpeg', '-i', video_path,
        '-vf', f'fps={fps},scale=1280:-2',
        '-q:v', '3',
        f'{frames_dir}/frame_%05d.jpg',
        '-hide_banner', '-loglevel', 'error'
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  [ERROR] ffmpeg: {result.stderr[:200]}")
        return []

    frames = sorted(glob.glob(f"{frames_dir}/frame_*.jpg"))
    if len(frames) > MAX_FRAMES:
        frames = frames[:MAX_FRAMES]

    print(f"  [FRAMES] {len(frames)} frames extracted (1 per {FRAME_INTERVAL}s)")
    return frames


# ─────────────────────────────────────────────
# STEP 4: CLAUDE VISION ANALYSIS
# ─────────────────────────────────────────────

def analyse_frame(client, frame_path: str, frame_num: int, total: int,
                  title: str, prev: str = "") -> str:
    ts_sec = frame_num * FRAME_INTERVAL
    ts_str = f"{ts_sec // 60}m{ts_sec % 60:02d}s"
    context = f"Previous frame: {prev[:150]}" if prev else "First frame."

    prompt = f"""Video: "{title}" | Frame {frame_num}/{total} at {ts_str}
{context}

Describe EVERYTHING visible:
- Charts: asset name, timeframe, indicators, price levels, patterns, entries/exits, exact numbers
- Code: copy exact variable names, parameters, logic, strategy settings shown
- Terminal output: copy the text
- Backtest results: exact return%, Sharpe, drawdown, trade count, all numbers shown
- Dashboards/websites: what data is displayed
- Presenter pointing at something: what they are highlighting
- Any settings, thresholds, or configuration values visible

Be specific. Numbers matter. This replaces the video permanently."""

    try:
        with open(frame_path, 'rb') as f:
            img_data = base64.standard_b64encode(f.read()).decode('utf-8')

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_data}},
                    {"type": "text", "text": prompt}
                ]
            }]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"[frame error: {e}]"


def analyse_frames(client, frames: list[str], title: str) -> list[dict]:
    results = []
    prev    = ""
    for i, path in enumerate(frames, 1):
        ts = i * FRAME_INTERVAL
        ts_str = f"{ts // 60}m{ts % 60:02d}s"
        print(f"  [VISION] {i}/{len(frames)} ({ts_str})...    ", end='\r')
        desc = analyse_frame(client, path, i, len(frames), title, prev)
        results.append({'frame': i, 'timestamp': ts_str, 'description': desc})
        prev = desc
        time.sleep(0.25)
    print()
    return results


# ─────────────────────────────────────────────
# STEP 5: WRITE OUTPUT
# ─────────────────────────────────────────────

def write_output(video: dict, frame_analyses: list[dict], transcript: str, client) -> str:
    title        = video['title']
    channel_name = video['channel_name']
    out_path     = output_path(channel_name, title)

    # Build full frame log text
    frame_log = "\n".join(f"[{r['timestamp']}] {r['description']}" for r in frame_analyses)

    # Generate summary with Claude
    print("  [SUMMARY] Generating summary...")
    summary = ""
    try:
        transcript_snippet = transcript[:3000] if transcript else ""
        msg = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": f"""You are building a permanent trading knowledge base entry for the video: "{title}"

Based on the frame-by-frame visual analysis and transcript below, write a comprehensive knowledge base entry covering:

1. **Core Concept** — what strategy, tool, or idea is being taught
2. **Exact Parameters & Settings** — every number, threshold, setting shown or mentioned
3. **Code & Logic** — any code shown, with variable names and values
4. **Backtest Results** — exact figures if shown (return%, Sharpe, drawdown, trades)
5. **Step-by-Step Implementation** — how to actually build/use what's shown
6. **Key Insights** — non-obvious takeaways from what was shown on screen
7. **APIs / Libraries / Tools** — specific tools demonstrated

Visual frame log (what was shown on screen):
{frame_log[:6000]}

Transcript:
{transcript_snippet}

Write in clear, detailed markdown. Be specific — exact numbers and parameters matter."""}]
        )
        summary = msg.content[0].text.strip()
    except Exception as e:
        summary = f"[Summary failed: {e}]"

    # Write the file
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(f"# {title}\n\n")
        f.write(f"**Channel:** {channel_name}  \n")
        f.write(f"**URL:** {video['url']}  \n")
        f.write(f"**Processed:** {datetime.now().strftime('%Y-%m-%d')}  \n")
        f.write(f"**Frames analysed:** {len(frame_analyses)} (1 per {FRAME_INTERVAL}s)  \n\n")
        f.write("---\n\n")
        f.write("## Knowledge Base Entry\n\n")
        f.write(summary)
        f.write("\n\n---\n\n")
        f.write("## Frame-by-Frame Visual Log\n\n")
        for r in frame_analyses:
            f.write(f"**[{r['timestamp']}]** {r['description']}\n\n")

    print(f"  [SAVED] {out_path}")
    return out_path


# ─────────────────────────────────────────────
# FULL PIPELINE FOR ONE VIDEO
# ─────────────────────────────────────────────

def process_video(video: dict, client, force: bool = False):
    title        = video['title']
    channel_name = video['channel_name']

    if is_done(channel_name, title) and not force:
        print(f"  [SKIP] Already done: {title[:60]}")
        return

    work_dir   = f"{TEMP_DIR}/{sanitize(title)}"
    frames_dir = f"{work_dir}/frames"

    try:
        # 1. Download
        video_path, transcript = download_video(video, work_dir)
        if not video_path:
            return

        # 2. Extract frames
        frames = extract_frames(video_path, frames_dir)
        if not frames:
            return

        # 3. Analyse frames
        frame_analyses = analyse_frames(client, frames, title)

        # 4. Write output
        write_output(video, frame_analyses, transcript, client)

    except KeyboardInterrupt:
        print("\n[INTERRUPTED]")
        raise
    except Exception as e:
        print(f"  [ERROR] {title[:60]}: {e}")
    finally:
        # 5. Clean up temp — video + frames deleted, only .md remains
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Build visual trading knowledge base from YouTube')
    parser.add_argument('--all',     action='store_true', help='Process all channels')
    parser.add_argument('--channel', type=str, default='', help='Process one channel: moondev / datatrader / larry / tradealgo / tradinglab')
    parser.add_argument('--match',   type=str, default='', help='Process videos matching keyword')
    parser.add_argument('--force',   action='store_true', help='Re-process even if already done')
    parser.add_argument('--list',    action='store_true', help='List all videos and status')
    args = parser.parse_args()

    os.makedirs(KB_DIR, exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)

    # Build video list
    all_videos = []
    channels_to_scan = CHANNELS.items()
    if args.channel:
        if args.channel not in CHANNELS:
            print(f"Unknown channel. Options: {', '.join(CHANNELS.keys())}")
            return
        channels_to_scan = [(args.channel, CHANNELS[args.channel])]

    for key, (url, name) in channels_to_scan:
        videos = fetch_video_list(url, name)
        for v in videos:
            v['channel_key'] = key
        all_videos.extend(videos)

    if args.match:
        kw = args.match.lower()
        all_videos = [v for v in all_videos if kw in v['title'].lower()]
        print(f"\nMatched {len(all_videos)} videos for '{args.match}'")

    if args.list:
        done  = sum(1 for v in all_videos if is_done(v['channel_name'], v['title']))
        print(f"\n{len(all_videos)} videos | {done} done | {len(all_videos)-done} remaining\n")
        for v in all_videos:
            status = '[DONE]' if is_done(v['channel_name'], v['title']) else '[TODO]'
            print(f"{status} [{v['channel_name'][:12]:<12}] {v['title'][:70]}")
        return

    if not args.all and not args.channel and not args.match:
        parser.print_help()
        return

    # Filter to unprocessed
    targets = [v for v in all_videos if not is_done(v['channel_name'], v['title']) or args.force]
    print(f"\n{len(targets)} videos to process...")

    client = get_client()

    for i, video in enumerate(targets, 1):
        print(f"\n{'='*60}")
        print(f"[{i}/{len(targets)}] {video['channel_name']} — {video['title'][:60]}")
        print(f"{'='*60}")
        process_video(video, client, force=args.force)

    print(f"\n[COMPLETE] {len(targets)} videos processed.")
    print(f"Knowledge base: {KB_DIR}")


if __name__ == '__main__':
    main()
