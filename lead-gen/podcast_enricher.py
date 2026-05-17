"""
===============================================================================
PODCAST ENRICHER - Script 2 of 2
===============================================================================
Reads raw_podcasts.csv from Script 1 and enriches each podcast with:
  - Email addresses (from RSS feed, website scraping)
  - Email verification (syntax, MX, disposable, SMTP, catch-all)
  - Website URL (from RSS feed if missing)

Process:
  1. Fetch RSS feed → extract <itunes:email>, <itunes:owner>, website
  2. Scrape podcast website → find contact emails
  3. Verify all found emails (multi-layer)
  4. Save to enriched_podcasts.csv

Features:
  - Batches of 100, saves after each batch
  - Fully resumable if interrupted
  - Rate limited to avoid bans
  - Same email verification as business registration scrapers

Output: enriched_podcasts.csv in D:\Desktop\Podcast\results\

Author: Built for Peter's podcast automation business
===============================================================================
"""

import requests
import csv
import os
import sys
import re
import json
import time
import socket
import smtplib
import random
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# Optional: dns.resolver for MX lookups
try:
    import dns.resolver
    HAS_DNS = True
except ImportError:
    HAS_DNS = False
    print("WARNING: dnspython not installed. MX verification will be limited.")
    print("         Install with: pip install dnspython")


# =============================================================================
# CONFIGURATION
# =============================================================================

BASE_DIR = r"D:\Desktop\Podcast"
RESULTS_DIR = os.path.join(BASE_DIR, "results")
LOGS_DIR = os.path.join(BASE_DIR, "logs")

INPUT_FILE = os.path.join(RESULTS_DIR, "raw_podcasts.csv")
OUTPUT_FILE = os.path.join(RESULTS_DIR, "enriched_podcasts.csv")
PROGRESS_FILE = os.path.join(LOGS_DIR, "enricher_progress.json")
LOG_FILE = os.path.join(LOGS_DIR, "enricher_log.txt")

# Processing settings
BATCH_SIZE = 100                # Process & save every N podcasts
WORKERS = 100                   # Concurrent threads per batch
RSS_TIMEOUT = 15                # Timeout for RSS fetch (seconds)
WEB_TIMEOUT = 15                # Timeout for website scraping
DELAY_BETWEEN_PODCASTS = 0.5    # Delay between each podcast
DELAY_BETWEEN_BATCHES = 60      # 1 minute pause between batches of 100
SMTP_TIMEOUT = 10               # SMTP verification timeout
MAX_RETRIES = 2                 # Retries per request


# =============================================================================
# DISPOSABLE EMAIL DOMAINS - Expanded list
# =============================================================================

DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "guerrillamail.net", "tempmail.com",
    "throwaway.email", "yopmail.com", "sharklasers.com", "grr.la",
    "guerrillamailblock.com", "pokemail.net", "spam4.me", "bccto.me",
    "trashmail.com", "trashmail.me", "trashmail.net", "dispostable.com",
    "maildrop.cc", "mailnesia.com", "mintemail.com", "temp-mail.org",
    "tempail.com", "tempmailaddress.com", "tmpmail.net", "tmpmail.org",
    "getnada.com", "10minutemail.com", "mohmal.com", "emailondeck.com",
    "mailcatch.com", "tempinbox.com", "fakeinbox.com", "mailforspam.com",
    "safetymail.info", "trashmail.org", "mailexpire.com", "tempmailo.com",
    "harakirimail.com", "mailnull.com", "spamgourmet.com", "mailzilla.com",
    "jetable.org", "trash-mail.com", "mytemp.email", "tempr.email",
    "discard.email", "discardmail.com", "discardmail.de", "emailfake.com",
    "guerrillamail.info", "guerrillamail.biz", "guerrillamail.de",
    "guerrillamail.org", "mailtemp.info", "mailtothis.com",
}

# Role-based prefixes (flag but don't reject)
ROLE_PREFIXES = {
    "info", "contact", "support", "admin", "hello", "help", "sales",
    "marketing", "press", "media", "team", "office", "general",
    "enquiries", "enquiry", "billing", "accounts", "feedback",
    "service", "webmaster", "postmaster", "abuse", "noreply", "no-reply",
}

# Common podcast hosting domains (catch-all - flag as low quality)
PODCAST_HOSTING_DOMAINS = {
    "anchor.fm", "spotify.com", "podbean.com", "buzzsprout.com",
    "libsyn.com", "spreaker.com", "transistor.fm", "simplecast.com",
    "megaphone.fm", "omnystudio.com", "captivate.fm", "fireside.fm",
    "podcastone.com", "acast.com", "blubrry.com", "ivoox.com",
    "podomatic.com", "soundcloud.com", "stitcher.com",
}


# =============================================================================
# LOGGING
# =============================================================================

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


# =============================================================================
# PROGRESS / RESUME
# =============================================================================

def load_progress():
    """Load enrichment progress"""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {
        "processed_count": 0,
        "emails_found": 0,
        "emails_verified": 0,
        "emails_invalid": 0,
        "rss_emails": 0,
        "website_emails": 0,
        "no_email_found": 0,
        "rss_fetch_errors": 0,
        "web_fetch_errors": 0,
    }


def save_progress(progress):
    """Save progress"""
    try:
        with open(PROGRESS_FILE, "w") as f:
            json.dump(progress, f, indent=2)
    except:
        pass


# =============================================================================
# RSS FEED PARSING
# =============================================================================

def fetch_rss(rss_url):
    """Fetch and return RSS feed content"""
    if not rss_url or not rss_url.startswith("http"):
        return None

    headers = {
        "User-Agent": "PodcastEnricher/1.0 (contact research tool)",
        "Accept": "application/rss+xml, application/xml, text/xml, */*"
    }

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(
                rss_url,
                headers=headers,
                timeout=RSS_TIMEOUT,
                allow_redirects=True
            )
            if resp.status_code == 200:
                return resp.text
            elif resp.status_code in [403, 429]:
                time.sleep(3)
        except requests.exceptions.Timeout:
            pass
        except requests.exceptions.RequestException:
            pass
        except Exception:
            pass

    return None


def extract_from_rss(rss_content):
    """Extract email and website from RSS feed XML"""
    result = {
        "rss_email": "",
        "rss_website": "",
        "rss_owner_name": "",
        "rss_owner_email": "",
    }

    if not rss_content:
        return result

    # --- Method 1: Regex extraction (more reliable than XML parsing for messy feeds) ---

    # Extract <itunes:email> tag
    itunes_email_match = re.search(
        r'<itunes:email[^>]*>([^<]+)</itunes:email>',
        rss_content, re.IGNORECASE
    )
    if itunes_email_match:
        email = itunes_email_match.group(1).strip()
        if is_valid_email_syntax(email):
            result["rss_owner_email"] = email

    # Extract <itunes:name> inside <itunes:owner>
    owner_block = re.search(
        r'<itunes:owner>(.*?)</itunes:owner>',
        rss_content, re.IGNORECASE | re.DOTALL
    )
    if owner_block:
        name_match = re.search(
            r'<itunes:name[^>]*>([^<]+)</itunes:name>',
            owner_block.group(1), re.IGNORECASE
        )
        if name_match:
            result["rss_owner_name"] = name_match.group(1).strip()

        # Also check for email inside owner block
        email_match = re.search(
            r'<itunes:email[^>]*>([^<]+)</itunes:email>',
            owner_block.group(1), re.IGNORECASE
        )
        if email_match:
            email = email_match.group(1).strip()
            if is_valid_email_syntax(email):
                result["rss_owner_email"] = email

    # Extract <link> tag for website
    link_matches = re.findall(
        r'<link[^>]*>([^<]+)</link>',
        rss_content, re.IGNORECASE
    )
    for link in link_matches:
        link = link.strip()
        if link.startswith("http") and not any(h in link for h in [
            "anchor.fm", "feeds.", "feed.", "rss.", "podcast.",
            "libsyn.com", "podbean.com", "buzzsprout.com", "spreaker.com",
            "transistor.fm", "feedburner.com", "feedpress.com"
        ]):
            result["rss_website"] = link
            break

    # Also try atom:link
    if not result["rss_website"]:
        atom_links = re.findall(
            r'<atom:link[^>]+href=["\']([^"\']+)["\']',
            rss_content, re.IGNORECASE
        )
        for link in atom_links:
            if link.startswith("http") and "feed" not in link.lower():
                result["rss_website"] = link
                break

    # Extract any email from description/content as fallback
    if not result["rss_owner_email"]:
        # Only look in the channel-level content, not episode descriptions
        channel_block = rss_content[:5000]  # Usually channel info is at the top
        emails_found = re.findall(
            r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}',
            channel_block
        )
        for email in emails_found:
            if is_valid_email_syntax(email) and not email.endswith(('.png', '.jpg', '.gif')):
                result["rss_email"] = email
                break

    # Use owner email as primary
    if result["rss_owner_email"]:
        result["rss_email"] = result["rss_owner_email"]

    return result


# =============================================================================
# WEBSITE SCRAPING FOR EMAILS
# =============================================================================

def scrape_website_for_email(url):
    """Scrape a podcast website to find contact email addresses"""
    if not url or not url.startswith("http"):
        return ""

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }

    emails_found = set()

    # Pages to check
    pages_to_try = [url]
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    # Add common contact pages
    for suffix in ["/contact", "/about", "/contact-us", "/about-us", "/connect", "/sponsor", "/advertise"]:
        pages_to_try.append(base_url + suffix)

    for page_url in pages_to_try:
        try:
            resp = requests.get(
                page_url,
                headers=headers,
                timeout=WEB_TIMEOUT,
                allow_redirects=True
            )
            if resp.status_code == 200:
                html = resp.text

                # Find all email addresses in HTML
                found = re.findall(
                    r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}',
                    html
                )
                for email in found:
                    email = email.lower().strip()
                    # Filter out image files and common false positives
                    if not email.endswith(('.png', '.jpg', '.gif', '.svg', '.webp', '.css', '.js')):
                        if not email.startswith(('wixpress', 'sentry', 'webpack', 'example')):
                            emails_found.add(email)

                # Also check mailto: links
                mailto_matches = re.findall(
                    r'mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
                    html, re.IGNORECASE
                )
                for email in mailto_matches:
                    emails_found.add(email.lower().strip())

                # If we found emails, don't need to check more pages
                if emails_found:
                    break

        except:
            continue

        time.sleep(0.3)  # Small delay between page requests

    if not emails_found:
        return ""

    # Prioritize: personal emails > info/contact > others
    personal = []
    generic = []
    other = []

    for email in emails_found:
        local = email.split("@")[0]
        domain = email.split("@")[1] if "@" in email else ""

        # Skip podcast hosting domains
        if any(h in domain for h in PODCAST_HOSTING_DOMAINS):
            continue

        if local in ROLE_PREFIXES:
            generic.append(email)
        elif any(c.isdigit() for c in local) and len(local) > 15:
            other.append(email)  # Likely auto-generated
        else:
            personal.append(email)

    # Return best email found
    if personal:
        return personal[0]
    elif generic:
        return generic[0]
    elif other:
        return other[0]

    return ""


# =============================================================================
# EMAIL VERIFICATION (Same as business registration scrapers)
# =============================================================================

def is_valid_email_syntax(email):
    """Check basic email syntax"""
    if not email or not isinstance(email, str):
        return False
    pattern = r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email.strip()) is not None


def check_disposable(email):
    """Check if email domain is disposable"""
    try:
        domain = email.split("@")[1].lower()
        return domain in DISPOSABLE_DOMAINS
    except:
        return False


def check_role_based(email):
    """Check if email is role-based (info@, support@, etc.)"""
    try:
        local = email.split("@")[0].lower()
        return local in ROLE_PREFIXES
    except:
        return False


def check_mx_records(email):
    """Check if domain has valid MX records"""
    if not HAS_DNS:
        return True, []  # Skip if dnspython not installed

    try:
        domain = email.split("@")[1]
        mx_records = dns.resolver.resolve(domain, "MX")
        mx_list = [str(r.exchange).rstrip(".") for r in mx_records]
        return True, mx_list
    except dns.resolver.NXDOMAIN:
        return False, []
    except dns.resolver.NoAnswer:
        return False, []
    except dns.resolver.NoNameservers:
        return False, []
    except Exception:
        return False, []


def check_smtp_mailbox(email, mx_host):
    """Verify mailbox exists via SMTP RCPT TO"""
    try:
        mx_host = mx_host.rstrip(".")
        server = smtplib.SMTP(timeout=SMTP_TIMEOUT)
        server.set_debuglevel(0)
        server.connect(mx_host, 25)
        server.helo("mail.example.com")
        server.mail("verify@example.com")
        code, message = server.rcpt(email)
        server.quit()

        if code == 250:
            return "valid", "Mailbox exists"
        elif code in [451, 452]:
            return "unknown", "Temporary error (assumed valid)"
        elif code == 550:
            return "invalid", "Mailbox does not exist"
        else:
            return "unknown", f"SMTP code {code}"

    except smtplib.SMTPServerDisconnected:
        return "unknown", "Server disconnected"
    except smtplib.SMTPConnectError:
        return "unknown", "Cannot connect"
    except socket.timeout:
        return "unknown", "Timeout"
    except Exception as e:
        return "unknown", f"Error: {str(e)[:50]}"


def check_catch_all(domain, mx_host):
    """Check if domain is catch-all (accepts any email)"""
    random_user = f"zzztest{random.randint(100000, 999999)}@{domain}"
    try:
        mx_host = mx_host.rstrip(".")
        server = smtplib.SMTP(timeout=SMTP_TIMEOUT)
        server.set_debuglevel(0)
        server.connect(mx_host, 25)
        server.helo("mail.example.com")
        server.mail("verify@example.com")
        code, message = server.rcpt(random_user)
        server.quit()

        if code == 250:
            return True  # Accepts anything = catch-all
        return False

    except:
        return False  # Assume not catch-all if we can't check


def verify_email(email):
    """Full email verification pipeline"""
    result = {
        "email": email,
        "status": "unknown",
        "reason": "",
        "is_disposable": False,
        "is_role_based": False,
        "is_catch_all": False,
    }

    if not email:
        result["status"] = "missing"
        result["reason"] = "No email found"
        return result

    email = email.strip().lower()
    result["email"] = email

    # Step 1: Syntax check
    if not is_valid_email_syntax(email):
        result["status"] = "invalid"
        result["reason"] = "Bad syntax"
        return result

    # Step 2: Disposable check
    if check_disposable(email):
        result["status"] = "disposable"
        result["reason"] = "Disposable email domain"
        result["is_disposable"] = True
        return result

    # Step 3: Role-based check (flag, don't reject)
    result["is_role_based"] = check_role_based(email)

    # Step 4: MX record check
    has_mx, mx_records = check_mx_records(email)
    if not has_mx:
        result["status"] = "invalid"
        result["reason"] = "No MX records (domain cannot receive email)"
        return result

    # Step 5: SMTP mailbox check
    if mx_records:
        mx_host = mx_records[0]
        status, reason = check_smtp_mailbox(email, mx_host)
        result["status"] = status
        result["reason"] = reason

        # Step 6: Catch-all check (only if mailbox appears valid)
        if status == "valid":
            domain = email.split("@")[1]
            result["is_catch_all"] = check_catch_all(domain, mx_host)
            if result["is_catch_all"]:
                result["status"] = "catch_all"
                result["reason"] = "Domain accepts all emails (catch-all)"
    else:
        result["status"] = "valid"
        result["reason"] = "MX records exist (SMTP check skipped)"

    return result


# =============================================================================
# MAIN ENRICHMENT LOGIC
# =============================================================================

def load_raw_podcasts():
    """Load raw podcasts from CSV"""
    podcasts = []
    try:
        with open(INPUT_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                podcasts.append(row)
    except Exception as e:
        log(f"ERROR loading {INPUT_FILE}: {e}")
        sys.exit(1)
    return podcasts


def load_already_processed():
    """Load set of already-processed podcast IDs"""
    processed = set()
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    processed.add(row.get("podcast_id", ""))
        except:
            pass
    return processed


def save_enriched_batch(batch, first_write=False):
    """Save enriched batch to CSV"""
    if not batch:
        return

    fieldnames = [
        "podcast_id", "title", "author", "description", "rss_url",
        "website", "apple_url", "language", "categories", "episode_count",
        "last_update", "last_update_date", "source",
        # Enriched fields
        "email", "email_source", "email_status", "email_reason",
        "is_role_based", "is_catch_all",
        "rss_owner_name", "rss_owner_email", "website_email",
        "enriched_at"
    ]

    mode = "w" if first_write else "a"
    write_header = first_write or not os.path.exists(OUTPUT_FILE)

    try:
        with open(OUTPUT_FILE, mode, newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            for row in batch:
                writer.writerow(row)
    except Exception as e:
        log(f"  ERROR saving batch: {e}")


def enrich_podcast(podcast):
    """Enrich a single podcast with email data"""
    enriched = {
        "podcast_id": podcast.get("podcast_id", ""),
        "title": podcast.get("title", ""),
        "author": podcast.get("author", ""),
        "description": podcast.get("description", ""),
        "rss_url": podcast.get("rss_url", ""),
        "website": podcast.get("website", ""),
        "apple_url": podcast.get("apple_url", ""),
        "language": podcast.get("language", ""),
        "categories": podcast.get("categories", ""),
        "episode_count": podcast.get("episode_count", ""),
        "last_update": podcast.get("last_update", ""),
        "last_update_date": podcast.get("last_update_date", ""),
        "source": podcast.get("source", ""),
        "email": "",
        "email_source": "",
        "email_status": "",
        "email_reason": "",
        "is_role_based": False,
        "is_catch_all": False,
        "rss_owner_name": "",
        "rss_owner_email": "",
        "website_email": "",
        "enriched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

    email_found = ""
    email_source = ""

    # --- Step 1: Fetch and parse RSS feed ---
    rss_url = podcast.get("rss_url", "")
    if rss_url:
        rss_content = fetch_rss(rss_url)
        if rss_content:
            rss_data = extract_from_rss(rss_content)
            enriched["rss_owner_name"] = rss_data.get("rss_owner_name", "")
            enriched["rss_owner_email"] = rss_data.get("rss_email", "")

            # Update website if we found one and didn't have it
            if rss_data.get("rss_website") and not enriched["website"]:
                enriched["website"] = rss_data["rss_website"]

            if rss_data.get("rss_email"):
                email_found = rss_data["rss_email"]
                email_source = "rss_feed"

    # --- Step 2: Scrape website for email (if no RSS email) ---
    if not email_found and enriched["website"]:
        website_email = scrape_website_for_email(enriched["website"])
        if website_email:
            enriched["website_email"] = website_email
            email_found = website_email
            email_source = "website"

    # --- Step 3: Verify the email ---
    if email_found:
        verification = verify_email(email_found)
        enriched["email"] = verification["email"]
        enriched["email_source"] = email_source
        enriched["email_status"] = verification["status"]
        enriched["email_reason"] = verification["reason"]
        enriched["is_role_based"] = verification["is_role_based"]
        enriched["is_catch_all"] = verification.get("is_catch_all", False)
    else:
        enriched["email_status"] = "not_found"
        enriched["email_reason"] = "No email found in RSS or website"

    return enriched


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("""
    ╔══════════════════════════════════════════════════════════════╗
    ║         PODCAST ENRICHER v1.0 - Script 2 of 2              ║
    ║                                                              ║
    ║  Finds emails: RSS feeds + website scraping                  ║
    ║  Verifies:     Syntax, MX, SMTP, disposable, catch-all      ║
    ║  Batches:      100 at a time, 100 threads per batch          ║
    ║  Resumable:    Stop anytime, picks up where it left off      ║
    ╚══════════════════════════════════════════════════════════════╝
    """)

    # Check dependencies
    if not HAS_DNS:
        print("=" * 60)
        print("RECOMMENDED: Install dnspython for full email verification")
        print("  Run: pip install dnspython")
        print("  Without it, MX record checks will be skipped.")
        print("=" * 60)
        print()
        response = input("Continue anyway? (y/n): ").strip().lower()
        if response != "y":
            sys.exit(0)

    # Check input file
    if not os.path.exists(INPUT_FILE):
        print(f"ERROR: Input file not found: {INPUT_FILE}")
        print("       Run podcast_scraper.py first to generate raw_podcasts.csv")
        sys.exit(1)

    # Load data
    log("Loading raw podcasts...")
    raw_podcasts = load_raw_podcasts()
    log(f"  Loaded {len(raw_podcasts)} podcasts from raw_podcasts.csv")

    already_processed = load_already_processed()
    log(f"  Already processed: {len(already_processed)}")

    # Filter out already processed
    to_process = [p for p in raw_podcasts if p.get("podcast_id", "") not in already_processed]
    log(f"  Remaining to process: {len(to_process)}")

    if not to_process:
        log("Nothing to process! All podcasts already enriched.")
        return

    progress = load_progress()
    progress["processed_count"] = len(already_processed)

    log(f"\nStarting enrichment...")
    log(f"  Batch size: {BATCH_SIZE}")
    log(f"  Workers per batch: {WORKERS}")
    log(f"  Delay between batches: {DELAY_BETWEEN_BATCHES}s")

    start_time = time.time()
    total_to_do = len(to_process)
    total_batches = (total_to_do + BATCH_SIZE - 1) // BATCH_SIZE

    # Process in batches of BATCH_SIZE, each batch runs WORKERS threads
    for batch_num in range(total_batches):
        batch_start = batch_num * BATCH_SIZE
        batch_end = min(batch_start + BATCH_SIZE, total_to_do)
        batch = to_process[batch_start:batch_end]

        overall_pos = len(already_processed) + batch_start + 1
        elapsed = time.time() - start_time
        batches_done = batch_num
        if batches_done > 0:
            time_per_batch = elapsed / batches_done
            remaining_batches = total_batches - batch_num
            eta_seconds = remaining_batches * time_per_batch
            eta_hours = int(eta_seconds // 3600)
            eta_mins = int((eta_seconds % 3600) // 60)
            eta_str = f"ETA: {eta_hours}h {eta_mins}m"
        else:
            eta_str = "ETA: calculating..."

        log(f"\n  BATCH {batch_num + 1}/{total_batches} | Podcasts {overall_pos}-{overall_pos + len(batch) - 1} of {len(raw_podcasts)} | {eta_str}")

        # --- Run batch with thread pool ---
        batch_results = []
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            future_to_podcast = {
                executor.submit(enrich_podcast, podcast): podcast
                for podcast in batch
            }

            for future in as_completed(future_to_podcast):
                podcast = future_to_podcast[future]
                try:
                    result = future.result()
                    batch_results.append(result)
                except Exception as e:
                    title = podcast.get("title", "Unknown")[:40]
                    log(f"    ERROR: {title} - {e}")
                    # Add error row
                    batch_results.append({
                        "podcast_id": podcast.get("podcast_id", ""),
                        "title": podcast.get("title", ""),
                        "author": podcast.get("author", ""),
                        "description": podcast.get("description", ""),
                        "rss_url": podcast.get("rss_url", ""),
                        "website": podcast.get("website", ""),
                        "apple_url": podcast.get("apple_url", ""),
                        "language": podcast.get("language", ""),
                        "categories": podcast.get("categories", ""),
                        "episode_count": podcast.get("episode_count", ""),
                        "last_update": podcast.get("last_update", ""),
                        "last_update_date": podcast.get("last_update_date", ""),
                        "source": podcast.get("source", ""),
                        "email": "", "email_source": "", "email_status": "error",
                        "email_reason": str(e)[:100], "is_role_based": False,
                        "is_catch_all": False, "rss_owner_name": "",
                        "rss_owner_email": "", "website_email": "",
                        "enriched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })

        # Save batch
        first_write = not os.path.exists(OUTPUT_FILE) or os.path.getsize(OUTPUT_FILE) == 0
        save_enriched_batch(batch_results, first_write=first_write)

        # Update stats
        for r in batch_results:
            if r.get("email"):
                progress["emails_found"] += 1
                if r.get("email_source") == "rss_feed":
                    progress["rss_emails"] += 1
                elif r.get("email_source") == "website":
                    progress["website_emails"] += 1
                if r.get("email_status") == "valid":
                    progress["emails_verified"] += 1
                elif r.get("email_status") == "invalid":
                    progress["emails_invalid"] += 1
            else:
                progress["no_email_found"] += 1

        progress["processed_count"] += len(batch_results)
        save_progress(progress)

        found_in_batch = sum(1 for b in batch_results if b.get("email"))
        verified_in_batch = sum(1 for b in batch_results if b.get("email_status") == "valid")

        log(f"     Emails found: {found_in_batch}/{len(batch_results)} | Verified: {verified_in_batch}")
        log(f"     Running totals: {progress['emails_found']} found | {progress['emails_verified']} verified | {progress['no_email_found']} not found")

        # Pause between batches (skip on last batch)
        if batch_num < total_batches - 1:
            log(f"     Pausing {DELAY_BETWEEN_BATCHES}s before next batch...")
            time.sleep(DELAY_BETWEEN_BATCHES)

    # --- Final Report ---
    elapsed = time.time() - start_time
    hours = int(elapsed // 3600)
    minutes = int((elapsed % 3600) // 60)

    total_processed = progress["processed_count"]
    email_rate = (progress["emails_found"] / total_processed * 100) if total_processed > 0 else 0
    verify_rate = (progress["emails_verified"] / progress["emails_found"] * 100) if progress["emails_found"] > 0 else 0

    log("\n" + "=" * 70)
    log("ENRICHMENT COMPLETE!")
    log("=" * 70)
    log(f"  Total processed:     {total_processed}")
    log(f"  Emails found:        {progress['emails_found']} ({email_rate:.1f}%)")
    log(f"    From RSS feeds:    {progress['rss_emails']}")
    log(f"    From websites:     {progress['website_emails']}")
    log(f"  Emails verified:     {progress['emails_verified']} ({verify_rate:.1f}% of found)")
    log(f"  Emails invalid:      {progress['emails_invalid']}")
    log(f"  No email found:      {progress['no_email_found']}")
    log(f"  Time elapsed:        {hours}h {minutes}m")
    log(f"  Output file:         {OUTPUT_FILE}")
    log(f"\n  Your enriched podcast list is ready for outreach!")


if __name__ == "__main__":
    main()
