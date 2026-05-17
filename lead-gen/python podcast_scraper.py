"""
===============================================================================
PODCAST SCRAPER - Script 1 of 2
===============================================================================
Collects active English-speaking podcasts from multiple sources:
  - Podcast Index API (primary - 4.7M+ podcasts)
  - Apple iTunes Search API (secondary - niche keyword searches)

Filters:
  - English language only
  - Active in last 6 months (published episode recently)
  - Deduplicates across all sources

Output: raw_podcasts.csv in D:\Desktop\Podcast\results\

Author: Built for Peter's podcast automation business
===============================================================================
"""

import requests
import hashlib
import time
import json
import csv
import os
import sys
import random
from datetime import datetime, timedelta
from urllib.parse import quote_plus


# =============================================================================
# CONFIGURATION - UPDATE THESE VALUES
# =============================================================================

# Podcast Index API credentials (get from https://api.podcastindex.org)
PI_API_KEY = "LPL9DRC9YVAX6URCBB8K"
PI_API_SECRET = "G2JU3aBU6zMKxqUQf8dNngfNM^HX8tPueXywyUNU"

# Paths
BASE_DIR = r"D:\Desktop\Podcast"
RESULTS_DIR = os.path.join(BASE_DIR, "results")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
SCRAPERS_DIR = os.path.join(BASE_DIR, "scrapers")
OUTPUT_FILE = os.path.join(RESULTS_DIR, "raw_podcasts.csv")
PROGRESS_FILE = os.path.join(LOGS_DIR, "scraper_progress.json")
LOG_FILE = os.path.join(LOGS_DIR, "scraper_log.txt")

# Scraping settings
BATCH_SIZE = 100                # Save to CSV every N new podcasts
REQUEST_DELAY = 1.5             # Seconds between API requests (be nice)
REQUEST_DELAY_APPLE = 2.0       # Apple is stricter
MAX_RETRIES = 3                 # Retry failed requests
ACTIVITY_CUTOFF_DAYS = 180      # 6 months - only keep active podcasts

# Target
TARGET_PODCASTS = 100000


# =============================================================================
# SEARCH KEYWORDS - Hundreds of niche terms to find unique podcasts
# =============================================================================

SEARCH_KEYWORDS = [
    # Business & Entrepreneurship
    "business", "entrepreneur", "startup", "marketing", "sales", "ecommerce",
    "real estate", "investing", "finance", "leadership", "management",
    "consulting", "freelance", "side hustle", "passive income", "dropshipping",
    "amazon fba", "shopify", "digital marketing", "seo", "content marketing",
    "social media marketing", "email marketing", "copywriting", "branding",
    "small business", "solopreneur", "bootstrapped", "venture capital",
    "private equity", "stock market", "cryptocurrency", "bitcoin", "trading",
    "accounting", "tax", "insurance", "mortgage", "financial planning",
    "wealth management", "retirement", "real estate investing", "rental property",
    "commercial real estate", "wholesale real estate", "house flipping",
    "business strategy", "growth hacking", "product management", "saas",
    "b2b sales", "cold calling", "negotiation", "networking", "career",
    "job interview", "remote work", "work from home", "digital nomad",

    # Technology
    "technology", "artificial intelligence", "machine learning", "data science",
    "programming", "software engineering", "web development", "python",
    "javascript", "cybersecurity", "cloud computing", "devops", "blockchain",
    "gaming", "tech news", "gadgets", "apple", "android", "robotics",
    "iot", "3d printing", "virtual reality", "augmented reality",
    "no code", "low code", "api", "database", "linux", "open source",

    # Health & Wellness
    "health", "fitness", "nutrition", "mental health", "meditation",
    "yoga", "weight loss", "running", "bodybuilding", "crossfit",
    "wellness", "self care", "therapy", "psychology", "anxiety",
    "depression", "adhd", "sleep", "biohacking", "longevity",
    "functional medicine", "holistic health", "plant based", "keto diet",
    "intermittent fasting", "supplements", "gut health", "hormones",

    # Personal Development
    "self improvement", "motivation", "mindset", "habits", "productivity",
    "time management", "goal setting", "confidence", "public speaking",
    "communication", "emotional intelligence", "stoicism", "philosophy",
    "reading", "book review", "journaling", "gratitude", "manifestation",
    "life coaching", "personal growth", "mindfulness",

    # Lifestyle & Culture
    "travel", "food", "cooking", "parenting", "relationships", "dating",
    "marriage", "divorce", "fashion", "beauty", "home design", "minimalism",
    "sustainability", "gardening", "pets", "dogs", "cats", "wine",
    "beer", "coffee", "photography", "art", "music", "film",
    "pop culture", "celebrity", "reality tv", "anime", "comics",

    # News & Politics
    "news", "politics", "current events", "economics", "geopolitics",
    "foreign policy", "democracy", "climate change", "environment",
    "social justice", "civil rights", "immigration", "education policy",

    # Science & Education
    "science", "physics", "biology", "chemistry", "astronomy", "space",
    "evolution", "neuroscience", "genetics", "climate science",
    "history", "archaeology", "anthropology", "linguistics", "mathematics",
    "philosophy of science", "critical thinking", "education",

    # Entertainment & Comedy
    "comedy", "improv", "standup comedy", "storytelling", "true crime",
    "mystery", "horror", "fiction", "fantasy", "science fiction",
    "dungeons and dragons", "tabletop games", "video games", "esports",
    "movie review", "tv review", "book club", "trivia",

    # Sports
    "sports", "football", "soccer", "basketball", "baseball", "hockey",
    "golf", "tennis", "mma", "boxing", "wrestling", "cycling",
    "marathon", "triathlon", "fantasy sports", "sports betting",
    "nfl", "nba", "premier league", "formula 1",

    # Religion & Spirituality
    "christianity", "bible", "faith", "church", "sermon",
    "spirituality", "buddhism", "hinduism", "islam", "jewish",
    "new age", "astrology", "tarot", "witchcraft",

    # Industry-Specific (niche - less overlap)
    "nursing", "medical", "dental", "veterinary", "pharmacy",
    "construction", "plumbing", "electrical", "hvac", "roofing",
    "trucking", "logistics", "supply chain", "manufacturing",
    "agriculture", "farming", "ranching", "forestry",
    "aviation", "pilot", "sailing", "fishing",
    "law", "legal", "attorney", "court", "criminal justice",
    "nonprofit", "charity", "volunteering", "social work",
    "military", "veteran", "first responder", "firefighter", "police",
    "teacher", "professor", "homeschool", "tutoring",
    "wedding", "event planning", "photography business",
    "tattoo", "barbershop", "salon", "spa",
    "auto repair", "mechanic", "car detailing",
    "cleaning business", "landscaping", "pest control",
    "restaurant", "food truck", "bakery", "brewery",
]

# Apple iTunes genre IDs for podcast categories
APPLE_GENRE_IDS = [
    1301, 1321, 1303, 1304, 1305, 1307, 1309, 1310, 1311, 1314,
    1315, 1316, 1318, 1320, 1323, 1324, 1325, 1401, 1402, 1403,
    1404, 1405, 1406, 1410, 1412, 1413, 1414, 1415, 1416, 1417,
    1418, 1420, 1421, 1438, 1439, 1440, 1441, 1442, 1443, 1444,
    1446, 1448, 1450, 1461, 1462, 1463, 1464, 1465, 1466, 1467,
    1468, 1469, 1470, 1471, 1472, 1473, 1474, 1475, 1476, 1477,
    1478, 1479, 1480, 1481, 1482, 1483, 1484, 1485, 1486, 1487,
    1488, 1489, 1490, 1491, 1492, 1493, 1494, 1495, 1496, 1497,
    1498, 1499, 1500, 1501, 1502, 1503, 1504, 1505, 1506, 1507,
    1508, 1509, 1510, 1511, 1512, 1513, 1514, 1515, 1516, 1517,
]

# Countries for Apple searches
APPLE_COUNTRIES = ["us", "gb", "ie", "au", "nz", "ca"]


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def ensure_dirs():
    """Create all necessary directories"""
    for d in [BASE_DIR, RESULTS_DIR, LOGS_DIR, SCRAPERS_DIR]:
        os.makedirs(d, exist_ok=True)


def log(message):
    """Log to console and file"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except:
        pass


def load_progress():
    """Load scraping progress for resume capability"""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {
        "pi_keywords_done": [],
        "apple_keywords_done": [],
        "apple_genres_done": [],
        "total_collected": 0,
        "total_duplicates": 0,
        "source_stats": {
            "podcast_index": 0,
            "apple_itunes": 0
        }
    }


def save_progress(progress):
    """Save progress for resume"""
    try:
        with open(PROGRESS_FILE, "w") as f:
            json.dump(progress, f, indent=2)
    except Exception as e:
        log(f"  WARNING: Could not save progress: {e}")


def load_existing_ids():
    """Load already-scraped podcast IDs from CSV to avoid duplicates"""
    ids = set()
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Use composite key: title_lower + rss_url for dedup
                    key = (row.get("title", "").lower().strip(), row.get("rss_url", "").strip())
                    ids.add(key)
        except Exception as e:
            log(f"  WARNING: Could not load existing data: {e}")
    return ids


def save_batch(podcasts, first_write=False):
    """Append a batch of podcasts to CSV"""
    if not podcasts:
        return

    fieldnames = [
        "podcast_id", "title", "author", "description", "rss_url",
        "website", "apple_url", "language", "categories", "episode_count",
        "last_update", "last_update_date", "source", "scraped_at"
    ]

    mode = "w" if first_write else "a"
    write_header = first_write or not os.path.exists(OUTPUT_FILE)

    try:
        with open(OUTPUT_FILE, mode, newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            for p in podcasts:
                writer.writerow(p)
    except Exception as e:
        log(f"  ERROR saving batch: {e}")


def is_active(last_update_timestamp):
    """Check if podcast has been active in the last 6 months"""
    if not last_update_timestamp or last_update_timestamp == 0:
        return False
    try:
        cutoff = datetime.now() - timedelta(days=ACTIVITY_CUTOFF_DAYS)
        last_update = datetime.fromtimestamp(int(last_update_timestamp))
        return last_update >= cutoff
    except:
        return False


def is_english(language):
    """Check if the podcast language is English"""
    if not language:
        return True  # Assume English if not specified
    lang = str(language).lower().strip()
    return lang.startswith("en") or lang in ["", "unknown"]


# =============================================================================
# PODCAST INDEX API
# =============================================================================

class PodcastIndexAPI:
    """Wrapper for Podcast Index API with auth handling"""

    BASE_URL = "https://api.podcastindex.org/api/1.0"

    def __init__(self, api_key, api_secret):
        self.api_key = api_key
        self.api_secret = api_secret

    def _get_headers(self):
        """Generate auth headers (key + secret + timestamp hashed)"""
        epoch_time = int(time.time())
        data_to_hash = self.api_key + self.api_secret + str(epoch_time)
        sha1_hash = hashlib.sha1(data_to_hash.encode("utf-8")).hexdigest()

        return {
            "User-Agent": "PodcastScraper/1.0",
            "X-Auth-Key": self.api_key,
            "X-Auth-Date": str(epoch_time),
            "Authorization": sha1_hash
        }

    def _request(self, endpoint, params=None):
        """Make authenticated request with retry logic"""
        url = f"{self.BASE_URL}/{endpoint}"

        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.get(
                    url,
                    headers=self._get_headers(),
                    params=params,
                    timeout=30
                )

                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code == 429:
                    wait = (attempt + 1) * 10
                    log(f"  Rate limited. Waiting {wait}s...")
                    time.sleep(wait)
                else:
                    log(f"  HTTP {resp.status_code} for {endpoint}")
                    time.sleep(3)

            except requests.exceptions.Timeout:
                log(f"  Timeout on attempt {attempt + 1}")
                time.sleep(5)
            except requests.exceptions.RequestException as e:
                log(f"  Request error: {e}")
                time.sleep(5)

        return None

    def search_by_term(self, term, clean=True):
        """Search podcasts by keyword term"""
        params = {"q": term, "max": 1000}
        if clean:
            params["clean"] = ""
        return self._request("search/byterm", params)

    def trending(self, max_results=1000, lang="en", cat=None):
        """Get trending podcasts"""
        params = {"max": max_results, "lang": lang}
        if cat:
            params["cat"] = cat
        return self._request("podcasts/trending", params)

    def recent_feeds(self, max_results=1000, lang="en"):
        """Get recently updated feeds"""
        # 'since' = unix timestamp for cutoff
        since = int((datetime.now() - timedelta(days=ACTIVITY_CUTOFF_DAYS)).timestamp())
        params = {"max": max_results, "lang": lang, "since": since}
        return self._request("recent/feeds", params)

    def get_categories(self):
        """Get all available categories"""
        return self._request("categories/list")


def parse_pi_podcast(feed, source_detail="search"):
    """Parse a Podcast Index feed into our standard format"""
    last_update = feed.get("lastUpdateTime") or feed.get("newestItemPubdate", 0)

    # Build categories string from the categories dict
    cats = feed.get("categories", {})
    if isinstance(cats, dict):
        cat_str = ", ".join(cats.values())
    elif isinstance(cats, list):
        cat_str = ", ".join(str(c) for c in cats)
    else:
        cat_str = str(cats) if cats else ""

    # Convert timestamp to readable date
    try:
        update_date = datetime.fromtimestamp(int(last_update)).strftime("%Y-%m-%d")
    except:
        update_date = ""

    return {
        "podcast_id": str(feed.get("id", "")),
        "title": str(feed.get("title", "")).strip(),
        "author": str(feed.get("author", "") or feed.get("ownerName", "")).strip(),
        "description": str(feed.get("description", ""))[:500].strip(),
        "rss_url": str(feed.get("url", "") or feed.get("originalUrl", "")).strip(),
        "website": str(feed.get("link", "")).strip(),
        "apple_url": f"https://podcasts.apple.com/podcast/id{feed.get('itunesId', '')}" if feed.get("itunesId") else "",
        "language": str(feed.get("language", "")).strip(),
        "categories": cat_str,
        "episode_count": str(feed.get("episodeCount", "")),
        "last_update": str(last_update),
        "last_update_date": update_date,
        "source": f"podcast_index_{source_detail}",
        "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }


# =============================================================================
# APPLE ITUNES API
# =============================================================================

def apple_search(term, country="us", limit=200):
    """Search Apple iTunes API for podcasts"""
    url = "https://itunes.apple.com/search"
    params = {
        "term": term,
        "country": country,
        "media": "podcast",
        "limit": limit
    }

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 403 or resp.status_code == 429:
                wait = (attempt + 1) * 15
                log(f"  Apple rate limited. Waiting {wait}s...")
                time.sleep(wait)
            else:
                log(f"  Apple HTTP {resp.status_code}")
                time.sleep(5)
        except Exception as e:
            log(f"  Apple request error: {e}")
            time.sleep(5)

    return None


def parse_apple_podcast(result, source_detail="search"):
    """Parse Apple iTunes result into our standard format"""
    release = result.get("releaseDate", "")
    try:
        update_date = release[:10] if release else ""
        update_ts = str(int(datetime.strptime(update_date, "%Y-%m-%d").timestamp())) if update_date else "0"
    except:
        update_date = ""
        update_ts = "0"

    genres = result.get("genres", [])
    # Filter out "Podcasts" from genre list
    genres = [g for g in genres if g.lower() != "podcasts"]

    return {
        "podcast_id": f"apple_{result.get('collectionId', '')}",
        "title": str(result.get("collectionName", "") or result.get("trackName", "")).strip(),
        "author": str(result.get("artistName", "")).strip(),
        "description": "",  # Apple API doesn't return descriptions in search
        "rss_url": str(result.get("feedUrl", "")).strip(),
        "website": "",  # Not in Apple search results
        "apple_url": str(result.get("collectionViewUrl", "")).strip(),
        "language": "",  # Not directly available
        "categories": ", ".join(genres),
        "episode_count": str(result.get("trackCount", "")),
        "last_update": update_ts,
        "last_update_date": update_date,
        "source": f"apple_{source_detail}",
        "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }


# =============================================================================
# MAIN SCRAPER LOGIC
# =============================================================================

def scrape_podcast_index(pi_api, existing_ids, progress):
    """Scrape Podcast Index API using keyword searches + trending + recent"""
    new_podcasts = []
    batch_buffer = []

    # --- Phase 1: Trending podcasts ---
    if "pi_trending" not in progress["pi_keywords_done"]:
        log("=" * 70)
        log("PHASE 1: Podcast Index - Trending Podcasts")
        log("=" * 70)

        result = pi_api.trending(max_results=1000, lang="en")
        if result and "feeds" in result:
            for feed in result["feeds"]:
                if not is_english(feed.get("language", "")):
                    continue
                if not is_active(feed.get("lastUpdateTime") or feed.get("newestItemPubdate", 0)):
                    continue

                parsed = parse_pi_podcast(feed, "trending")
                key = (parsed["title"].lower(), parsed["rss_url"])
                if key not in existing_ids and parsed["title"]:
                    existing_ids.add(key)
                    batch_buffer.append(parsed)
                    progress["source_stats"]["podcast_index"] += 1
                else:
                    progress["total_duplicates"] += 1

            log(f"  Trending: {len(batch_buffer)} new podcasts")

        progress["pi_keywords_done"].append("pi_trending")
        save_progress(progress)
        time.sleep(REQUEST_DELAY)

    # --- Phase 2: Recent feeds ---
    if "pi_recent" not in progress["pi_keywords_done"]:
        log("\n" + "=" * 70)
        log("PHASE 2: Podcast Index - Recent Feeds (last 6 months)")
        log("=" * 70)

        result = pi_api.recent_feeds(max_results=1000, lang="en")
        if result and "feeds" in result:
            count_before = len(batch_buffer)
            for feed in result["feeds"]:
                if not is_english(feed.get("language", "")):
                    continue

                parsed = parse_pi_podcast(feed, "recent")
                key = (parsed["title"].lower(), parsed["rss_url"])
                if key not in existing_ids and parsed["title"]:
                    existing_ids.add(key)
                    batch_buffer.append(parsed)
                    progress["source_stats"]["podcast_index"] += 1
                else:
                    progress["total_duplicates"] += 1

            log(f"  Recent feeds: {len(batch_buffer) - count_before} new podcasts")

        progress["pi_keywords_done"].append("pi_recent")
        save_progress(progress)
        time.sleep(REQUEST_DELAY)

    # --- Phase 3: Keyword searches ---
    log("\n" + "=" * 70)
    log(f"PHASE 3: Podcast Index - Keyword Searches ({len(SEARCH_KEYWORDS)} terms)")
    log("=" * 70)

    keywords_to_do = [k for k in SEARCH_KEYWORDS if k not in progress["pi_keywords_done"]]
    total_keywords = len(SEARCH_KEYWORDS)
    done_keywords = total_keywords - len(keywords_to_do)

    for i, keyword in enumerate(keywords_to_do):
        current = done_keywords + i + 1

        # Check if we've hit target
        total_so_far = progress["total_collected"] + len(batch_buffer)
        if total_so_far >= TARGET_PODCASTS:
            log(f"\n  TARGET REACHED: {total_so_far} podcasts collected!")
            break

        log(f"  [{current}/{total_keywords}] Searching: '{keyword}'...")
        result = pi_api.search_by_term(keyword)

        new_count = 0
        if result and "feeds" in result:
            for feed in result["feeds"]:
                if not is_english(feed.get("language", "")):
                    continue
                if not is_active(feed.get("lastUpdateTime") or feed.get("newestItemPubdate", 0)):
                    continue

                parsed = parse_pi_podcast(feed, f"keyword_{keyword}")
                key = (parsed["title"].lower(), parsed["rss_url"])
                if key not in existing_ids and parsed["title"]:
                    existing_ids.add(key)
                    batch_buffer.append(parsed)
                    new_count += 1
                    progress["source_stats"]["podcast_index"] += 1
                else:
                    progress["total_duplicates"] += 1

        log(f"           Found {result.get('count', 0) if result else 0} results, {new_count} new unique")

        progress["pi_keywords_done"].append(keyword)

        # Save batch every BATCH_SIZE new podcasts
        if len(batch_buffer) >= BATCH_SIZE:
            first_write = not os.path.exists(OUTPUT_FILE) or os.path.getsize(OUTPUT_FILE) == 0
            save_batch(batch_buffer, first_write=first_write)
            progress["total_collected"] += len(batch_buffer)
            log(f"  >> Saved batch of {len(batch_buffer)} | Total: {progress['total_collected']}")
            batch_buffer = []
            save_progress(progress)

        time.sleep(REQUEST_DELAY + random.uniform(0, 0.5))

    # Save remaining buffer
    if batch_buffer:
        first_write = not os.path.exists(OUTPUT_FILE) or os.path.getsize(OUTPUT_FILE) == 0
        save_batch(batch_buffer, first_write=first_write)
        progress["total_collected"] += len(batch_buffer)
        log(f"  >> Saved final batch of {len(batch_buffer)} | Total: {progress['total_collected']}")
        batch_buffer = []

    save_progress(progress)
    return progress["total_collected"]


def scrape_apple_itunes(existing_ids, progress):
    """Scrape Apple iTunes API with niche keyword searches across countries"""
    batch_buffer = []

    log("\n" + "=" * 70)
    log(f"PHASE 4: Apple iTunes - Keyword × Country Searches")
    log("=" * 70)

    # Use a subset of more niche keywords for Apple (the long tail)
    apple_keywords = [k for k in SEARCH_KEYWORDS if k not in progress.get("apple_keywords_done", [])]
    total_searches = len(apple_keywords) * len(APPLE_COUNTRIES)
    search_count = 0

    for keyword in apple_keywords:
        for country in APPLE_COUNTRIES:
            search_count += 1

            total_so_far = progress["total_collected"] + len(batch_buffer)
            if total_so_far >= TARGET_PODCASTS:
                log(f"\n  TARGET REACHED: {total_so_far} podcasts collected!")
                break

            if search_count % 20 == 0:
                log(f"  Apple search {search_count}: '{keyword}' in {country.upper()} | Buffer: {len(batch_buffer)} | Total: {total_so_far}")

            result = apple_search(keyword, country=country, limit=200)
            new_count = 0

            if result and "results" in result:
                for item in result["results"]:
                    if item.get("kind") != "podcast":
                        continue

                    parsed = parse_apple_podcast(item, f"keyword_{keyword}_{country}")

                    # Activity filter
                    if not is_active(parsed["last_update"]):
                        continue

                    key = (parsed["title"].lower(), parsed["rss_url"])
                    if key not in existing_ids and parsed["title"]:
                        existing_ids.add(key)
                        batch_buffer.append(parsed)
                        new_count += 1
                        progress["source_stats"]["apple_itunes"] += 1
                    else:
                        progress["total_duplicates"] += 1

            # Save batch
            if len(batch_buffer) >= BATCH_SIZE:
                first_write = not os.path.exists(OUTPUT_FILE) or os.path.getsize(OUTPUT_FILE) == 0
                save_batch(batch_buffer, first_write=first_write)
                progress["total_collected"] += len(batch_buffer)
                log(f"  >> Saved batch of {len(batch_buffer)} | Total: {progress['total_collected']}")
                batch_buffer = []
                save_progress(progress)

            time.sleep(REQUEST_DELAY_APPLE + random.uniform(0, 1.0))

        progress["apple_keywords_done"].append(keyword)
        save_progress(progress)

        # Check target after each keyword (all countries)
        if progress["total_collected"] + len(batch_buffer) >= TARGET_PODCASTS:
            break

    # Save remaining
    if batch_buffer:
        first_write = not os.path.exists(OUTPUT_FILE) or os.path.getsize(OUTPUT_FILE) == 0
        save_batch(batch_buffer, first_write=first_write)
        progress["total_collected"] += len(batch_buffer)
        log(f"  >> Saved final Apple batch of {len(batch_buffer)} | Total: {progress['total_collected']}")

    save_progress(progress)


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("""
    ╔══════════════════════════════════════════════════════════════╗
    ║           PODCAST SCRAPER v1.0 - Script 1 of 2             ║
    ║                                                              ║
    ║  Sources: Podcast Index API + Apple iTunes API               ║
    ║  Target:  100,000 active English-speaking podcasts           ║
    ║  Filter:  Active in last 6 months                            ║
    ╚══════════════════════════════════════════════════════════════╝
    """)

    # Validate API credentials
    if PI_API_KEY == "YOUR_API_KEY_HERE" or PI_API_SECRET == "YOUR_API_SECRET_HERE":
        print("ERROR: Please set your Podcast Index API key and secret in the script!")
        print("       Edit PI_API_KEY and PI_API_SECRET at the top of this file.")
        sys.exit(1)

    # Setup
    ensure_dirs()
    progress = load_progress()
    existing_ids = load_existing_ids()

    log(f"Starting scraper...")
    log(f"  Existing podcasts in CSV: {len(existing_ids)}")
    log(f"  Progress: {progress['total_collected']} collected, {progress['total_duplicates']} duplicates skipped")
    log(f"  Target: {TARGET_PODCASTS}")
    log(f"  Keywords: {len(SEARCH_KEYWORDS)}")
    log(f"  Activity filter: last {ACTIVITY_CUTOFF_DAYS} days")

    start_time = time.time()

    # --- Source 1: Podcast Index ---
    pi_api = PodcastIndexAPI(PI_API_KEY, PI_API_SECRET)

    log("\nTesting Podcast Index API connection...")
    test = pi_api.trending(max_results=1, lang="en")
    if test and test.get("status"):
        log("  Podcast Index API: CONNECTED")
    else:
        log("  WARNING: Podcast Index API connection failed. Check your credentials.")
        log("  Continuing with Apple iTunes only...")

    if test and test.get("status"):
        scrape_podcast_index(pi_api, existing_ids, progress)

    # --- Source 2: Apple iTunes ---
    if progress["total_collected"] < TARGET_PODCASTS:
        scrape_apple_itunes(existing_ids, progress)

    # --- Final Report ---
    elapsed = time.time() - start_time
    hours = int(elapsed // 3600)
    minutes = int((elapsed % 3600) // 60)

    log("\n" + "=" * 70)
    log("SCRAPING COMPLETE!")
    log("=" * 70)
    log(f"  Total podcasts collected: {progress['total_collected']}")
    log(f"  Duplicates skipped:       {progress['total_duplicates']}")
    log(f"  From Podcast Index:       {progress['source_stats']['podcast_index']}")
    log(f"  From Apple iTunes:        {progress['source_stats']['apple_itunes']}")
    log(f"  Time elapsed:             {hours}h {minutes}m")
    log(f"  Output file:              {OUTPUT_FILE}")
    log(f"\n  Next step: Run podcast_enricher.py to find & verify emails")


if __name__ == "__main__":
    main()