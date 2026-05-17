"""
Pinterest Image Metadata Generator
===================================
Scans a folder of images, uses Claude API (vision) to analyze each one,
generates Pinterest-optimized metadata, groups similar images, and
organizes everything into subfolders with text files.

Usage:
    python pinterest_metadata.py

Requirements:
    pip install anthropic pillow

Setup:
    - Put your images in D:\Desktop\Pinterest_Raw (or change INPUT_DIR below)
    - API key at D:\Desktop\api_key.txt
    - Run the script
    - Check D:\Desktop\Pinterest_Ready for organized output
"""

import os
import sys
import json
import shutil
import base64
import time
from pathlib import Path
from collections import defaultdict

try:
    from anthropic import Anthropic
except ImportError:
    print("ERROR: pip install anthropic")
    sys.exit(1)

try:
    from PIL import Image
except ImportError:
    print("ERROR: pip install pillow")
    sys.exit(1)


# ─── SETTINGS ────────────────────────────────────────────────────────────────

API_KEY_PATH = r"D:\Desktop\api_key.txt"
INPUT_DIR = Path(r"D:\Desktop\Pinterest_Raw")
OUTPUT_DIR = Path(r"D:\Desktop\Pinterest_Ready")

SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# ─── PROMPTS ─────────────────────────────────────────────────────────────────

SINGLE_IMAGE_PROMPT = """You are a Pinterest SEO expert. Analyze this image and generate optimized metadata for posting on Pinterest.

Return ONLY valid JSON with this exact structure (no markdown, no code blocks, no extra text):

{
    "title": "Pinterest-optimized title (60-100 chars, include searchable keywords)",
    "description": "Detailed Pinterest description (150-300 chars, include relevant keywords people actually search for on Pinterest, be specific not generic)",
    "hashtags": ["tag1", "tag2", "tag3", "tag4", "tag5", "tag6", "tag7", "tag8", "tag9", "tag10"],
    "board_suggestion": "Best Pinterest board name for this image",
    "style_category": "One of: modern-minimalist, cozy-rustic, luxury-elegant, bohemian, scandinavian, industrial, mid-century, coastal, farmhouse, traditional, contemporary, eclectic, art-deco, japanese-zen, tropical, other",
    "room_type": "One of: living-room, bedroom, bathroom, kitchen, dining-room, office, outdoor, entryway, nursery, laundry, hallway, garden, balcony, general-decor, not-home-decor",
    "color_palette": ["primary color", "secondary color", "accent color"],
    "mood": "One word describing the mood (cozy, elegant, fresh, bold, serene, warm, playful, sophisticated, etc.)",
    "seasonal": "One of: spring, summer, fall, winter, all-seasons",
    "carousel_keywords": ["keyword1", "keyword2", "keyword3"],
    "alt_text": "Accessible image description (what someone who can't see the image needs to know)"
}

RULES:
- Titles MUST include specific searchable terms (not "Beautiful Room" but "Small Apartment Living Room Ideas Under $500")
- Descriptions should read naturally and include long-tail keywords people search for
- Hashtags should mix popular tags with niche ones
- Be specific about what's in the image — colors, materials, furniture types, styles
- If it's NOT home decor, still categorize it appropriately
- Think about what someone would TYPE into Pinterest search to find this image"""

GROUP_ANALYSIS_PROMPT = """You are a Pinterest content strategist. I have a collection of images that have been categorized. Based on the categories and metadata below, suggest:

1. Which images should be grouped into CAROUSELS (2-5 images that tell a story together)
2. Which images should be grouped into COLLECTIONS (similar style/theme)  
3. What ORDER to post them in for maximum engagement
4. Any SERIES ideas (e.g., "10 Cozy Bedroom Ideas" spread across multiple posts)

Return ONLY valid JSON:

{
    "carousels": [
        {
            "name": "Carousel title",
            "description": "Why these go together",
            "images": ["filename1.jpg", "filename2.jpg"]
        }
    ],
    "collections": [
        {
            "board_name": "Board name",
            "description": "Board description",
            "images": ["filename1.jpg", "filename2.jpg"]
        }
    ],
    "posting_order": ["filename1.jpg", "filename2.jpg"],
    "series_ideas": [
        {
            "series_name": "Series title",
            "description": "What this series covers",
            "images": ["filename1.jpg", "filename2.jpg"]
        }
    ]
}

Here is the metadata for all images:
"""


# ─── FUNCTIONS ───────────────────────────────────────────────────────────────

def load_api_key() -> str:
    key_path = Path(API_KEY_PATH)
    if not key_path.exists():
        print(f"ERROR: API key not found at {API_KEY_PATH}")
        sys.exit(1)
    return key_path.read_text().strip()


def get_image_files(directory: Path) -> list:
    """Get all supported image files from directory."""
    files = []
    for f in sorted(directory.iterdir()):
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS:
            files.append(f)
    return files


def encode_image(image_path: Path) -> tuple:
    """Encode image to base64 and return with media type."""
    suffix = image_path.suffix.lower()
    media_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }
    media_type = media_types.get(suffix, "image/jpeg")

    # Resize if too large (Claude has limits)
    img = Image.open(image_path)
    max_size = 1568
    if img.width > max_size or img.height > max_size:
        img.thumbnail((max_size, max_size), Image.LANCZOS)
        # Save to bytes
        import io
        buffer = io.BytesIO()
        fmt = "PNG" if suffix == ".png" else "JPEG"
        img.save(buffer, format=fmt)
        image_bytes = buffer.getvalue()
    else:
        image_bytes = image_path.read_bytes()

    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    return b64, media_type


def analyze_image(client: Anthropic, image_path: Path) -> dict:
    """Send image to Claude for Pinterest metadata analysis."""
    b64, media_type = encode_image(image_path)

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": b64,
                    },
                },
                {
                    "type": "text",
                    "text": SINGLE_IMAGE_PROMPT,
                },
            ],
        }],
    )

    text = response.content[0].text.strip()

    # Clean up response — remove markdown code blocks if present
    if text.startswith("```"):
        text = text.split("\n", 1)[1]  # Remove first line
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"  WARNING: Failed to parse JSON for {image_path.name}")
        print(f"  Raw response: {text[:200]}...")
        return {
            "title": image_path.stem,
            "description": "Metadata generation failed — manual entry needed",
            "hashtags": [],
            "board_suggestion": "Uncategorized",
            "style_category": "other",
            "room_type": "general-decor",
            "color_palette": [],
            "mood": "unknown",
            "seasonal": "all-seasons",
            "carousel_keywords": [],
            "alt_text": image_path.stem,
        }


def analyze_groups(client: Anthropic, all_metadata: dict) -> dict:
    """Analyze all images together for grouping suggestions."""
    # Build summary for Claude
    summary = []
    for filename, meta in all_metadata.items():
        summary.append({
            "filename": filename,
            "title": meta.get("title", ""),
            "style_category": meta.get("style_category", ""),
            "room_type": meta.get("room_type", ""),
            "mood": meta.get("mood", ""),
            "color_palette": meta.get("color_palette", []),
            "hashtags": meta.get("hashtags", [])[:3],
        })

    prompt = GROUP_ANALYSIS_PROMPT + json.dumps(summary, indent=2)

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": prompt,
        }],
    )

    text = response.content[0].text.strip()

    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print("  WARNING: Failed to parse group analysis")
        return {"carousels": [], "collections": [], "posting_order": [], "series_ideas": []}


def save_image_metadata(output_dir: Path, filename: str, metadata: dict):
    """Save metadata as a text file next to the image."""
    txt_path = output_dir / f"{Path(filename).stem}_metadata.txt"

    lines = [
        f"PINTEREST METADATA — {filename}",
        f"{'=' * 60}",
        f"",
        f"TITLE: {metadata.get('title', '')}",
        f"",
        f"DESCRIPTION:",
        f"{metadata.get('description', '')}",
        f"",
        f"HASHTAGS: {' '.join('#' + h for h in metadata.get('hashtags', []))}",
        f"",
        f"BOARD: {metadata.get('board_suggestion', '')}",
        f"STYLE: {metadata.get('style_category', '')}",
        f"ROOM: {metadata.get('room_type', '')}",
        f"MOOD: {metadata.get('mood', '')}",
        f"SEASON: {metadata.get('seasonal', '')}",
        f"COLORS: {', '.join(metadata.get('color_palette', []))}",
        f"",
        f"ALT TEXT: {metadata.get('alt_text', '')}",
        f"",
        f"CAROUSEL KEYWORDS: {', '.join(metadata.get('carousel_keywords', []))}",
    ]

    txt_path.write_text("\n".join(lines), encoding="utf-8")


def save_group_metadata(output_dir: Path, group_data: dict):
    """Save group/carousel/collection suggestions."""
    txt_path = output_dir / "_POSTING_GUIDE.txt"

    lines = [
        "PINTEREST POSTING GUIDE",
        "=" * 60,
        "",
    ]

    # Posting order
    if group_data.get("posting_order"):
        lines.append("RECOMMENDED POSTING ORDER:")
        lines.append("-" * 40)
        for i, fname in enumerate(group_data["posting_order"], 1):
            lines.append(f"  {i}. {fname}")
        lines.append("")

    # Carousels
    if group_data.get("carousels"):
        lines.append("CAROUSEL IDEAS:")
        lines.append("-" * 40)
        for c in group_data["carousels"]:
            lines.append(f"  [{c.get('name', 'Untitled')}]")
            lines.append(f"  {c.get('description', '')}")
            lines.append(f"  Images: {', '.join(c.get('images', []))}")
            lines.append("")

    # Collections
    if group_data.get("collections"):
        lines.append("COLLECTIONS / BOARDS:")
        lines.append("-" * 40)
        for c in group_data["collections"]:
            lines.append(f"  Board: {c.get('board_name', 'Untitled')}")
            lines.append(f"  {c.get('description', '')}")
            lines.append(f"  Images: {', '.join(c.get('images', []))}")
            lines.append("")

    # Series
    if group_data.get("series_ideas"):
        lines.append("SERIES IDEAS:")
        lines.append("-" * 40)
        for s in group_data["series_ideas"]:
            lines.append(f"  [{s.get('series_name', 'Untitled')}]")
            lines.append(f"  {s.get('description', '')}")
            lines.append(f"  Images: {', '.join(s.get('images', []))}")
            lines.append("")

    txt_path.write_text("\n".join(lines), encoding="utf-8")


def organize_into_folders(output_dir: Path, input_dir: Path, all_metadata: dict):
    """Copy images into organized subfolders by style category."""
    for filename, metadata in all_metadata.items():
        style = metadata.get("style_category", "other")
        room = metadata.get("room_type", "general-decor")

        # Create subfolder: style/room
        subfolder = output_dir / style / room
        subfolder.mkdir(parents=True, exist_ok=True)

        # Copy image
        src = input_dir / filename
        if src.exists():
            shutil.copy2(src, subfolder / filename)

        # Save metadata in subfolder too
        save_image_metadata(subfolder, filename, metadata)


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    print("\n  Pinterest Image Metadata Generator")
    print(f"  {'=' * 50}")

    # Setup
    api_key = load_api_key()
    client = Anthropic(api_key=api_key)

    # Check input directory
    if not INPUT_DIR.exists():
        INPUT_DIR.mkdir(parents=True, exist_ok=True)
        print(f"\n  Created input folder: {INPUT_DIR}")
        print(f"  Put your images in there and run again.")
        return

    images = get_image_files(INPUT_DIR)
    if not images:
        print(f"\n  No images found in {INPUT_DIR}")
        print(f"  Supported formats: {', '.join(SUPPORTED_EXTENSIONS)}")
        return

    print(f"\n  Found {len(images)} images in {INPUT_DIR}")
    print(f"  Output: {OUTPUT_DIR}")

    # Create output directory
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Process each image
    all_metadata = {}
    for i, image_path in enumerate(images, 1):
        print(f"\n  [{i}/{len(images)}] Analyzing: {image_path.name}")

        try:
            metadata = analyze_image(client, image_path)
            all_metadata[image_path.name] = metadata

            # Save individual metadata to output root
            save_image_metadata(OUTPUT_DIR, image_path.name, metadata)

            # Copy image to output root
            shutil.copy2(image_path, OUTPUT_DIR / image_path.name)

            print(f"    Title: {metadata.get('title', 'N/A')}")
            print(f"    Board: {metadata.get('board_suggestion', 'N/A')}")
            print(f"    Style: {metadata.get('style_category', 'N/A')}")

            # Rate limit — don't hammer the API
            if i < len(images):
                time.sleep(1)

        except Exception as e:
            print(f"    ERROR: {e}")
            continue

    if not all_metadata:
        print("\n  No images were successfully analyzed.")
        return

    # Save all metadata as JSON for future use
    json_path = OUTPUT_DIR / "_all_metadata.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_metadata, f, indent=2, ensure_ascii=False)
    print(f"\n  Saved master metadata: {json_path}")

    # Group analysis
    if len(all_metadata) >= 2:
        print(f"\n  Analyzing groups and carousels...")
        group_data = analyze_groups(client, all_metadata)
        save_group_metadata(OUTPUT_DIR, group_data)
        print(f"  Saved posting guide: {OUTPUT_DIR / '_POSTING_GUIDE.txt'}")

    # Organize into subfolders
    print(f"\n  Organizing into style/room subfolders...")
    organize_into_folders(OUTPUT_DIR, INPUT_DIR, all_metadata)

    # Summary
    print(f"\n  {'=' * 50}")
    print(f"  DONE!")
    print(f"  {'=' * 50}")
    print(f"  Images processed: {len(all_metadata)}")
    print(f"  Output folder: {OUTPUT_DIR}")
    print(f"")
    print(f"  What's inside:")
    print(f"    - Each image + its metadata .txt file")
    print(f"    - _all_metadata.json (master file)")
    print(f"    - _POSTING_GUIDE.txt (carousels, collections, posting order)")
    print(f"    - Subfolders organized by style/room type")
    print(f"")

    # Quick stats
    styles = defaultdict(int)
    rooms = defaultdict(int)
    for meta in all_metadata.values():
        styles[meta.get("style_category", "other")] += 1
        rooms[meta.get("room_type", "general-decor")] += 1

    print(f"  Style breakdown:")
    for style, count in sorted(styles.items(), key=lambda x: -x[1]):
        print(f"    {style}: {count}")

    print(f"\n  Room breakdown:")
    for room, count in sorted(rooms.items(), key=lambda x: -x[1]):
        print(f"    {room}: {count}")


if __name__ == "__main__":
    main()
