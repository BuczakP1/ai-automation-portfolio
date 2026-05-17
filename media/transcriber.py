#!/usr/bin/env python3
"""
Podcast Transcriber v2
========================
Drop videos/audio into the input folder.
Extracts audio, transcribes with Whisper, saves transcripts, deletes originals.

Workflow:
    1. Place video/audio files in input folder (subfolders supported)
    2. Run this script
    3. Audio extracted, video deleted
    4. Whisper transcribes (no hallucinations, handles any length)
    5. Transcripts saved to output folder with matching structure
    6. Original files deleted after successful transcription

Setup:
    Create these folders (or the script will create them):
        D:/Desktop/Transcripts/input/    <- drop files here
        D:/Desktop/Transcripts/output/   <- transcripts appear here

Usage:
    py -3.12 transcriber.py
    py -3.12 transcriber.py --model medium
    py -3.12 transcriber.py --keep-audio
    py -3.12 transcriber.py --input "D:/path/to/input" --output "D:/path/to/output"

Requirements:
    pip install openai-whisper --break-system-packages
    ffmpeg must be installed and in PATH
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    import whisper
    HAS_WHISPER = True
except ImportError:
    HAS_WHISPER = False
    print("ERROR: whisper not installed")
    print("Run: py -3.12 -m pip install openai-whisper --break-system-packages")

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".webm", ".mov", ".flv", ".wmv"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aac", ".wma", ".opus"}
ALL_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS

DEFAULT_INPUT = Path("D:/Desktop/Transcripts/input")
DEFAULT_OUTPUT = Path("D:/Desktop/Transcripts/output")


def extract_audio(video_path):
    audio_path = video_path.with_suffix(".wav")
    print(f"    Extracting audio...", end=" ", flush=True)
    try:
        cmd = [
            "ffmpeg", "-i", str(video_path),
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", "-y",
            str(audio_path)
        ]
        subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if audio_path.exists() and audio_path.stat().st_size > 1000:
            size_mb = audio_path.stat().st_size / (1024 * 1024)
            print(f"done ({size_mb:.1f}MB)")
            return audio_path
        else:
            print(f"failed")
            return None
    except subprocess.TimeoutExpired:
        print(f"timeout")
        return None
    except FileNotFoundError:
        print(f"FFmpeg not found! Install: https://ffmpeg.org/download.html")
        return None
    except Exception as e:
        print(f"error: {e}")
        return None


def transcribe_audio(model, audio_path):
    print(f"    Transcribing...", end=" ", flush=True)
    start = time.time()
    try:
        result = model.transcribe(
            str(audio_path),
            verbose=False,
            language="en",
            task="transcribe",
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            compression_ratio_threshold=2.4,
            temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
            fp16=True,
        )
        elapsed = time.time() - start
        text = result.get("text", "").strip()
        if not text or len(text) < 20:
            print(f"no speech detected ({elapsed:.0f}s)")
            return None
        word_count = len(text.split())
        duration = result.get("duration", 0)
        print(f"done ({elapsed:.0f}s | {str(timedelta(seconds=int(duration)))} | {word_count:,} words)")
        return {
            "text": text,
            "segments": result.get("segments", []),
            "duration": duration,
            "language": result.get("language", "en"),
            "word_count": word_count,
        }
    except RuntimeError as e:
        if "out of memory" in str(e).lower():
            print(f"GPU OOM, retrying on CPU...")
            try:
                result = model.transcribe(
                    str(audio_path), verbose=False, language="en",
                    task="transcribe", condition_on_previous_text=False,
                    no_speech_threshold=0.6, fp16=False,
                )
                text = result.get("text", "").strip()
                if text:
                    elapsed = time.time() - start
                    print(f"done on CPU ({elapsed:.0f}s)")
                    return {
                        "text": text,
                        "segments": result.get("segments", []),
                        "duration": result.get("duration", 0),
                        "language": result.get("language", "en"),
                        "word_count": len(text.split()),
                    }
            except Exception as e2:
                print(f"CPU retry failed: {e2}")
        else:
            print(f"error: {e}")
        return None
    except Exception as e:
        print(f"error: {e}")
        return None


def save_transcript(result, original_path, input_dir, output_dir):
    try:
        rel_path = original_path.relative_to(input_dir)
    except ValueError:
        rel_path = Path(original_path.name)
    
    out_path = output_dir / rel_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    
    txt_path = out_path.with_suffix(".txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(result["text"])
    
    json_path = out_path.with_suffix(".transcript.json")
    transcript_data = {
        "source_file": original_path.name,
        "folder": original_path.parent.name,
        "duration_seconds": result.get("duration", 0),
        "duration_formatted": str(timedelta(seconds=int(result.get("duration", 0)))),
        "word_count": result.get("word_count", 0),
        "language": result.get("language", "en"),
        "transcribed_at": datetime.now().isoformat(),
        "text": result["text"],
        "segments": [
            {"start": round(s["start"], 2), "end": round(s["end"], 2), "text": s["text"].strip()}
            for s in result.get("segments", [])
        ],
    }
    
    info_path = original_path.with_suffix(".info.json")
    if info_path.exists():
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
            transcript_data["video_url"] = info.get("webpage_url", info.get("url", ""))
            transcript_data["video_title"] = info.get("title", "")
            transcript_data["channel"] = info.get("channel", info.get("uploader", ""))
            transcript_data["upload_date"] = info.get("upload_date", "")
            transcript_data["description"] = info.get("description", "")[:1000]
            transcript_data["tags"] = info.get("tags", [])
            transcript_data["playlist"] = info.get("playlist_title", "")
            transcript_data["thumbnail"] = info.get("thumbnail", "")
        except:
            pass
    
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(transcript_data, f, indent=2, ensure_ascii=False)
    
    print(f"    Saved: {txt_path.relative_to(output_dir)}")
    return txt_path, json_path


def process_file(model, file_path, input_dir, output_dir, keep_audio=False):
    is_video = file_path.suffix.lower() in VIDEO_EXTENSIONS
    audio_path = None
    
    try:
        if is_video:
            audio_path = extract_audio(file_path)
            if not audio_path:
                return False
        else:
            audio_path = file_path
        
        result = transcribe_audio(model, audio_path)
        if not result:
            if audio_path and audio_path != file_path and audio_path.exists():
                audio_path.unlink()
            return False
        
        save_transcript(result, file_path, input_dir, output_dir)
        
        # Move metadata files to output
        try:
            rel_path = file_path.relative_to(input_dir)
        except ValueError:
            rel_path = Path(file_path.name)
        out_base = output_dir / rel_path
        
        for ext in [".info.json", ".webp", ".jpg", ".png"]:
            src = file_path.with_suffix(ext)
            if src.exists():
                dst = out_base.with_suffix(ext)
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(src), str(dst))
                except:
                    pass
        
        # Cleanup
        if is_video:
            if audio_path and audio_path != file_path and audio_path.exists():
                audio_path.unlink()
            try:
                file_path.unlink()
                print(f"    Deleted video: {file_path.name}")
            except Exception as e:
                print(f"    Could not delete video: {e}")
        elif not keep_audio:
            try:
                file_path.unlink()
                print(f"    Deleted audio: {file_path.name}")
            except Exception as e:
                print(f"    Could not delete audio: {e}")
        
        return True
        
    except Exception as e:
        print(f"    ERROR: {e}")
        if audio_path and audio_path != file_path and audio_path.exists():
            try:
                audio_path.unlink()
            except:
                pass
        return False


def find_files(input_dir, output_dir):
    to_process = []
    already_done = 0
    
    for path in sorted(input_dir.rglob("*")):
        if path.suffix.lower() in ALL_EXTENSIONS:
            try:
                rel = path.relative_to(input_dir)
            except ValueError:
                rel = Path(path.name)
            out_txt = output_dir / rel.with_suffix(".txt")
            if out_txt.exists() and out_txt.stat().st_size > 100:
                already_done += 1
            else:
                to_process.append(path)
    
    return to_process, already_done


def main():
    parser = argparse.ArgumentParser(description="Podcast Transcriber v2")
    parser.add_argument("--input", default=str(DEFAULT_INPUT),
                        help=f"Input folder (default: {DEFAULT_INPUT})")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT),
                        help=f"Output folder (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--model", default="base",
                        choices=["tiny", "base", "small", "medium", "large"],
                        help="Whisper model (default: base, medium = better quality)")
    parser.add_argument("--keep-audio", action="store_true",
                        help="Don't delete audio files after transcription")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max files to process (0 = all)")
    
    args = parser.parse_args()
    input_dir = Path(args.input)
    output_dir = Path(args.output)
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print("=" * 60)
    print("  PODCAST TRANSCRIBER v2")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Model: {args.model}")
    print(f"  Input:  {input_dir}")
    print(f"  Output: {output_dir}")
    print("=" * 60)
    
    if not HAS_WHISPER:
        sys.exit(1)
    
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
    except FileNotFoundError:
        print("\n  ERROR: FFmpeg not found!")
        print("  Install: winget install FFmpeg")
        sys.exit(1)
    
    files, already_done = find_files(input_dir, output_dir)
    print(f"\n  Found {len(files)} files to process")
    print(f"  Already transcribed: {already_done}")
    
    if not files:
        print("\n  Nothing to process. Drop files in the input folder.")
        return
    
    if args.limit > 0:
        files = files[:args.limit]
    
    for f in files[:20]:
        size_mb = f.stat().st_size / (1024 * 1024)
        print(f"    {f.parent.name}/{f.name} ({size_mb:.0f}MB)")
    if len(files) > 20:
        print(f"    ... and {len(files) - 20} more")
    
    print(f"\n  Loading Whisper {args.model} model...")
    model = whisper.load_model(args.model)
    print(f"  Model loaded\n")
    
    success = 0
    failed = 0
    failed_files = []
    
    for i, file_path in enumerate(files, 1):
        print(f"\n  [{i}/{len(files)}] {file_path.parent.name}/{file_path.name}")
        if process_file(model, file_path, input_dir, output_dir, keep_audio=args.keep_audio):
            success += 1
        else:
            failed += 1
            failed_files.append(file_path)
    
    print(f"\n{'=' * 60}")
    print(f"  COMPLETE")
    print(f"{'=' * 60}")
    print(f"  Transcribed: {success}")
    print(f"  Failed: {failed}")
    print(f"  Output: {output_dir}")
    
    if failed_files:
        print(f"\n  Failed:")
        for f in failed_files:
            print(f"    - {f.name}")
    
    for dirpath in sorted(input_dir.rglob("*"), reverse=True):
        if dirpath.is_dir():
            try:
                dirpath.rmdir()
            except OSError:
                pass


if __name__ == "__main__":
    main()
