#!/usr/bin/env python3
"""
LinkedIn Content Generator v4
==============================
Reads your scraped data, compares with last month, researches current news,
sends everything to GPT-4o-mini to generate 30 unique LinkedIn posts.

Run once a month after your scrapers.

Output:
    linkedin_content/
        posts.txt              - 30 posts, dated, ready to copy/paste
        samples/               - Redacted sample CSVs
        history/               - Monthly snapshots for trend tracking
        YYYY-MM_posts.txt      - Archive of each month's posts

REQUIREMENTS:
    pip install pandas openai requests
"""

import pandas as pd
import csv
import json
import random
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    import anthropic
except ImportError:
    print("ERROR: pip install anthropic --break-system-packages")
    exit(1)

try:
    import requests
except ImportError:
    print("ERROR: pip install requests --break-system-packages")
    exit(1)


# ============================================================
# CONFIG
# ============================================================
BASE_DIR = Path(__file__).parent

CARE_OUTPUT = BASE_DIR / "Care Providers" / "output"
BUSREG_DIR = BASE_DIR / "Business Registrations"
BUSREG_FOLDERS = {
    "Ireland": BUSREG_DIR / "1 - Ireland Data",
    "UK": BUSREG_DIR / "2 - UK Data",
    "New Zealand": BUSREG_DIR / "3 - NZ Data",
    "Australia": BUSREG_DIR / "4 - Australia Data",
}

OUTPUT_DIR = BASE_DIR / "linkedin_content"
SAMPLES_DIR = OUTPUT_DIR / "samples"
HISTORY_DIR = OUTPUT_DIR / "history"

for d in [OUTPUT_DIR, SAMPLES_DIR, HISTORY_DIR]:
    d.mkdir(parents=True, exist_ok=True)


# ============================================================
# API KEY
# ============================================================
def get_api_key():
    # Check environment variable first
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    
    # Check config file
    config_path = BASE_DIR / "linkedin_config.json"
    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
            key = config.get("anthropic_api_key", "")
            if key:
                return key
    
    # Ask user
    print("\n" + "=" * 60)
    print("  ANTHROPIC API KEY REQUIRED")
    print("=" * 60)
    print("\n  Paste your Anthropic API key:")
    print("  (starts with sk-ant-)")
    key = input("\n  API Key: ").strip()
    
    # Save for next time
    save = input("  Save key for future runs? (y/n): ").strip().lower()
    if save == "y":
        with open(config_path, "w") as f:
            json.dump({"anthropic_api_key": key}, f)
        print(f"  Saved to {config_path}")
    
    return key


# ============================================================
# DATA LOADING
# ============================================================
def find_latest_care_folder():
    if not CARE_OUTPUT.exists():
        return None
    folders = sorted([f for f in CARE_OUTPUT.iterdir() if f.is_dir()], reverse=True)
    return folders[0] if folders else None


def load_care_uk(folder):
    if not folder:
        return None
    path = folder / "UK" / "UK_CQC_POORLY_RATED.csv"
    if path.exists():
        return pd.read_csv(path, low_memory=False, on_bad_lines="skip")
    return None


def load_care_ireland(folder):
    if not folder:
        return None
    path = folder / "Ireland" / "IRL_HIQA_ALL_CENTRES.csv"
    if path.exists():
        return pd.read_csv(path, low_memory=False, on_bad_lines="skip")
    return None


def load_care_ireland_expiring(folder):
    if not folder:
        return None
    path = folder / "Ireland" / "IRL_HIQA_EXPIRING_90_DAYS.csv"
    if path.exists():
        return pd.read_csv(path, low_memory=False, on_bad_lines="skip")
    return None


def load_busreg_monthly():
    data = {}
    for country, folder in BUSREG_FOLDERS.items():
        if not folder.exists():
            continue
        for f in folder.glob("*LAST_30_DAYS*.csv"):
            try:
                df = pd.read_csv(f, low_memory=False, on_bad_lines="skip")
                data[country] = {"count": len(df), "file": f.name}
            except:
                continue
    return data


# ============================================================
# STATS EXTRACTION
# ============================================================
def get_care_uk_stats(df):
    stats = {"total": len(df)}
    regions = {}
    ratings = {}
    cities = {}
    for _, row in df.iterrows():
        r = str(row.get("Region", "")).strip()
        rat = str(row.get("Rating", "")).strip()
        c = str(row.get("City", "")).strip()
        if r and r != "nan": regions[r] = regions.get(r, 0) + 1
        if rat and rat != "nan": ratings[rat] = ratings.get(rat, 0) + 1
        if c and c != "nan": cities[c] = cities.get(c, 0) + 1
    stats["regions"] = dict(sorted(regions.items(), key=lambda x: x[1], reverse=True))
    stats["ratings"] = ratings
    stats["inadequate"] = ratings.get("Inadequate", 0)
    stats["requires_imp"] = ratings.get("Requires improvement", 0) + ratings.get("Requires Improvement", 0)
    stats["cities"] = dict(sorted(cities.items(), key=lambda x: x[1], reverse=True)[:20])
    
    # Enrichment coverage stats
    total = len(df)
    stats["has_phone"] = int(df["Phone"].notna().sum()) if "Phone" in df.columns else 0
    stats["has_website"] = int(df["Website"].notna().sum()) if "Website" in df.columns else 0
    stats["has_email"] = int(df["Email"].notna().sum()) if "Email" in df.columns else 0
    stats["has_beds"] = int(df["Beds"].notna().sum()) if "Beds" in df.columns else 0
    stats["has_manager"] = int(df["Registered_Manager"].notna().sum()) if "Registered_Manager" in df.columns else 0
    stats["has_nominated"] = int(df["Nominated_Individual"].notna().sum()) if "Nominated_Individual" in df.columns else 0
    
    # Also count non-empty strings (some fields might have empty strings not NaN)
    for field, key in [("Phone", "has_phone"), ("Website", "has_website"), ("Email", "has_email"),
                       ("Beds", "has_beds"), ("Registered_Manager", "has_manager"), ("Nominated_Individual", "has_nominated")]:
        if field in df.columns:
            filled = df[field].apply(lambda x: bool(str(x).strip()) and str(x).strip() != "nan")
            stats[key] = int(filled.sum())
    
    stats["phone_pct"] = round(stats["has_phone"] / total * 100) if total else 0
    stats["website_pct"] = round(stats["has_website"] / total * 100) if total else 0
    stats["email_pct"] = round(stats["has_email"] / total * 100) if total else 0
    stats["beds_pct"] = round(stats["has_beds"] / total * 100) if total else 0
    stats["manager_pct"] = round(stats["has_manager"] / total * 100) if total else 0
    
    # Beds analysis
    if "Beds" in df.columns:
        beds_numeric = pd.to_numeric(df["Beds"], errors="coerce").dropna()
        if len(beds_numeric) > 0:
            stats["total_beds"] = int(beds_numeric.sum())
            stats["avg_beds"] = round(beds_numeric.mean(), 1)
            stats["median_beds"] = int(beds_numeric.median())
            stats["max_beds"] = int(beds_numeric.max())
            stats["min_beds"] = int(beds_numeric.min())
            # Beds by region
            if "Region" in df.columns:
                beds_by_region = {}
                for region in stats["regions"]:
                    region_beds = pd.to_numeric(df[df["Region"] == region]["Beds"], errors="coerce").dropna()
                    if len(region_beds) > 0:
                        beds_by_region[region] = {
                            "total": int(region_beds.sum()),
                            "avg": round(region_beds.mean(), 1),
                            "count": len(region_beds)
                        }
                stats["beds_by_region"] = beds_by_region
    
    # Specialisms breakdown
    if "Specialisms" in df.columns:
        all_specs = {}
        for specs_str in df["Specialisms"].dropna():
            for spec in str(specs_str).split(","):
                spec = spec.strip()
                if spec and spec != "nan":
                    all_specs[spec] = all_specs.get(spec, 0) + 1
        stats["specialisms"] = dict(sorted(all_specs.items(), key=lambda x: x[1], reverse=True)[:10])
    
    # Provider analysis (who runs the most poorly rated homes)
    if "Provider" in df.columns:
        providers = {}
        for _, row in df.iterrows():
            p = str(row.get("Provider", "")).strip()
            if p and p != "nan":
                providers[p] = providers.get(p, 0) + 1
        stats["top_providers"] = dict(sorted(providers.items(), key=lambda x: x[1], reverse=True)[:10])
        stats["unique_providers"] = len(providers)
    
    return stats


def get_care_irl_stats(df):
    stats = {"total": len(df)}
    counties = {}
    for _, row in df.iterrows():
        c = str(row.get("County", "")).strip()
        if c and c != "nan": counties[c] = counties.get(c, 0) + 1
    stats["counties"] = dict(sorted(counties.items(), key=lambda x: x[1], reverse=True))
    return stats


# ============================================================
# HISTORY / TREND TRACKING
# ============================================================
def save_monthly_snapshot(uk_stats, irl_stats, irl_expiring, busreg_data):
    """Save this month's numbers for future comparison"""
    today = datetime.now().strftime("%Y-%m")
    snapshot = {
        "date": today,
        "care_uk_total": uk_stats["total"] if uk_stats else 0,
        "care_uk_inadequate": uk_stats["inadequate"] if uk_stats else 0,
        "care_uk_requires_imp": uk_stats["requires_imp"] if uk_stats else 0,
        "care_uk_regions": uk_stats["regions"] if uk_stats else {},
        "care_uk_phone_pct": uk_stats.get("phone_pct", 0) if uk_stats else 0,
        "care_uk_website_pct": uk_stats.get("website_pct", 0) if uk_stats else 0,
        "care_uk_email_pct": uk_stats.get("email_pct", 0) if uk_stats else 0,
        "care_uk_beds_pct": uk_stats.get("beds_pct", 0) if uk_stats else 0,
        "care_uk_manager_pct": uk_stats.get("manager_pct", 0) if uk_stats else 0,
        "care_uk_total_beds": uk_stats.get("total_beds", 0) if uk_stats else 0,
        "care_uk_avg_beds": uk_stats.get("avg_beds", 0) if uk_stats else 0,
        "care_irl_total": irl_stats["total"] if irl_stats else 0,
        "care_irl_expiring": irl_expiring,
        "busreg": {c: d["count"] for c, d in busreg_data.items()} if busreg_data else {},
        "busreg_total": sum(d["count"] for d in busreg_data.values()) if busreg_data else 0,
    }
    
    path = HISTORY_DIR / f"{today}.json"
    with open(path, "w") as f:
        json.dump(snapshot, f, indent=2)
    return snapshot


def load_previous_snapshot():
    """Load last month's snapshot for comparison"""
    files = sorted(HISTORY_DIR.glob("*.json"), reverse=True)
    # Skip current month, get previous
    current = datetime.now().strftime("%Y-%m")
    for f in files:
        if f.stem != current:
            with open(f) as fh:
                return json.load(fh)
    return None


def calculate_changes(current, previous):
    """Calculate month-over-month changes"""
    if not previous:
        return None
    
    changes = {}
    
    # Care UK
    prev_uk = previous.get("care_uk_total", 0)
    curr_uk = current.get("care_uk_total", 0)
    if prev_uk > 0:
        changes["care_uk_diff"] = curr_uk - prev_uk
        changes["care_uk_pct"] = round((curr_uk - prev_uk) / prev_uk * 100, 1)
    
    # Care Ireland
    prev_irl = previous.get("care_irl_total", 0)
    curr_irl = current.get("care_irl_total", 0)
    if prev_irl > 0:
        changes["care_irl_diff"] = curr_irl - prev_irl
        changes["care_irl_pct"] = round((curr_irl - prev_irl) / prev_irl * 100, 1)
    
    # Bus reg
    prev_br = previous.get("busreg_total", 0)
    curr_br = current.get("busreg_total", 0)
    if prev_br > 0:
        changes["busreg_diff"] = curr_br - prev_br
        changes["busreg_pct"] = round((curr_br - prev_br) / prev_br * 100, 1)
    
    # Per country bus reg
    changes["busreg_countries"] = {}
    prev_countries = previous.get("busreg", {})
    curr_countries = current.get("busreg", {})
    for country in set(list(prev_countries.keys()) + list(curr_countries.keys())):
        p = prev_countries.get(country, 0)
        c = curr_countries.get(country, 0)
        if p > 0:
            changes["busreg_countries"][country] = {
                "diff": c - p,
                "pct": round((c - p) / p * 100, 1)
            }
    
    # Region changes
    changes["region_changes"] = {}
    prev_regions = previous.get("care_uk_regions", {})
    curr_regions = current.get("care_uk_regions", {})
    for region in set(list(prev_regions.keys()) + list(curr_regions.keys())):
        p = prev_regions.get(region, 0)
        c = curr_regions.get(region, 0)
        if p > 0:
            changes["region_changes"][region] = {
                "diff": c - p,
                "pct": round((c - p) / p * 100, 1)
            }
    
    return changes


# ============================================================
# WEB RESEARCH
# ============================================================
def search_web(query):
    """Simple web search using DuckDuckGo instant answers"""
    try:
        url = "https://api.duckduckgo.com/"
        params = {"q": query, "format": "json", "no_html": 1}
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        results = []
        if data.get("Abstract"):
            results.append(data["Abstract"])
        for topic in data.get("RelatedTopics", [])[:3]:
            if isinstance(topic, dict) and topic.get("Text"):
                results.append(topic["Text"])
        return results
    except:
        return []


def gather_web_research():
    """Search for current news and facts about care sector and business registrations"""
    print("\n  Searching for current industry news...")
    
    research = {}
    
    queries = [
        "CQC care home ratings 2026 UK",
        "UK care home sector challenges 2026",
        "new business registrations UK 2026 statistics",
        "HIQA Ireland care standards 2026",
        "care home staffing crisis UK",
    ]
    
    for query in queries:
        results = search_web(query)
        if results:
            research[query] = results
            print(f"    Found {len(results)} results for: {query}")
        time.sleep(0.5)
    
    return research


# ============================================================
# REDACTED SAMPLES
# ============================================================
def redact_phone(phone):
    phone = str(phone).strip()
    if not phone or phone == "nan": return ""
    if len(phone) > 6: return phone[:5] + " XXX " + phone[-2:]
    return "XXXX XXXX"

def redact_website(website):
    website = str(website).strip()
    if not website or website == "nan": return ""
    if "." in website:
        parts = website.split(".")
        if len(parts) >= 2:
            name = parts[-2]
            if len(name) > 4:
                return website.replace(name, name[:3] + "***")
    return "www.***.co.uk"

def redact_name(name):
    name = str(name).strip()
    if not name or name == "nan": return ""
    words = name.split()
    if len(words) > 2 and random.random() > 0.5:
        return words[0] + " *** " + words[-1]
    return name

def generate_redacted_sample(df, filename, num_rows=12):
    if df is None or len(df) == 0: return None
    sample = df.sample(min(num_rows, len(df)))
    redacted_rows = []
    for _, row in sample.iterrows():
        redacted = {}
        for col in sample.columns:
            val = str(row[col]).strip()
            if val == "nan": val = ""
            col_lower = col.lower()
            if "phone" in col_lower: redacted[col] = redact_phone(val)
            elif "website" in col_lower or "web" in col_lower or "url" in col_lower: redacted[col] = redact_website(val)
            elif "email" in col_lower: redacted[col] = "***@***.com" if val else ""
            elif "name" in col_lower and "provider" not in col_lower: redacted[col] = redact_name(val)
            else: redacted[col] = val
        redacted_rows.append(redacted)
    out_path = SAMPLES_DIR / filename
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(redacted_rows[0].keys()))
        w.writeheader()
        w.writerows(redacted_rows)
    return out_path


# ============================================================
# GPT POST GENERATION
# ============================================================
def generate_post(client, post_type, day_number, data_context, research_context, changes_context, specific_focus, previous_posts=None):
    """Send context to Claude Sonnet and get back a LinkedIn post"""
    
    system_prompt = """You ghostwrite LinkedIn posts for a data intelligence professional. Your job is to make each post sound like a real person wrote it — not an AI, not a marketing department, not a press release.

BANNED WORDS (using ANY of these means you failed — rewrite without them):
staggering, robust, stakeholders, landscape, noteworthy, underscores, showcasing, highlights, crucial, essential, vital, leveraging, navigating, realm, paramount, unveil, delve, insightful, harnessing, innovative, dynamic, comprehensive, strategic, significant, remarkable, substantial, impressive, striking, compelling, alarming, fascinating, intriguing, promising, pivotal, bustling, vibrant, ripe, surge, influx, interestingly, notably

BANNED PHRASES (rewrite any sentence containing these):
"key data points", "call to action", "data paints", "it's clear that", "it's worth noting", "this data shows", "these figures", "these numbers reflect", "this situation", "the implications", "the challenges we face", "the current state of", "a deeper dive", "explore this data", "the intersection of", "it's interesting to", "what's fascinating is", "what stands out", "what's striking", "it's eye-opening", "it's alarming", "on another note", "turning our attention", "turning our focus", "for those of us", "let's think about", "let's share insights", "let's learn from", "presents an opportunity", "presents both a challenge and an opportunity", "could really use some help", "shouldn't be overlooked", "when you think about it", "here's the thing", "imagine if you could", "the landscape of", "it's been an interesting", "I find it intriguing"

BANNED FILLER PATTERNS — do not use these sentence structures:
- "It's [adjective] to see/think/consider..." — just state the fact directly
- "What's [adjective] is that..." — just say the thing
- "Think about..." or "Imagine..." setups
- "Let's share ideas" or "Let's get the conversation going" — empty filler
- "For those of you who..." — sounds like a teacher addressing a class

STRUCTURE:
- 150-250 words. Every sentence must add information or a new thought. No padding.
- NO bullet points. NO bold text. NO numbered lists. Flowing prose only.
- ONE topic per post. Care posts = care ONLY. Business registration posts = business registrations ONLY. NEVER mix them unless the post type is "insight" with a cross-topic focus.
- MAX 3 statistics per post. Pick the most interesting ones.
- Start each post differently. NEVER open with: "It's", "As we", "This month", "In the UK", "The latest", "I just saw", "I've been", "There are currently", "How do you"
- LAST SENTENCE is a CTA. Use the CTA number matching the day number (mod 10). Day 1 = CTA 1, Day 2 = CTA 2, etc:
  1. "Message me if you want the full list for your area."
  2. "Happy to share a sample — just send me a message."
  3. "If this is relevant to your work, reach out and I'll send you a sample."
  4. "Send me a message if you want the breakdown for your region."
  5. "Reach out if you want the data for your area."
  6. "Want to see what this looks like for your region? Message me."
  7. "I share regional samples — just connect with me."
  8. "If you work in this space, message me and I'll send you a free sample."
  9. "Curious how your area compares? Send me a message."
  10. "Let me know your region and I'll send you a sample."
- No emojis. No hashtags.

ANTI-REPETITION RULES (CRITICAL — violating these means you failed):
- NEVER use "A care consultant told me" or "A consultant I know" more than ONCE across ALL posts. If PREVIOUS POSTS already used this framing, use a COMPLETELY different opening.
- NEVER open two posts the same way. Check the PREVIOUS POSTS section and use a different first sentence structure.
- NEVER repeat the same argument twice. If a previous post already made the "referrals are unreliable" point, the next value_prop must make a DIFFERENT argument.
- NEVER use the same anecdote framing twice. If one post starts with a story, the next must start with data, a question, or a direct statement.
- Use consistent stats: phone coverage is 97%, not 98%. Pick one number and stick to it across all posts.
- EVERY post MUST mention the country by name. Care posts say "UK" or "Ireland" explicitly. Busreg posts say "UK" or "Ireland" explicitly. Never assume the reader knows which country you're talking about from context alone. CQC alone is not enough — say "UK" alongside it. HIQA alone is not enough — say "Ireland" alongside it.

TONE:
Telling a friend something interesting you found. Not selling. Not reporting. Sharing. Short sentences mixed with longer ones.

BAD: "It's interesting to see that across the UK, there are currently 14,825 care homes..."
GOOD: "Nearly 2,900 care homes in the South East are rated below Good right now."

BAD: "This presents both a challenge and an opportunity for care professionals."
GOOD: "If you're a care consultant in the South East, these are your potential clients."

BAD: "What's fascinating is the variety of sectors these new businesses are likely entering."
GOOD: "50,000 new businesses registered in the UK last month. Every one of them needs an accountant."

EXAMPLES OF GOOD POSTS:

Example 1 (care_regional):
"The South East has nearly 2,900 care homes rated below Good. That's more than any other region by a wide margin. The North West comes second with around 2,100, and after that it drops off. Most of these homes are rated Requires Improvement rather than Inadequate, which means they could turn things around with the right support. If you're a care consultant working in the South East, the demand for your services is right where you are. Message me if you want the full list for your area."

Example 2 (busreg_regional):
"50,000 new businesses registered in the UK last month. Every single one of them needs an accountant within their first year. Most won't start looking until they're confused by their first VAT return, but by then someone else might already have the relationship. The registration data tells you who just started a business, where they're based, and when they incorporated. If you're an accountant in the UK and you're still relying on referrals, there are thousands of potential clients registering every 30 days who don't have an accountant yet. Send me a message if you want a sample of the latest registrations."

Example 3 (engagement):
"Question for accountants: when a new business registers with Companies House, how long does it typically take before they hire an accountant? I track new registrations monthly and I'm trying to understand the window. Is it week one? Month three? Tax season? The answer changes how useful fresh registration data actually is. Would love to hear from anyone who's been on the receiving end of this."

Example 4 (value_prop):
"Most care consultants I've spoken to find new clients the same way — referrals and word of mouth. It works, but it's unpredictable. Some months you get three new enquiries, other months nothing. Meanwhile there are over 14,000 care homes in the UK rated below Good right now. They need help. The question is whether you're going to wait for someone to refer them to you, or go to them directly. Want to see what this looks like for your region? Message me."
"""

    type_instructions = {
        "care_total": f"MONTHLY TOTAL UPDATE. Share the big UK number and top 3 regions. Briefly mention Ireland. This is the once-a-month overview post.\n\nFOCUS: {specific_focus}",
        
        "care_reminder": f"MID-MONTH REMINDER. Do NOT repeat the full breakdown. Pick ONE fresh angle — maybe Ireland's expiring registrations, maybe one specific region, maybe the gap between best and worst. Different from the monthly total post.\n\nFOCUS: {specific_focus}",
        
        "care_regional": f"REGIONAL DEEP DIVE. Talk about ONLY the region or city in the FOCUS. Compare it to one other region max. What makes it stand out? No business registrations.\n\nFOCUS: {specific_focus}",
        
        "busreg_total": f"MONTHLY BUSINESS REGISTRATION UPDATE. UK and Ireland ONLY — do not mention Australia or New Zealand. Target the SPECIFIC PROFESSION in the focus. Speak directly to that profession about why fresh registration data matters to them.\n\nFOCUS: {specific_focus}",
        
        "busreg_regional": f"BUSINESS REGISTRATION DEEP DIVE. UK or Ireland ONLY. Target the SPECIFIC PROFESSION in the focus. Make it hyper-relevant to that one profession. No care homes.\n\nFOCUS: {specific_focus}",
        
        "insight": f"INSIGHT POST. ONE observation. Not a data dump. Pick one interesting thing and talk about why it matters. Could reference web research if available.\n\nFOCUS: {specific_focus}",
        
        "engagement": f"QUESTION POST. Ask ONE specific question to the audience in FOCUS. Make it something people would actually want to answer. Lead with the question or build to it quickly. Don't dump stats first.\n\nTARGET AUDIENCE: {specific_focus}",
        
        "value_prop": f"VALUE POST. This post must use the SPECIFIC ANGLE below — do NOT default to the referral story. Each value_prop has a unique angle. Use it.\n\nANGLE: {specific_focus}",
    }

    prev_context = ""
    if previous_posts:
        # Show last 5 posts so Claude avoids repetition
        recent = previous_posts[-5:]
        prev_summaries = []
        for p in recent:
            first_line = p["text_no_hashtags"].split("\n")[0][:120]
            prev_summaries.append(f"  Day {p['day']} ({p['type']}): \"{first_line}...\"")
        prev_context = "PREVIOUS POSTS (do NOT repeat openings, arguments, or story framings from these):\n" + "\n".join(prev_summaries)

    user_prompt = f"""Write ONE LinkedIn post.

TYPE: {post_type}
DAY {day_number} of 30 (use CTA number {((day_number - 1) % 10) + 1} from the list)

{type_instructions.get(post_type, "Write a general insight post.")}

DATA (pick 2-3 numbers max — do NOT dump everything):
{data_context}

{f"CHANGES FROM LAST MONTH:{chr(10)}{changes_context}" if changes_context else "First month tracking. No comparison data yet."}

{f"INDUSTRY NEWS (use only if directly relevant):{chr(10)}{research_context}" if research_context else ""}

{prev_context}

CRITICAL REMINDERS:
- If this is a care post, do NOT mention business registrations at all
- If this is a busreg post, do NOT mention care homes at all
- If this is a busreg post, ONLY talk about UK and Ireland — NEVER mention Australia or New Zealand
- EVERY post must mention the country by name (UK or Ireland). Never leave it ambiguous.
- 150-250 words max. If you go over, cut filler sentences
- Check every sentence against the banned words list before including it
- Open with a fact or question, never with "It's [adjective]..."
- Sound like a person sharing something they noticed, not writing a report
- Phone coverage = 97%. Use this number consistently, not 98%.
- If PREVIOUS POSTS used "A consultant told me" or similar anecdote openings, do NOT use that framing again

Write ONLY the post text. No title. No labels."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            system=system_prompt,
            messages=[
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.9,
        )
        return response.content[0].text.strip()
    except Exception as e:
        print(f"    Claude error: {e}")
        return None


# ============================================================
# POST SCHEDULE WITH SPECIFIC FOCUS PER DAY
# ============================================================
def build_monthly_schedule(uk_stats, irl_stats, busreg_data):
    """Build 30 days of post types with specific focus angles"""
    
    regions = list(uk_stats["regions"].keys()) if uk_stats else ["South East", "North West", "East of England"]
    counties = list((irl_stats or {}).get("counties", {}).keys())[:5]
    countries = list(busreg_data.keys()) if busreg_data else ["UK", "Australia"]
    cities = list(uk_stats["cities"].keys())[:10] if uk_stats else ["London", "Birmingham"]
    
    schedule = [
        # Week 1: Care focus — lead with the new enriched data
        ("care_total", "Full UK + Ireland overview — include bed counts and data completeness stats"),
        ("care_regional", f"{regions[0]} — highest concentration of poorly rated homes in the UK, include beds and manager data"),
        ("insight", "We now have direct phone numbers for 97% of poorly rated care homes in the UK — what that means for consultants"),
        ("busreg_total", "UK and Ireland monthly total — targeted at ACCOUNTANTS specifically. How many new businesses need an accountant this month?"),
        ("engagement", "Care consultants in the UK — if you had every poorly rated home's phone, manager name, and bed count, what would you do first?"),
        ("care_regional", f"{regions[1]} vs {regions[2]} in the UK — comparing beds, managers, and contact data"),
        ("value_prop", "The referral ceiling — why word of mouth has limits when there are nearly 4,000 homes in the UK that need help"),
        
        # Week 2: Business registration + beds deep dive
        ("busreg_regional", "UK — targeted at INSURANCE BROKERS. Every new business needs insurance before they can trade. 50,000 registered last month."),
        ("care_reminder", "Ireland focus — expiring HIQA registrations in Ireland"),
        ("insight", "Average bed count in poorly rated UK homes vs the national average — what the gap tells us"),
        ("engagement", "Accountants in the UK and Ireland — how fast do new businesses appoint one after registering?"),
        ("busreg_regional", "Ireland — targeted at ACCOUNTANTS. 2,500 new businesses registered in Ireland last month, every one needs bookkeeping."),
        ("care_regional", f"{cities[0]} in the UK — city-level deep dive with beds and specialisms"),
        ("value_prop", "The named decision maker advantage — knowing the registered manager's name before you call changes the conversation entirely"),
        
        # Week 3: Specialisms + provider analysis
        ("care_total", "Monthly total refresh — focus on specialisms breakdown in UK care homes (mental health, learning disabilities, nursing)"),
        ("insight", "Which care home groups in the UK have the most poorly rated homes — provider concentration data"),
        ("care_regional", f"{regions[3] if len(regions) > 3 else regions[0]} in the UK — beds per home and service types"),
        ("engagement", "Recruitment agencies — is care home staffing in the UK your biggest sector?"),
        ("busreg_regional", "UK — targeted at BUSINESS FORMATION AGENTS. 50,000 businesses registered, how many used a formation service?"),
        ("value_prop", "Time cost — a consultant who spends 20 hours a month finding UK care home clients vs one who spends 2 hours calling from a list. Same skills, different results."),
        ("care_reminder", f"{counties[0] if counties else 'Dublin'} county deep dive — Ireland care centres"),
        
        # Week 4: Engagement + wrap up
        ("busreg_total", "End of month UK and Ireland business registration summary — targeted at SOLICITORS. New businesses need legal setup."),
        ("insight", "Homes rated Inadequate vs Requires Improvement in the UK — bed counts and what it means for residents"),
        ("care_regional", f"{cities[1] if len(cities) > 1 else cities[0]} vs {cities[2] if len(cities) > 2 else cities[0]} in the UK"),
        ("engagement", "Training providers — what compliance gaps do you see most in UK care homes with 20+ beds?"),
        ("value_prop", "The inspection cycle creates urgency — UK homes don't need you someday, they need you before their next CQC visit. That's a deadline you can see in the data."),
        ("care_reminder", "End of month UK care data reminder — emphasise email and website coverage"),
        ("insight", f"Ireland — {irl_stats['total'] if irl_stats else 0} centres tracked by HIQA and what the expiry data tells us"),
        
        # Extra days
        ("busreg_regional", "Ireland — targeted at INSURANCE BROKERS. 2,500 new Irish businesses need cover."),
        ("engagement", "Insurance brokers in the UK — how do you reach new businesses before your competitors?"),
        ("care_regional", f"{regions[4] if len(regions) > 4 else regions[0]} in the UK — beds, managers, and contact rates"),
    ]
    
    return schedule[:30]


# ============================================================
# HASHTAGS PER POST TYPE
# ============================================================
HASHTAGS = {
    "care_total": "#CareHomes #CQC #CareQuality",
    "care_reminder": "#CareHomes #HIQA #CareQuality",
    "care_regional": "#CareHomes #CQC #CareConsulting",
    "busreg_total": "#NewBusiness #Entrepreneurship #BusinessServices",
    "busreg_regional": "#NewBusiness #StartUp #BusinessGrowth",
    "insight": "#DataIntelligence #CareHomes #BusinessData",
    "engagement": "#BusinessDevelopment #LeadGeneration",
    "value_prop": "#CareConsulting #LeadGeneration #BusinessDevelopment",
}

# Engagement hashtags depend on content — detect from focus text
ENGAGEMENT_HASHTAGS_CARE = "#CareHomes #BusinessDevelopment #CareConsulting"
ENGAGEMENT_HASHTAGS_BUSREG = "#NewBusiness #BusinessDevelopment #LeadGeneration"


# ============================================================
# MAIN
# ============================================================
def main():
    import calendar
    
    today = datetime.now()
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    days_remaining = days_in_month - today.day + 1  # Include today
    
    print("=" * 60)
    print("  LINKEDIN CONTENT GENERATOR v5")
    print(f"  {today:%Y-%m-%d %H:%M}")
    print(f"  Generating {days_remaining} posts ({today.strftime('%b %d')} — {today.strftime('%b')} {days_in_month})")
    print("=" * 60)

    # --- Load data ---
    print("\n  Loading data...")
    
    latest_folder = find_latest_care_folder()
    care_uk_df = load_care_uk(latest_folder)
    care_irl_df = load_care_ireland(latest_folder)
    care_irl_exp_df = load_care_ireland_expiring(latest_folder)

    uk_stats = get_care_uk_stats(care_uk_df) if care_uk_df is not None else None
    irl_stats = get_care_irl_stats(care_irl_df) if care_irl_df is not None else None
    irl_expiring = len(care_irl_exp_df) if care_irl_exp_df is not None else 0

    busreg_data = load_busreg_monthly()
    busreg_total = sum(d["count"] for d in busreg_data.values())

    # Print summary
    if uk_stats:
        print(f"\n  UK Care: {uk_stats['total']:,} poorly rated")
        print(f"    Inadequate: {uk_stats['inadequate']:,}")
        print(f"    Requires Improvement: {uk_stats['requires_imp']:,}")
        print(f"    Phone: {uk_stats.get('has_phone', 0):,} ({uk_stats.get('phone_pct', 0)}%)")
        print(f"    Website: {uk_stats.get('has_website', 0):,} ({uk_stats.get('website_pct', 0)}%)")
        print(f"    Email: {uk_stats.get('has_email', 0):,} ({uk_stats.get('email_pct', 0)}%)")
        print(f"    Beds: {uk_stats.get('has_beds', 0):,} ({uk_stats.get('beds_pct', 0)}%)")
        print(f"    Manager: {uk_stats.get('has_manager', 0):,} ({uk_stats.get('manager_pct', 0)}%)")
        if "total_beds" in uk_stats:
            print(f"    Total beds: {uk_stats['total_beds']:,} (avg {uk_stats['avg_beds']})")
        if "unique_providers" in uk_stats:
            print(f"    Unique providers: {uk_stats['unique_providers']:,}")
    if irl_stats:
        print(f"  Ireland Care: {irl_stats['total']:,} centres")
        print(f"  Ireland Expiring: {irl_expiring}")
    print(f"\n  Business Registrations (last 30 days):")
    for c, d in sorted(busreg_data.items(), key=lambda x: x[1]["count"], reverse=True):
        print(f"    {c}: {d['count']:,}")
    print(f"    TOTAL: {busreg_total:,}")

    # --- Save snapshot & calculate changes ---
    print("\n  Saving monthly snapshot...")
    current_snapshot = save_monthly_snapshot(uk_stats, irl_stats, irl_expiring, busreg_data)
    
    previous = load_previous_snapshot()
    changes = calculate_changes(current_snapshot, previous)
    
    if changes:
        print(f"  Previous month found. Calculating changes...")
        if "care_uk_diff" in changes:
            print(f"    UK Care: {changes['care_uk_diff']:+,} ({changes['care_uk_pct']:+.1f}%)")
        if "busreg_diff" in changes:
            print(f"    Bus Reg: {changes['busreg_diff']:+,} ({changes['busreg_pct']:+.1f}%)")
    else:
        print("  First month — no previous data for comparison.")

    # --- Web research ---
    research = gather_web_research()

    # --- Build context strings ---
    data_context = ""
    if uk_stats:
        top_regions = "\n".join(f"  {r}: {c:,}" for r, c in list(uk_stats["regions"].items())[:10])
        top_cities = "\n".join(f"  {c}: {n:,}" for c, n in list(uk_stats["cities"].items())[:10])
        data_context += f"""UK CARE HOMES (CQC):
Total poorly rated: {uk_stats['total']:,}
Inadequate: {uk_stats['inadequate']:,}
Requires Improvement: {uk_stats['requires_imp']:,}

DATA COMPLETENESS:
Phone numbers: {uk_stats.get('has_phone', 0):,}/{uk_stats['total']:,} ({uk_stats.get('phone_pct', 0)}%)
Websites: {uk_stats.get('has_website', 0):,}/{uk_stats['total']:,} ({uk_stats.get('website_pct', 0)}%)
Emails: {uk_stats.get('has_email', 0):,}/{uk_stats['total']:,} ({uk_stats.get('email_pct', 0)}%)
Bed counts: {uk_stats.get('has_beds', 0):,}/{uk_stats['total']:,} ({uk_stats.get('beds_pct', 0)}%)
Registered managers: {uk_stats.get('has_manager', 0):,}/{uk_stats['total']:,} ({uk_stats.get('manager_pct', 0)}%)

BEDS ANALYSIS:
Total beds across poorly rated homes: {uk_stats.get('total_beds', 'N/A'):,}
Average beds per home: {uk_stats.get('avg_beds', 'N/A')}
Median beds: {uk_stats.get('median_beds', 'N/A')}
Range: {uk_stats.get('min_beds', 'N/A')} — {uk_stats.get('max_beds', 'N/A')}

Top regions:
{top_regions}

Top cities:
{top_cities}
"""
        # Add beds by region if available
        if "beds_by_region" in uk_stats:
            beds_region_lines = "\n".join(
                f"  {r}: {d['total']:,} beds across {d['count']} homes (avg {d['avg']})"
                for r, d in list(uk_stats["beds_by_region"].items())[:10]
            )
            data_context += f"\nBeds by region:\n{beds_region_lines}\n"
        
        # Add specialisms
        if "specialisms" in uk_stats:
            specs_lines = "\n".join(f"  {s}: {c:,} homes" for s, c in list(uk_stats["specialisms"].items())[:8])
            data_context += f"\nService types / specialisms:\n{specs_lines}\n"
        
        # Add top providers (groups running multiple poorly rated homes)
        if "top_providers" in uk_stats:
            provider_lines = "\n".join(f"  {p}: {c} poorly rated homes" for p, c in list(uk_stats["top_providers"].items())[:5])
            data_context += f"\nProviders with MOST poorly rated homes:\n{provider_lines}\nUnique providers: {uk_stats.get('unique_providers', 'N/A')}\n"
    
    if irl_stats:
        top_counties = "\n".join(f"  {c}: {n:,}" for c, n in list(irl_stats["counties"].items())[:10])
        data_context += f"""
IRELAND CARE CENTRES (HIQA):
Total centres: {irl_stats['total']:,}
Expiring in 90 days: {irl_expiring}

Top counties:
{top_counties}
"""
    
    if busreg_data:
        br_lines = "\n".join(f"  {c}: {d['count']:,}" for c, d in sorted(busreg_data.items(), key=lambda x: x[1]["count"], reverse=True))
        data_context += f"""
BUSINESS REGISTRATIONS (last 30 days):
Total: {busreg_total:,}
{br_lines}
"""

    changes_context = ""
    if changes:
        if "care_uk_diff" in changes:
            changes_context += f"UK Care homes: {changes['care_uk_diff']:+,} ({changes['care_uk_pct']:+.1f}%) from last month\n"
        if "care_irl_diff" in changes:
            changes_context += f"Ireland centres: {changes['care_irl_diff']:+,} ({changes['care_irl_pct']:+.1f}%) from last month\n"
        if "busreg_diff" in changes:
            changes_context += f"Business registrations total: {changes['busreg_diff']:+,} ({changes['busreg_pct']:+.1f}%) from last month\n"
        for country, cd in changes.get("busreg_countries", {}).items():
            changes_context += f"  {country}: {cd['diff']:+,} ({cd['pct']:+.1f}%)\n"
        for region, rd in list(changes.get("region_changes", {}).items())[:5]:
            changes_context += f"Region {region}: {rd['diff']:+,} ({rd['pct']:+.1f}%)\n"

    research_context = ""
    for query, results in research.items():
        research_context += f"\n{query}:\n"
        for r in results[:2]:
            research_context += f"  - {r}\n"

    # --- Get API key ---
    api_key = get_api_key()
    client = anthropic.Anthropic(api_key=api_key)

    # --- Generate posts for remaining days ---
    full_schedule = build_monthly_schedule(uk_stats, irl_stats, busreg_data)
    
    # Slice schedule to only remaining days
    start_day = today.day
    schedule = full_schedule[start_day - 1 : start_day - 1 + days_remaining]
    
    # If we're past the schedule length, cycle back
    if not schedule:
        schedule = full_schedule[:days_remaining]
    
    print(f"\n{'=' * 60}")
    print(f"  GENERATING {len(schedule)} POSTS")
    print(f"  {today.strftime('%b %d')} — {today.strftime('%b')} {days_in_month}")
    print(f"{'=' * 60}")

    posts = []
    for i, (post_type, specific_focus) in enumerate(schedule):
        post_date = today + timedelta(days=i)
        day_number = i + 1
        
        print(f"\n  [{day_number}/{len(schedule)}] {post_type} — {post_date.strftime('%a %b %d')} — {specific_focus[:50]}")
        
        text = generate_post(
            client, post_type, day_number,
            data_context, research_context, changes_context,
            specific_focus, previous_posts=posts
        )
        
        if not text:
            print(f"    FAILED — skipping")
            continue
        
        # Add hashtags — smart assignment for engagement posts
        if post_type == "engagement":
            focus_lower = specific_focus.lower()
            if any(w in focus_lower for w in ["care", "cqc", "hiqa", "home", "bed", "recruit", "train"]):
                hashtags = ENGAGEMENT_HASHTAGS_CARE
            else:
                hashtags = ENGAGEMENT_HASHTAGS_BUSREG
        else:
            hashtags = HASHTAGS.get(post_type, "#DataIntelligence #LeadGeneration")
        text_with_hashtags = f"{text}\n\n{hashtags}"
        
        # Generate redacted samples for care and busreg posts
        sample = None
        if post_type in ["care_total", "care_reminder"] and care_uk_df is not None:
            cols = ["Name", "City", "Region", "Rating", "Phone", "Website", "Email", "Beds", "Registered_Manager", "Specialisms"]
            available = [c for c in cols if c in care_uk_df.columns]
            if available:
                sample = generate_redacted_sample(care_uk_df[available], f"care_sample_{post_date.strftime('%Y%m%d')}.csv")
        
        elif post_type in ["busreg_total", "busreg_regional"] and busreg_data:
            # Find the right country CSV for busreg samples
            for country, folder in BUSREG_FOLDERS.items():
                if not folder.exists():
                    continue
                for f in folder.glob("*LAST_30_DAYS*.csv"):
                    try:
                        df = pd.read_csv(f, low_memory=False, on_bad_lines="skip")
                        sample = generate_redacted_sample(df, f"busreg_sample_{post_date.strftime('%Y%m%d')}.csv")
                        break
                    except:
                        continue
                if sample:
                    break
        
        posts.append({
            "day": day_number,
            "date": post_date.strftime("%Y-%m-%d"),
            "weekday": post_date.strftime("%A"),
            "type": post_type,
            "text": text_with_hashtags,
            "text_no_hashtags": text,
            "hashtags": hashtags,
            "sample": str(sample) if sample else None,
            "chars": len(text_with_hashtags),
        })
        
        print(f"    Done ({len(text)} chars) {'+ sample' if sample else ''}")
        time.sleep(0.5)  # Rate limiting

    # --- Save posts ---
    month_str = today.strftime("%Y-%m")
    
    # Main posts file
    posts_file = OUTPUT_DIR / "posts.txt"
    with open(posts_file, "w", encoding="utf-8") as f:
        for p in posts:
            f.write(f"{'=' * 60}\n")
            f.write(f"{p['weekday']}, {p['date']} — {p['type']}\n")
            f.write(f"Characters: {p['chars']}\n")
            if p['sample']:
                f.write(f"ATTACH SAMPLE: {p['sample']}\n")
            f.write(f"{'=' * 60}\n\n")
            f.write(p['text'])
            f.write(f"\n\n{'─' * 60}\n\n")
    
    # Archive copy
    archive_file = OUTPUT_DIR / f"{month_str}_posts.txt"
    with open(archive_file, "w", encoding="utf-8") as f:
        for p in posts:
            f.write(f"{'=' * 60}\n")
            f.write(f"{p['weekday']}, {p['date']} — {p['type']}\n")
            f.write(f"Characters: {p['chars']}\n")
            if p['sample']:
                f.write(f"ATTACH SAMPLE: {p['sample']}\n")
            f.write(f"{'=' * 60}\n\n")
            f.write(p['text'])
            f.write(f"\n\n{'─' * 60}\n\n")
    
    # JSON for programmatic access
    json_file = OUTPUT_DIR / f"{month_str}_posts.json"
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(posts, f, indent=2, ensure_ascii=False)

    # --- Summary ---
    print(f"\n{'=' * 60}")
    print(f"  DONE")
    print(f"{'=' * 60}")
    print(f"\n  Posts generated: {len(posts)}")
    print(f"  Date range: {posts[0]['date']} — {posts[-1]['date']}" if posts else "")
    print(f"  Avg length: {sum(p['chars'] for p in posts) // len(posts) if posts else 0} chars")
    print(f"  Samples generated: {sum(1 for p in posts if p['sample'])}")
    print(f"\n  Files:")
    print(f"    {posts_file}")
    print(f"    {archive_file}")
    print(f"    {json_file}")
    print(f"    {HISTORY_DIR / f'{month_str}.json'}")
    print(f"    {SAMPLES_DIR}/")
    print(f"\n  Open posts.txt and copy one post per day into LinkedIn.")
    print(f"  Posts marked ATTACH SAMPLE have a redacted CSV in the samples folder.")
    print(f"  Screenshot the CSV and attach it as an image to the LinkedIn post.")
    print()


if __name__ == "__main__":
    main()
