#!/usr/bin/env python3
"""
Twitch Clipper v2
=================
Automated clip extraction from Twitch VODs with chat-enhanced scoring.

Usage:
    python twitch_clipper.py <twitch_username>
    python twitch_clipper.py <twitch_username> --vod-id 2012345678
    python twitch_clipper.py <twitch_username> --clips-per-hour 5
    python twitch_clipper.py <twitch_username> --min-duration 20 --max-duration 120

Requirements:
    pip install openai-whisper librosa numpy requests --break-system-packages

FFmpeg must be installed.

Output:
    clips/<streamer>/<date>_stream/
        post_queue/     - Score 80+ clips (best, post daily)
        backlog/        - Score 60-79 clips (filler)
        discarded/      - Score <60 (auto-cleaned)
        report.txt      - Full breakdown of all clips
        queue.db        - JSON database for queue management
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

# ============================================================
# Lazy imports (fail gracefully with install instructions)
# ============================================================
def check_dependencies():
    missing = []
    try:
        import whisper
    except ImportError:
        missing.append("openai-whisper")
    try:
        import librosa
    except ImportError:
        missing.append("librosa")
    try:
        import requests
    except ImportError:
        missing.append("requests")
    
    if missing:
        print(f"ERROR: Missing dependencies: {', '.join(missing)}")
        print(f"Run: pip install {' '.join(missing)} --break-system-packages")
        sys.exit(1)


# ============================================================
# CONFIG / DEFAULTS
# ============================================================
BASE_DIR = Path(__file__).parent
CLIPS_DIR = BASE_DIR / "clips"

# Twitch API — user must set these or use env vars
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "ubbd896zqv58y6fg498bw1qzzs0zx7")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "mhhuxwu45ubuj8a5gtk8hnkxvtbyje")

# Clipping defaults
DEFAULT_CLIPS_PER_HOUR = 3
DEFAULT_MIN_DURATION = 30      # seconds
DEFAULT_MAX_DURATION = 90      # seconds
DEFAULT_MIN_SPACING = 120      # seconds between clips
DEFAULT_WHISPER_MODEL = "medium"

# Scoring thresholds
SCORE_POST_QUEUE = 80
SCORE_BACKLOG = 60
BACKLOG_MAX = 100              # cull lowest when exceeded

# Scoring weights (sum to 1.0)
WEIGHT_AUDIO_ENERGY = 0.20
WEIGHT_SPEECH_RATE = 0.10
WEIGHT_VOLUME_SPIKE = 0.15
WEIGHT_KEYWORDS = 0.10
WEIGHT_CHAT_VELOCITY = 0.20
WEIGHT_EMOTE_SPIKE = 0.10
WEIGHT_VIEWER_CLIPS = 0.10
WEIGHT_TOPIC_COHERENCE = 0.05

# When no chat data available, redistribute chat weights
WEIGHT_AUDIO_ENERGY_NO_CHAT = 0.30
WEIGHT_SPEECH_RATE_NO_CHAT = 0.15
WEIGHT_VOLUME_SPIKE_NO_CHAT = 0.25
WEIGHT_KEYWORDS_NO_CHAT = 0.20
WEIGHT_TOPIC_COHERENCE_NO_CHAT = 0.10

# Excitement keywords
EXCITEMENT_KEYWORDS = {
    # Reactions
    "wow", "whoa", "oh my god", "omg", "no way", "what the", "holy",
    "insane", "crazy", "unbelievable", "incredible", "amazing",
    "dude", "bro", "bruh", "yooo", "sheesh",
    # Gaming
    "clutch", "lets go", "let's go", "gg", "poggers", "pog",
    "destroyed", "demolished", "wrecked", "owned", "rekt",
    "ace", "headshot", "triple", "quadra", "penta",
    "win", "victory", "champion", "first place",
    # Drama / storytelling
    "basically", "listen", "so what happened", "the thing is",
    "you wont believe", "i swear", "literally", "actually",
    "plot twist", "turns out", "long story short",
    # Emotional peaks
    "i love", "i hate", "crying", "dead", "im done", "im dead",
    "screaming", "laughing", "hilarious", "funniest",
    # Value / advice
    "the trick is", "pro tip", "heres the thing", "the secret",
    "most people dont know", "the reason", "this is why",
    # Questions (engagement)
    "what do you think", "chat", "you guys", "should i",
}

# Twitch emotes that signal highlights
HIGHLIGHT_EMOTES = {
    "PogChamp", "Pog", "POGGERS", "PogU", "POGGIES",
    "LUL", "LULW", "OMEGALUL", "KEKW", "ICANT",
    "Kreygasm", "PepeHands", "Sadge", "widepeepoSad",
    "HypeEmote", "catJAM", "EZ", "Clap",
    "monkaS", "monkaW", "MONKA", "pepeMeltdown",
    "KEKW", "LETSGO", "FeelsStrongMan",
    "D:", "gasp", "Shocked",
}


# ============================================================
# TWITCH API
# ============================================================
def get_twitch_token():
    """Get OAuth token using client credentials"""
    import requests
    
    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        print("\n  ERROR: Twitch API credentials not set.")
        print("  Set environment variables:")
        print("    export TWITCH_CLIENT_ID=your_client_id")
        print("    export TWITCH_CLIENT_SECRET=your_client_secret")
        print("\n  Get these from https://dev.twitch.tv/console/apps")
        sys.exit(1)
    
    r = requests.post("https://id.twitch.tv/oauth2/token", data={
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
        "grant_type": "client_credentials",
    })
    r.raise_for_status()
    return r.json()["access_token"]


def twitch_api(endpoint, token, params=None):
    """Make authenticated Twitch API request"""
    import requests
    
    headers = {
        "Client-ID": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {token}",
    }
    r = requests.get(f"https://api.twitch.tv/helix/{endpoint}",
                     headers=headers, params=params or {})
    r.raise_for_status()
    return r.json()


def get_user_id(username, token):
    """Get Twitch user ID from username"""
    data = twitch_api("users", token, {"login": username})
    if not data["data"]:
        print(f"  ERROR: User '{username}' not found on Twitch")
        sys.exit(1)
    user = data["data"][0]
    print(f"  Found: {user['display_name']} (ID: {user['id']})")
    return user["id"], user["display_name"]


def get_latest_vod(user_id, token, vod_id=None):
    """Get latest VOD or specific VOD by ID"""
    if vod_id:
        data = twitch_api("videos", token, {"id": vod_id})
    else:
        data = twitch_api("videos", token, {
            "user_id": user_id,
            "type": "archive",
            "first": 1,
        })
    
    if not data["data"]:
        print("  ERROR: No VODs found")
        sys.exit(1)
    
    vod = data["data"][0]
    print(f"  VOD: {vod['title']}")
    print(f"  Duration: {vod['duration']}")
    print(f"  Created: {vod['created_at']}")
    return vod


def get_vod_clips(broadcaster_id, token, started_at, ended_at):
    """Get viewer-created clips from the stream period"""
    clips = []
    cursor = None
    
    for _ in range(5):  # Max 5 pages
        params = {
            "broadcaster_id": broadcaster_id,
            "started_at": started_at,
            "ended_at": ended_at,
            "first": 100,
        }
        if cursor:
            params["after"] = cursor
        
        data = twitch_api("clips", token, params)
        clips.extend(data["data"])
        
        cursor = data.get("pagination", {}).get("cursor")
        if not cursor:
            break
    
    print(f"  Found {len(clips)} viewer-created clips")
    return clips


def parse_twitch_duration(duration_str):
    """Parse Twitch duration format like '3h22m10s' to seconds"""
    hours = 0
    minutes = 0
    seconds = 0
    
    h_match = re.search(r'(\d+)h', duration_str)
    m_match = re.search(r'(\d+)m', duration_str)
    s_match = re.search(r'(\d+)s', duration_str)
    
    if h_match:
        hours = int(h_match.group(1))
    if m_match:
        minutes = int(m_match.group(1))
    if s_match:
        seconds = int(s_match.group(1))
    
    return hours * 3600 + minutes * 60 + seconds


def get_chat_log(vod_id):
    """
    Get chat log for a VOD.
    
    Twitch doesn't have an official chat replay API.
    We use the unofficial GQL endpoint that powers the chat replay
    on the Twitch website. Falls back gracefully if unavailable.
    """
    import requests
    
    print("  Fetching chat log...")
    
    chat_messages = []
    cursor = None
    
    try:
        for page in range(200):  # Max pages to prevent infinite loop
            body = [{
                "operationName": "VideoCommentsByOffsetOrCursor",
                "variables": {
                    "videoID": str(vod_id),
                },
                "extensions": {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c81f04a6571ea395"
                    }
                }
            }]
            
            if cursor:
                body[0]["variables"]["cursor"] = cursor
            else:
                body[0]["variables"]["contentOffsetSeconds"] = 0
            
            r = requests.post(
                "https://gql.twitch.tv/gql",
                json=body,
                headers={"Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko"},
                timeout=10
            )
            
            if r.status_code != 200:
                print(f"  Chat API returned {r.status_code}, skipping chat data")
                return []
            
            data = r.json()
            comments = data[0].get("data", {}).get("video", {}).get("comments", {})
            edges = comments.get("edges", [])
            
            if not edges:
                break
            
            for edge in edges:
                node = edge.get("node", {})
                offset = node.get("contentOffsetSeconds", 0)
                
                # Extract message text
                fragments = node.get("message", {}).get("fragments", [])
                text = ""
                emotes = []
                for frag in fragments:
                    if frag.get("emote"):
                        emotes.append(frag.get("text", ""))
                    text += frag.get("text", "")
                
                chat_messages.append({
                    "offset": offset,
                    "text": text.strip(),
                    "emotes": emotes,
                    "commenter": node.get("commenter", {}).get("displayName", ""),
                })
            
            cursor = edges[-1].get("cursor")
            if not comments.get("pageInfo", {}).get("hasNextPage", False):
                break
            
            if page % 20 == 0 and page > 0:
                print(f"    ...{len(chat_messages)} messages so far")
        
        print(f"  Chat log: {len(chat_messages)} messages")
        return chat_messages
    
    except Exception as e:
        print(f"  Chat fetch failed: {e}")
        print("  Continuing without chat data")
        return []


# ============================================================
# VOD DOWNLOAD
# ============================================================
def download_vod(vod, output_dir):
    """Download VOD using streamlink or yt-dlp"""
    vod_url = vod["url"]
    output_path = output_dir / "vod.mp4"
    
    if output_path.exists():
        print(f"  VOD already downloaded: {output_path}")
        return output_path
    
    print(f"  Downloading VOD...")
    
    # Try streamlink first
    try:
        result = subprocess.run(
            ["streamlink", "--output", str(output_path), vod_url, "best"],
            capture_output=True, text=True, timeout=7200
        )
        if result.returncode == 0 and output_path.exists():
            print(f"  Downloaded via streamlink: {output_path}")
            return output_path
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    
    # Try yt-dlp
    try:
        result = subprocess.run(
            ["yt-dlp", "-o", str(output_path), vod_url],
            capture_output=True, text=True, timeout=7200
        )
        if result.returncode == 0:
            # yt-dlp might add extension
            for f in output_dir.glob("vod.*"):
                if f.suffix in [".mp4", ".mkv", ".ts"]:
                    print(f"  Downloaded via yt-dlp: {f}")
                    return f
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    
    print("  ERROR: Could not download VOD.")
    print("  Install streamlink: pip install streamlink --break-system-packages")
    print("  Or install yt-dlp: pip install yt-dlp --break-system-packages")
    sys.exit(1)


# ============================================================
# AUDIO EXTRACTION & ANALYSIS
# ============================================================
def extract_audio(video_path, output_dir):
    """Extract audio as WAV for analysis"""
    audio_path = output_dir / "audio.wav"
    
    if audio_path.exists():
        print(f"  Audio already extracted")
        return audio_path
    
    print("  Extracting audio...")
    subprocess.run([
        "ffmpeg", "-i", str(video_path),
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        str(audio_path), "-y", "-loglevel", "warning"
    ], check=True)
    
    return audio_path


def analyse_audio(audio_path):
    """Analyse audio for energy, spectral features, volume spikes"""
    import librosa
    
    print("  Analysing audio...")
    y, sr = librosa.load(str(audio_path), sr=16000)
    duration = len(y) / sr
    
    # Calculate features in 1-second windows
    hop_length = sr  # 1 second
    n_windows = int(duration)
    
    energy = np.zeros(n_windows)
    spectral = np.zeros(n_windows)
    
    for i in range(n_windows):
        start = i * sr
        end = min((i + 1) * sr, len(y))
        window = y[start:end]
        
        if len(window) == 0:
            continue
        
        # RMS energy
        energy[i] = np.sqrt(np.mean(window ** 2))
        
        # Spectral centroid (brightness)
        sc = librosa.feature.spectral_centroid(y=window, sr=sr)
        spectral[i] = np.mean(sc) if sc.size > 0 else 0
    
    # Normalise to 0-1
    if energy.max() > 0:
        energy = energy / energy.max()
    if spectral.max() > 0:
        spectral = spectral / spectral.max()
    
    # Volume spikes — detect sudden jumps
    volume_spikes = np.zeros(n_windows)
    for i in range(1, n_windows):
        diff = energy[i] - energy[i - 1]
        volume_spikes[i] = max(0, diff)
    if volume_spikes.max() > 0:
        volume_spikes = volume_spikes / volume_spikes.max()
    
    # Combined audio signal
    audio_signal = (energy + spectral) / 2
    
    print(f"  Audio: {n_windows} seconds analysed")
    return {
        "energy": energy,
        "spectral": spectral,
        "volume_spikes": volume_spikes,
        "combined": audio_signal,
        "duration": duration,
    }


# ============================================================
# TRANSCRIPTION
# ============================================================
def transcribe(audio_path, model_name="medium"):
    """Transcribe with Whisper, return word-level timestamps"""
    import whisper
    
    print(f"  Transcribing with Whisper ({model_name})...")
    model = whisper.load_model(model_name)
    result = model.transcribe(
        str(audio_path),
        word_timestamps=True,
        language="en",
        verbose=False,
    )
    
    # Extract word-level timestamps
    words = []
    for segment in result["segments"]:
        for word_info in segment.get("words", []):
            words.append({
                "word": word_info["word"].strip(),
                "start": word_info["start"],
                "end": word_info["end"],
            })
    
    # Build sentences from segments
    sentences = []
    for segment in result["segments"]:
        sentences.append({
            "text": segment["text"].strip(),
            "start": segment["start"],
            "end": segment["end"],
        })
    
    total_words = len(words)
    total_sentences = len(sentences)
    print(f"  Transcription: {total_words} words, {total_sentences} sentences")
    
    return {
        "words": words,
        "sentences": sentences,
        "full_text": result["text"],
        "segments": result["segments"],
    }


# ============================================================
# TRANSCRIPT ANALYSIS
# ============================================================
def analyse_transcript(transcript, duration_seconds):
    """Analyse transcript for keywords, speech rate, topic boundaries"""
    
    n_windows = int(duration_seconds)
    keyword_signal = np.zeros(n_windows)
    speech_rate = np.zeros(n_windows)
    
    # Keyword scoring per second
    for word_info in transcript["words"]:
        t = int(word_info["start"])
        if 0 <= t < n_windows:
            word_lower = word_info["word"].lower().strip(".,!?")
            if word_lower in EXCITEMENT_KEYWORDS:
                keyword_signal[t] += 1.0
            
            # Also check two-word phrases
            # (handled by checking sentence-level below)
    
    # Check sentence-level for multi-word keywords
    for sent in transcript["sentences"]:
        t = int(sent["start"])
        text_lower = sent["text"].lower()
        for kw in EXCITEMENT_KEYWORDS:
            if " " in kw and kw in text_lower:
                if 0 <= t < n_windows:
                    keyword_signal[t] += 1.5
    
    # Normalise keywords
    if keyword_signal.max() > 0:
        keyword_signal = keyword_signal / keyword_signal.max()
    
    # Speech rate — words per second in 5-second windows
    word_counts = np.zeros(n_windows)
    for word_info in transcript["words"]:
        t = int(word_info["start"])
        if 0 <= t < n_windows:
            word_counts[t] += 1
    
    # Smooth over 5-second windows
    window_size = 5
    for i in range(n_windows):
        start = max(0, i - window_size // 2)
        end = min(n_windows, i + window_size // 2 + 1)
        speech_rate[i] = np.mean(word_counts[start:end])
    
    if speech_rate.max() > 0:
        speech_rate = speech_rate / speech_rate.max()
    
    # Topic boundary detection — find where topics shift
    # Uses sentence embedding similarity (simple: word overlap between windows)
    topic_boundaries = []
    window_texts = []
    topic_window = 30  # 30-second text windows
    
    for i in range(0, n_windows, topic_window):
        text = ""
        for sent in transcript["sentences"]:
            if i <= sent["start"] < i + topic_window:
                text += " " + sent["text"]
        window_texts.append(text.strip().lower())
    
    for i in range(1, len(window_texts)):
        if not window_texts[i] or not window_texts[i - 1]:
            continue
        
        words_prev = set(window_texts[i - 1].split())
        words_curr = set(window_texts[i].split())
        
        if not words_prev or not words_curr:
            continue
        
        # Jaccard similarity
        overlap = len(words_prev & words_curr)
        total = len(words_prev | words_curr)
        similarity = overlap / total if total > 0 else 0
        
        # Low similarity = topic change
        if similarity < 0.15:
            boundary_time = i * topic_window
            topic_boundaries.append(boundary_time)
    
    # Topic coherence signal — higher score when NOT near a boundary
    topic_coherence = np.ones(n_windows)
    for boundary in topic_boundaries:
        for offset in range(-10, 10):
            t = boundary + offset
            if 0 <= t < n_windows:
                # Lower coherence near boundaries (bad place to clip)
                topic_coherence[t] *= 0.3
    
    print(f"  Transcript: {len(topic_boundaries)} topic boundaries detected")
    
    return {
        "keywords": keyword_signal,
        "speech_rate": speech_rate,
        "topic_boundaries": topic_boundaries,
        "topic_coherence": topic_coherence,
    }


# ============================================================
# CHAT ANALYSIS
# ============================================================
def analyse_chat(chat_messages, duration_seconds):
    """Analyse Twitch chat for velocity spikes and emote usage"""
    
    n_windows = int(duration_seconds)
    chat_velocity = np.zeros(n_windows)
    emote_signal = np.zeros(n_windows)
    
    if not chat_messages:
        return {
            "velocity": chat_velocity,
            "emotes": emote_signal,
            "has_data": False,
        }
    
    # Chat messages per second
    for msg in chat_messages:
        t = int(msg["offset"])
        if 0 <= t < n_windows:
            chat_velocity[t] += 1
            
            # Check for highlight emotes
            for emote in msg.get("emotes", []):
                if emote in HIGHLIGHT_EMOTES:
                    emote_signal[t] += 1
            
            # Also check message text for emote names
            for emote_name in HIGHLIGHT_EMOTES:
                if emote_name.lower() in msg["text"].lower():
                    emote_signal[t] += 0.5
    
    # Smooth over 5-second windows
    window_size = 5
    smoothed_velocity = np.zeros(n_windows)
    smoothed_emotes = np.zeros(n_windows)
    
    for i in range(n_windows):
        start = max(0, i - window_size // 2)
        end = min(n_windows, i + window_size // 2 + 1)
        smoothed_velocity[i] = np.mean(chat_velocity[start:end])
        smoothed_emotes[i] = np.mean(emote_signal[start:end])
    
    # Normalise
    if smoothed_velocity.max() > 0:
        smoothed_velocity = smoothed_velocity / smoothed_velocity.max()
    if smoothed_emotes.max() > 0:
        smoothed_emotes = smoothed_emotes / smoothed_emotes.max()
    
    total_msgs = len(chat_messages)
    peak_velocity = chat_velocity.max()
    print(f"  Chat: {total_msgs} messages, peak {int(peak_velocity)} msgs/sec")
    
    return {
        "velocity": smoothed_velocity,
        "emotes": smoothed_emotes,
        "has_data": True,
    }


def process_viewer_clips(viewer_clips, duration_seconds):
    """Convert viewer-created clips into a signal overlay"""
    
    n_windows = int(duration_seconds)
    clip_signal = np.zeros(n_windows)
    
    if not viewer_clips:
        return clip_signal
    
    for clip in viewer_clips:
        # Viewer clips have a vod_offset field (seconds into VOD)
        offset = clip.get("vod_offset")
        if offset is None:
            # Try to parse from URL or other fields
            continue
        
        duration = clip.get("duration", 30)
        view_count = clip.get("view_count", 1)
        
        # Weight by view count (log scale)
        weight = min(1.0, math.log(max(1, view_count) + 1) / 10)
        
        for t in range(int(offset), min(int(offset + duration), n_windows)):
            clip_signal[t] = max(clip_signal[t], weight)
    
    if clip_signal.max() > 0:
        clip_signal = clip_signal / clip_signal.max()
    
    clips_with_offset = sum(1 for c in viewer_clips if c.get("vod_offset") is not None)
    print(f"  Viewer clips: {clips_with_offset} with timing data")
    
    return clip_signal


# ============================================================
# SCORING
# ============================================================
def compute_scores(audio_data, transcript_data, chat_data, viewer_clip_signal):
    """Combine all signals into per-second scores 0-100"""
    
    n_windows = len(audio_data["energy"])
    scores = np.zeros(n_windows)
    has_chat = chat_data["has_data"]
    
    for i in range(n_windows):
        if has_chat:
            score = (
                WEIGHT_AUDIO_ENERGY * audio_data["combined"][i] +
                WEIGHT_SPEECH_RATE * transcript_data["speech_rate"][i] +
                WEIGHT_VOLUME_SPIKE * audio_data["volume_spikes"][i] +
                WEIGHT_KEYWORDS * transcript_data["keywords"][i] +
                WEIGHT_CHAT_VELOCITY * chat_data["velocity"][i] +
                WEIGHT_EMOTE_SPIKE * chat_data["emotes"][i] +
                WEIGHT_VIEWER_CLIPS * viewer_clip_signal[i] +
                WEIGHT_TOPIC_COHERENCE * transcript_data["topic_coherence"][i]
            )
        else:
            score = (
                WEIGHT_AUDIO_ENERGY_NO_CHAT * audio_data["combined"][i] +
                WEIGHT_SPEECH_RATE_NO_CHAT * transcript_data["speech_rate"][i] +
                WEIGHT_VOLUME_SPIKE_NO_CHAT * audio_data["volume_spikes"][i] +
                WEIGHT_KEYWORDS_NO_CHAT * transcript_data["keywords"][i] +
                WEIGHT_TOPIC_COHERENCE_NO_CHAT * transcript_data["topic_coherence"][i]
            )
        
        scores[i] = score * 100
    
    return scores


# ============================================================
# CLIP EXTRACTION
# ============================================================
def find_clip_candidates(scores, transcript, duration,
                         clips_per_hour, min_duration, max_duration, min_spacing):
    """Find best clip candidates with sentence boundary snapping"""
    
    total_hours = duration / 3600
    target_clips = max(1, int(total_hours * clips_per_hour))
    
    print(f"  Finding {target_clips} clips ({clips_per_hour}/hour, {total_hours:.1f}h stream)...")
    
    sentences = transcript["sentences"]
    topic_boundaries = transcript.get("topic_boundaries", [])
    
    # Smooth scores over windows to find peaks
    window = 10  # 10-second smoothing
    smoothed = np.convolve(scores, np.ones(window) / window, mode='same')
    
    # Find peaks — local maxima in smoothed scores
    peaks = []
    for i in range(window, len(smoothed) - window):
        if smoothed[i] == max(smoothed[i - window:i + window + 1]):
            peaks.append((i, smoothed[i]))
    
    # Sort by score descending
    peaks.sort(key=lambda x: x[1], reverse=True)
    
    # Select non-overlapping clips
    candidates = []
    used_ranges = []
    
    for peak_time, peak_score in peaks:
        if len(candidates) >= target_clips * 3:  # Get 3x candidates for selection
            break
        
        # Check spacing
        too_close = False
        for used_start, used_end in used_ranges:
            if abs(peak_time - (used_start + used_end) / 2) < min_spacing:
                too_close = True
                break
        
        if too_close:
            continue
        
        # Determine raw clip boundaries around peak
        raw_start = max(0, peak_time - max_duration // 2)
        raw_end = min(int(duration), peak_time + max_duration // 2)
        
        # Snap to sentence boundaries
        clip_start = snap_to_sentence_start(raw_start, sentences)
        clip_end = snap_to_sentence_end(raw_end, sentences)
        
        # Check topic boundaries — trim if clip crosses one
        clip_start, clip_end = trim_to_topic(
            clip_start, clip_end, peak_time, topic_boundaries, sentences
        )
        
        # Enforce duration limits
        clip_duration = clip_end - clip_start
        if clip_duration < min_duration:
            # Try expanding to meet minimum
            clip_end = snap_to_sentence_end(clip_start + min_duration, sentences)
            clip_duration = clip_end - clip_start
        
        if clip_duration < min_duration or clip_duration > max_duration * 1.2:
            continue
        
        # Calculate clip score (average of scores in clip range)
        clip_scores = scores[int(clip_start):int(clip_end)]
        avg_score = np.mean(clip_scores) if len(clip_scores) > 0 else 0
        peak_in_clip = np.max(clip_scores) if len(clip_scores) > 0 else 0
        
        # Final score: 60% peak + 40% average
        final_score = int(0.6 * peak_in_clip + 0.4 * avg_score)
        final_score = min(100, max(0, final_score))
        
        candidates.append({
            "start": clip_start,
            "end": clip_end,
            "duration": clip_end - clip_start,
            "score": final_score,
            "peak_time": peak_time,
        })
        
        used_ranges.append((clip_start, clip_end))
    
    # Sort by score and take top N
    candidates.sort(key=lambda x: x["score"], reverse=True)
    selected = candidates[:target_clips]
    
    # Sort selected by time order
    selected.sort(key=lambda x: x["start"])
    
    print(f"  Selected {len(selected)} clips from {len(candidates)} candidates")
    return selected


def snap_to_sentence_start(raw_time, sentences):
    """Snap a time to the nearest sentence start BEFORE it"""
    best = raw_time
    for sent in sentences:
        if sent["start"] <= raw_time:
            best = sent["start"]
        else:
            break
    return best


def snap_to_sentence_end(raw_time, sentences):
    """Snap a time to the nearest sentence end AFTER it"""
    for sent in sentences:
        if sent["end"] >= raw_time:
            return sent["end"]
    return raw_time


def trim_to_topic(clip_start, clip_end, peak_time, topic_boundaries, sentences):
    """If clip crosses a topic boundary, trim to keep the peak's topic"""
    
    boundaries_in_clip = [b for b in topic_boundaries if clip_start < b < clip_end]
    
    if not boundaries_in_clip:
        return clip_start, clip_end
    
    # Find which side of the boundary the peak is on
    for boundary in boundaries_in_clip:
        if peak_time < boundary:
            # Peak is before boundary — trim end to boundary
            clip_end = snap_to_sentence_end(boundary - 5, sentences)
            if clip_end <= clip_start:
                clip_end = boundary
        else:
            # Peak is after boundary — trim start to boundary
            clip_start = snap_to_sentence_start(boundary + 5, sentences)
            if clip_start >= clip_end:
                clip_start = boundary
    
    return clip_start, clip_end


# ============================================================
# CLIP CUTTING & EXPORT
# ============================================================
def cut_clips(video_path, clips, output_dir, transcript):
    """Cut clips from video using FFmpeg"""
    
    print(f"\n  Cutting {len(clips)} clips...")
    
    for i, clip in enumerate(clips):
        clip_num = i + 1
        start_ts = format_timestamp(clip["start"])
        score = clip["score"]
        
        # Determine queue folder
        if score >= SCORE_POST_QUEUE:
            folder = output_dir / "post_queue"
        elif score >= SCORE_BACKLOG:
            folder = output_dir / "backlog"
        else:
            folder = output_dir / "discarded"
        
        folder.mkdir(parents=True, exist_ok=True)
        
        filename = f"clip_{clip_num:02d}_score{score}_{start_ts.replace(':', 'h', 1).replace(':', 'm', 1)}s"
        
        # Original aspect ratio clip
        original_path = folder / f"{filename}_original.mp4"
        
        subprocess.run([
            "ffmpeg",
            "-ss", str(clip["start"]),
            "-i", str(video_path),
            "-t", str(clip["duration"]),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            str(original_path),
            "-y", "-loglevel", "warning"
        ], check=True)
        
        clip["file_original"] = str(original_path)
        
        # Get transcript for this clip
        clip_text = get_clip_transcript(clip["start"], clip["end"], transcript)
        clip["transcript"] = clip_text
        
        # Generate title from transcript
        clip["title"] = generate_clip_title(clip_text)
        
        # Queue category
        if score >= SCORE_POST_QUEUE:
            clip["queue"] = "post"
        elif score >= SCORE_BACKLOG:
            clip["queue"] = "backlog"
        else:
            clip["queue"] = "discarded"
        
        print(f"    [{clip_num}/{len(clips)}] Score {score} | {clip['duration']:.0f}s | {clip['queue']} | {clip['title'][:50]}")
    
    return clips


def get_clip_transcript(start, end, transcript):
    """Get transcript text for a clip timerange"""
    texts = []
    for sent in transcript["sentences"]:
        if sent["start"] >= start and sent["end"] <= end:
            texts.append(sent["text"])
        elif sent["start"] < end and sent["end"] > start:
            texts.append(sent["text"])
    return " ".join(texts)


def generate_clip_title(text):
    """Generate a short title from clip transcript"""
    # Take first sentence or first 10 words
    text = text.strip()
    
    # First sentence
    for delim in [". ", "! ", "? "]:
        if delim in text:
            first = text[:text.index(delim) + 1]
            if 5 < len(first) < 100:
                return first.strip()
    
    # First N words
    words = text.split()[:10]
    title = " ".join(words)
    if len(title) > 80:
        title = title[:80] + "..."
    return title


def format_timestamp(seconds):
    """Format seconds as HH:MM:SS"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ============================================================
# QUEUE MANAGEMENT
# ============================================================
def load_queue(queue_path):
    """Load existing queue database"""
    if queue_path.exists():
        with open(queue_path) as f:
            return json.load(f)
    return {"post_queue": [], "backlog": [], "stats": {"total_processed": 0}}


def save_queue(queue, queue_path):
    """Save queue database"""
    with open(queue_path, "w") as f:
        json.dump(queue, f, indent=2)


def update_queue(queue, new_clips, streamer_name, stream_date):
    """Add new clips to queue and cull backlog if needed"""
    
    for clip in new_clips:
        entry = {
            "streamer": streamer_name,
            "stream_date": stream_date,
            "score": clip["score"],
            "title": clip.get("title", ""),
            "file": clip.get("file_original", ""),
            "duration": clip["duration"],
            "start": clip["start"],
            "added": datetime.now().isoformat(),
        }
        
        if clip["queue"] == "post":
            queue["post_queue"].append(entry)
        elif clip["queue"] == "backlog":
            queue["backlog"].append(entry)
    
    queue["stats"]["total_processed"] = queue["stats"].get("total_processed", 0) + len(new_clips)
    
    # Sort by score descending
    queue["post_queue"].sort(key=lambda x: x["score"], reverse=True)
    queue["backlog"].sort(key=lambda x: x["score"], reverse=True)
    
    # Cull backlog if over limit
    if len(queue["backlog"]) > BACKLOG_MAX:
        removed = len(queue["backlog"]) - BACKLOG_MAX
        culled = queue["backlog"][BACKLOG_MAX:]
        queue["backlog"] = queue["backlog"][:BACKLOG_MAX]
        
        # Delete culled files
        for entry in culled:
            try:
                Path(entry["file"]).unlink(missing_ok=True)
            except:
                pass
        
        print(f"  Backlog culled: removed {removed} lowest-scoring clips")
    
    return queue


# ============================================================
# REPORT
# ============================================================
def generate_report(clips, output_dir, streamer_name, vod_info, chat_count, audio_duration):
    """Generate human-readable report"""
    
    report_path = output_dir / "report.txt"
    
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("=" * 60 + "\n")
        f.write(f"  TWITCH CLIPPER REPORT\n")
        f.write(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
        f.write("=" * 60 + "\n\n")
        
        f.write(f"  Streamer: {streamer_name}\n")
        f.write(f"  VOD: {vod_info.get('title', 'N/A')}\n")
        f.write(f"  Duration: {format_timestamp(audio_duration)}\n")
        f.write(f"  Chat messages: {chat_count}\n")
        f.write(f"  Clips generated: {len(clips)}\n\n")
        
        post = [c for c in clips if c["queue"] == "post"]
        backlog = [c for c in clips if c["queue"] == "backlog"]
        discarded = [c for c in clips if c["queue"] == "discarded"]
        
        f.write(f"  Post queue (80+): {len(post)}\n")
        f.write(f"  Backlog (60-79): {len(backlog)}\n")
        f.write(f"  Discarded (<60): {len(discarded)}\n\n")
        
        f.write("-" * 60 + "\n")
        f.write("  CLIPS\n")
        f.write("-" * 60 + "\n\n")
        
        for i, clip in enumerate(clips):
            f.write(f"  Clip {i + 1}\n")
            f.write(f"  Score: {clip['score']}/100 [{clip['queue'].upper()}]\n")
            f.write(f"  Time: {format_timestamp(clip['start'])} — {format_timestamp(clip['end'])}\n")
            f.write(f"  Duration: {clip['duration']:.0f}s\n")
            f.write(f"  Title: {clip.get('title', 'N/A')}\n")
            f.write(f"  File: {clip.get('file_original', 'N/A')}\n")
            
            if clip.get("transcript"):
                preview = clip["transcript"][:200]
                f.write(f"  Transcript: {preview}...\n")
            
            f.write("\n")
    
    # Also save full transcript
    transcript_path = output_dir / "full_transcript.txt"
    # Will be written separately if needed
    
    print(f"\n  Report: {report_path}")
    return report_path


# ============================================================
# MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Twitch Clipper v2")
    parser.add_argument("username", help="Twitch username")
    parser.add_argument("--vod-id", help="Specific VOD ID (default: latest)")
    parser.add_argument("--clips-per-hour", type=int, default=DEFAULT_CLIPS_PER_HOUR)
    parser.add_argument("--min-duration", type=int, default=DEFAULT_MIN_DURATION)
    parser.add_argument("--max-duration", type=int, default=DEFAULT_MAX_DURATION)
    parser.add_argument("--min-spacing", type=int, default=DEFAULT_MIN_SPACING)
    parser.add_argument("--whisper-model", default=DEFAULT_WHISPER_MODEL,
                        choices=["tiny", "base", "small", "medium", "large"])
    parser.add_argument("--skip-chat", action="store_true", help="Skip chat log fetch")
    parser.add_argument("--local-video", help="Use local video file instead of downloading")
    
    args = parser.parse_args()
    
    check_dependencies()
    
    print("=" * 60)
    print("  TWITCH CLIPPER v2")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)
    
    # --- Twitch API ---
    print("\n  Connecting to Twitch...")
    token = get_twitch_token()
    user_id, display_name = get_user_id(args.username, token)
    
    # --- Get VOD ---
    print("\n  Getting VOD...")
    vod = get_latest_vod(user_id, token, args.vod_id)
    vod_id = vod["id"]
    vod_duration = parse_twitch_duration(vod["duration"])
    
    # --- Setup output directory ---
    stream_date = vod["created_at"][:10]
    output_dir = CLIPS_DIR / args.username / f"{stream_date}_stream"
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # --- Download VOD ---
    if args.local_video:
        video_path = Path(args.local_video)
        if not video_path.exists():
            print(f"  ERROR: File not found: {args.local_video}")
            sys.exit(1)
        print(f"  Using local video: {video_path}")
    else:
        print("\n  Downloading VOD...")
        video_path = download_vod(vod, output_dir)
    
    # --- Get chat log ---
    chat_messages = []
    if not args.skip_chat:
        print("\n  Getting chat data...")
        chat_messages = get_chat_log(vod_id)
    
    # --- Get viewer clips ---
    print("\n  Getting viewer clips...")
    vod_created = vod["created_at"]
    # Estimate end time from duration
    viewer_clips = get_vod_clips(user_id, token, vod_created, 
                                  datetime.now().isoformat() + "Z")
    
    # --- Extract audio ---
    print("\n  Processing audio...")
    audio_path = extract_audio(video_path, output_dir)
    
    # --- Analyse audio ---
    audio_data = analyse_audio(audio_path)
    duration = audio_data["duration"]
    
    # --- Transcribe ---
    print("\n  Transcribing...")
    transcript = transcribe(audio_path, args.whisper_model)
    
    # --- Analyse transcript ---
    print("\n  Analysing content...")
    transcript_data = analyse_transcript(transcript, duration)
    
    # --- Analyse chat ---
    chat_data = analyse_chat(chat_messages, duration)
    
    # --- Process viewer clips ---
    viewer_clip_signal = process_viewer_clips(viewer_clips, duration)
    
    # --- Score everything ---
    print("\n  Scoring moments...")
    scores = compute_scores(audio_data, transcript_data, chat_data, viewer_clip_signal)
    
    avg_score = np.mean(scores)
    max_score = np.max(scores)
    print(f"  Average score: {avg_score:.1f}")
    print(f"  Peak score: {max_score:.1f}")
    
    # --- Find clips ---
    clips = find_clip_candidates(
        scores, 
        {"sentences": transcript["sentences"], "topic_boundaries": transcript_data["topic_boundaries"]},
        duration,
        args.clips_per_hour,
        args.min_duration,
        args.max_duration,
        args.min_spacing,
    )
    
    if not clips:
        print("\n  No suitable clips found.")
        return
    
    # --- Cut clips ---
    clips = cut_clips(video_path, clips, output_dir, transcript)
    
    # --- Queue management ---
    queue_path = CLIPS_DIR / args.username / "queue.db"
    queue = load_queue(queue_path)
    queue = update_queue(queue, clips, display_name, stream_date)
    save_queue(queue, queue_path)
    
    # --- Report ---
    generate_report(clips, output_dir, display_name, vod, len(chat_messages), duration)
    
    # --- Save full transcript ---
    transcript_path = output_dir / "full_transcript.txt"
    with open(transcript_path, "w", encoding="utf-8") as f:
        for sent in transcript["sentences"]:
            f.write(f"[{format_timestamp(sent['start'])}] {sent['text']}\n")
    
    # --- Summary ---
    post_count = sum(1 for c in clips if c["queue"] == "post")
    backlog_count = sum(1 for c in clips if c["queue"] == "backlog")
    discard_count = sum(1 for c in clips if c["queue"] == "discarded")
    
    print(f"\n{'=' * 60}")
    print(f"  DONE")
    print(f"{'=' * 60}")
    print(f"\n  Streamer: {display_name}")
    print(f"  Stream: {vod['title']}")
    print(f"  Duration: {format_timestamp(duration)}")
    print(f"  Chat messages: {len(chat_messages)}")
    print(f"\n  Clips: {len(clips)} total")
    print(f"    Post queue (80+): {post_count}")
    print(f"    Backlog (60-79):  {backlog_count}")
    print(f"    Discarded (<60):  {discard_count}")
    print(f"\n  Queue totals:")
    print(f"    Post queue: {len(queue['post_queue'])} clips")
    print(f"    Backlog: {len(queue['backlog'])} clips")
    print(f"\n  Output: {output_dir}")
    print()


if __name__ == "__main__":
    main()
