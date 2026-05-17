"""
Comprehensive Lead Enricher - FINAL VERSION
Batch processing with resume + fixed phone extraction
"""

import requests
import re
import csv
import time
import dns.resolver
import smtplib
from pathlib import Path
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import threading
from difflib import SequenceMatcher
from ddgs import DDGS

# Configuration
BASE_DIR = Path("D:/Desktop/Business Registrations")
ENRICHED_DIR = BASE_DIR / "5 - Enriched"
ENRICHED_DIR.mkdir(parents=True, exist_ok=True)

TIMEOUT = 10
MAX_WORKERS = 100
BATCH_SIZE = 100
BATCH_DELAY = 60

COUNTRIES = {
    'ireland': {
        'folder': '1 - Ireland Data',
        'last_30': 'ireland_businesses_LAST_30_DAYS.csv',
        'archive': 'ireland_businesses_ARCHIVE.csv',
        'name_col': 'company_name',
        'id_col': 'company_num',
        'address_cols': ['company_address_1', 'company_address_2', 'company_address_3', 'company_address_4']
    },
    'uk': {
        'folder': '2 - UK Data',
        'last_30': 'uk_businesses_LAST_30_DAYS.csv',
        'archive': 'uk_businesses_ARCHIVE.csv',
        'name_col': 'CompanyName',
        'id_col': ' CompanyNumber',
        'address_cols': [' RegAddress.AddressLine1', ' RegAddress.AddressLine2', ' RegAddress.PostTown', ' RegAddress.County', ' RegAddress.PostCode']
    },
    'australia': {
        'folder': '4 - Australia Data',
        'last_30': 'australia_businesses_LAST_30_DAYS.csv',
        'archive': 'australia_businesses_ARCHIVE.csv',
        'name_col': 'Company Name',
        'id_col': 'ACN',
        'address_cols': []
    },
    'new_zealand': {
        'folder': '3 - NZ Data',
        'last_30': 'nz_businesses_LAST_30_DAYS.csv',
        'archive': 'nz_businesses_ARCHIVE.csv',
        'name_col': 'ENTITY_NAME',
        'id_col': 'NZBN',
        'address_cols': []
    }
}

progress_lock = threading.Lock()
progress_counter = {'current': 0, 'total': 0, 'batch': 0}
enrichment_results = []

def get_month_code():
    """Get current month code"""
    now = datetime.now()
    return now.strftime("%b%Y").upper()

def load_already_enriched_ids(enriched_path, id_col):
    """Load IDs that are already enriched"""
    if not enriched_path.exists():
        return set(), []
    
    enriched_ids = set()
    existing_rows = []
    
    try:
        with open(enriched_path, 'r', encoding='utf-8', errors='ignore') as f:
            reader = csv.DictReader(f)
            for row in reader:
                company_id = str(row.get(id_col, '')).strip()
                if company_id:
                    enriched_ids.add(company_id)
                existing_rows.append(row)
    except:
        pass
    
    return enriched_ids, existing_rows

def count_new_leads(country_name, config):
    """Count how many NEW leads still need enriching"""
    country_folder = BASE_DIR / config['folder']
    last_30_path = country_folder / config['last_30']
    archive_path = country_folder / config['archive']
    
    if not last_30_path.exists():
        return 0
    
    month_code = get_month_code()
    output_filename = f"{country_name}_businesses_ENRICHED_{month_code}.csv"
    output_path = ENRICHED_DIR / output_filename
    
    already_enriched_ids, _ = load_already_enriched_ids(output_path, config['id_col'])
    
    archive_ids = set()
    if archive_path.exists():
        try:
            with open(archive_path, 'r', encoding='utf-8', errors='ignore') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    company_id = str(row.get(config['id_col'], '')).strip()
                    if company_id:
                        archive_ids.add(company_id)
        except:
            pass
    
    new_count = 0
    try:
        with open(last_30_path, 'r', encoding='utf-8', errors='ignore') as f:
            reader = csv.DictReader(f)
            for row in reader:
                company_id = str(row.get(config['id_col'], '')).strip()
                if company_id and company_id not in archive_ids and company_id not in already_enriched_ids:
                    new_count += 1
    except:
        pass
    
    return new_count

def extract_emails_from_text(text):
    """Extract all email addresses from text"""
    email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    emails = re.findall(email_pattern, text)
    
    obfuscated = re.findall(r'\b[\w.+-]+\s*\[at\]\s*[\w.-]+\s*\[dot\]\s*\w+\b', text, re.IGNORECASE)
    for email in obfuscated:
        clean = email.replace('[at]', '@').replace('[dot]', '.').replace(' ', '')
        emails.append(clean)
    
    obfuscated2 = re.findall(r'\b[\w.+-]+\(at\)[\w.-]+\(dot\)\w+\b', text, re.IGNORECASE)
    for email in obfuscated2:
        clean = email.replace('(at)', '@').replace('(dot)', '.').replace(' ', '')
        emails.append(clean)
    
    exclude_patterns = [
        'example.com', 'yourdomain.com', 'domain.com',
        'yourcompany.com', 'company.com', 'example.org',
        'wix.com', 'wordpress.com', 'shopify.com',
        'sentry.io', 'schema.org', 'w3.org', 'placeholder.com',
        '@sentry', '@example', '@test', '@domain'
    ]
    
    valid_emails = []
    seen = set()
    for email in emails:
        email_lower = email.lower().strip()
        if email_lower not in seen and not any(pattern in email_lower for pattern in exclude_patterns):
            valid_emails.append(email)
            seen.add(email_lower)
    
    return valid_emails

def extract_phones_from_text(text):
    """Extract phone numbers with strict validation"""
    
    patterns = [
        r'\+[\d\s\-\(\)]{10,18}',
        r'\b0\d{1,2}[\s\-]?\d{3,4}[\s\-]?\d{4}\b',
        r'\b1[38]00[\s\-]?\d{3}[\s\-]?\d{3}\b',
        r'\(\d{2,4}\)[\s\-]?\d{3,4}[\s\-]?\d{4}',
        r'\b\d{2,4}[\s\-\.]\d{3,4}[\s\-\.]\d{4}\b',
    ]
    
    potential_phones = []
    for pattern in patterns:
        matches = re.findall(pattern, text)
        potential_phones.extend(matches)
    
    phones_with_context = []
    for phone in potential_phones:
        index = text.lower().find(phone.lower())
        if index >= 0:
            context_start = max(0, index - 100)
            context_end = min(len(text), index + len(phone) + 100)
            context = text[context_start:context_end].lower()
            phones_with_context.append((phone, context))
    
    validated = []
    seen = set()
    
    for phone, context in phones_with_context:
        phone = phone.strip()
        digits_only = re.sub(r'\D', '', phone)
        
        if digits_only in seen:
            continue
        
        if not (10 <= len(digits_only) <= 15):
            continue
        
        if digits_only in ['2147483647', '2147483646', '2147483645', '9999999999']:
            continue
        
        if digits_only.startswith('20') and len(digits_only) >= 8:
            continue
        
        if len(set(digits_only)) <= 2:
            continue
        
        if len(digits_only) >= 9:
            chunk_size = 3
            chunk = digits_only[:chunk_size]
            if digits_only == chunk * (len(digits_only) // chunk_size):
                continue
        
        bad_context_words = [
            'file', 'size', 'byte', 'kb', 'mb', 'gb',
            'pixel', 'width', 'height', 'resolution',
            'download', 'upload', 'speed',
            'price', 'cost', '€', '$', '£',
            'year', 'date', 'time', 'hour',
            'id', 'code', 'number:', 'ref:',
            'version', 'build', 'release'
        ]
        
        if any(word in context for word in bad_context_words):
            continue
        
        good_context_words = [
            'phone', 'tel', 'call', 'mobile', 'fax',
            'contact', 'reach', 'ring', 'dial'
        ]
        
        has_good_context = any(word in context for word in good_context_words)
        
        has_formatting = (
            ' ' in phone or 
            '-' in phone or 
            '.' in phone or 
            phone.startswith('+') or 
            '(' in phone
        )
        
        if not has_good_context and not has_formatting:
            continue
        
        if digits_only.startswith('353'):
            local = digits_only[3:]
            if not local.startswith('0'):
                if 8 <= len(local) <= 9:
                    validated.append(phone)
                    seen.add(digits_only)
        
        elif digits_only.startswith('0'):
            if 10 <= len(digits_only) <= 11:
                validated.append(phone)
                seen.add(digits_only)
        
        elif digits_only.startswith('44'):
            local = digits_only[2:]
            if 10 <= len(local) <= 11:
                validated.append(phone)
                seen.add(digits_only)
        
        elif digits_only.startswith('61'):
            local = digits_only[2:]
            if 9 <= len(local) <= 10:
                validated.append(phone)
                seen.add(digits_only)
        
        elif digits_only.startswith('64'):
            local = digits_only[2:]
            if 8 <= len(local) <= 10:
                validated.append(phone)
                seen.add(digits_only)
        
        elif digits_only.startswith(('1300', '1800')):
            if len(digits_only) == 10:
                validated.append(phone)
                seen.add(digits_only)
        
        else:
            if has_good_context and has_formatting:
                if 10 <= len(digits_only) <= 15:
                    validated.append(phone)
                    seen.add(digits_only)
        
        if len(validated) >= 3:
            break
    
    return validated

def extract_social_media(text):
    """Extract social media links"""
    social = {
        'facebook': '',
        'linkedin': '',
        'instagram': '',
        'twitter': ''
    }
    
    patterns = {
        'facebook': r'(?:https?://)?(?:www\.)?facebook\.com/[\w\-\.]+/?',
        'linkedin': r'(?:https?://)?(?:www\.)?linkedin\.com/(?:company|in)/[\w\-]+/?',
        'instagram': r'(?:https?://)?(?:www\.)?instagram\.com/[\w\-\.]+/?',
        'twitter': r'(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/[\w\-]+/?'
    }
    
    for platform, pattern in patterns.items():
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            url = matches[0].rstrip('/')
            if not url.startswith('http'):
                url = 'https://' + url
            social[platform] = url
    
    return social

def similarity(a, b):
    """Calculate similarity between two strings"""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def try_request(url, headers, timeout=10):
    """Try HTTP request with retries"""
    for attempt in range(2):
        try:
            response = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            if response.status_code == 200:
                return response
            time.sleep(0.5)
        except:
            if attempt == 0:
                time.sleep(1)
            continue
    return None

def scrape_website(url):
    """Scrape website for contact info"""
    if not url or url == '':
        return None
    
    if not url.startswith('http'):
        url = 'https://' + url
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    all_emails = []
    all_phones = []
    all_content = ""
    social = {'facebook': '', 'linkedin': '', 'instagram': '', 'twitter': ''}
    
    try:
        domain = urlparse(url).netloc
        
        pages_to_try = [
            url,
            f"https://{domain}/contact",
            f"https://{domain}/contact-us",
            f"https://{domain}/contactus",
            f"https://{domain}/about",
            f"https://{domain}/about-us",
            f"https://{domain}/get-in-touch",
            f"https://{domain}/reach-us",
            f"https://{domain}/enquiry",
            f"https://{domain}/contact-form",
            f"https://{domain}/team",
            f"https://{domain}/support",
        ]
        
        for page_url in pages_to_try:
            response = try_request(page_url, headers, timeout=8)
            
            if response and response.status_code == 200:
                content = response.text
                all_content += " " + content
                
                page_emails = extract_emails_from_text(content)
                page_phones = extract_phones_from_text(content)
                
                all_emails.extend(page_emails)
                all_phones.extend(page_phones)
                
                if page_url == url:
                    social = extract_social_media(content)
                
                if page_emails:
                    break
            
            time.sleep(0.2)
        
        all_emails = list(dict.fromkeys(all_emails))
        all_phones = list(dict.fromkeys(all_phones))
        
        if all_emails or all_phones or social['facebook'] or social['linkedin']:
            return {
                'emails': all_emails,
                'phones': all_phones,
                'social': social,
                'content': all_content
            }
        
        return None
        
    except Exception as e:
        return None

def generate_email_patterns(domain):
    """Generate common email patterns"""
    if not domain:
        return []
    
    domain = domain.replace('http://', '').replace('https://', '').replace('www.', '').split('/')[0]
    
    patterns = [
        f"info@{domain}",
        f"contact@{domain}",
        f"hello@{domain}",
        f"admin@{domain}",
        f"mail@{domain}",
        f"office@{domain}",
        f"sales@{domain}",
        f"support@{domain}",
    ]
    
    return patterns

def verify_email_domain(email):
    """Check if email domain has valid MX records"""
    try:
        domain = email.split('@')[1]
        dns.resolver.resolve(domain, 'MX')
        return True
    except:
        return False

def verify_email_smtp(email):
    """Try SMTP verification"""
    try:
        domain = email.split('@')[1]
        mx_records = dns.resolver.resolve(domain, 'MX')
        mx_host = str(mx_records[0].exchange)
        
        server = smtplib.SMTP(timeout=5)
        server.set_debuglevel(0)
        server.connect(mx_host)
        server.helo(server.local_hostname)
        server.mail('verify@example.com')
        code, message = server.rcpt(email)
        server.quit()
        
        return code == 250
    except:
        return None

def find_website_via_search(company_name, company_id, country=''):
    """Search for company website using DuckDuckGo"""
    try:
        query = f"{company_name}"
        if company_id:
            query += f" {company_id}"
        if country:
            query += f" {country}"
        
        ddgs = DDGS()
        results = ddgs.text(query, max_results=5)
        
        if not results:
            return None
        
        for result in results:
            url = result.get('href', '')
            if url and url.startswith('http'):
                skip_domains = ['facebook.com', 'linkedin.com', 'twitter.com', 'instagram.com', 
                               'wikipedia.org', 'youtube.com', 'gov.', '.gov', 'register', 'companies']
                
                domain = urlparse(url).netloc.lower()
                if not any(skip in domain for skip in skip_domains):
                    return url
        
        return None
        
    except Exception as e:
        return None

def calculate_confidence(business, scraped_data, website):
    """Calculate confidence score"""
    score = 0
    reasons = []
    
    company_name = business.get('name', '')
    company_id = business.get('id', '')
    address = business.get('address', '')
    
    if not scraped_data:
        return 0, 'not_found'
    
    content = scraped_data.get('content', '').lower()
    
    if company_id and str(company_id) in content:
        score += 40
        reasons.append('company_id_match')
    
    if address:
        address_parts = [p.strip().lower() for p in address.split(',') if p.strip() and len(p.strip()) > 3]
        matches = sum(1 for part in address_parts if part in content)
        if matches >= 2:
            score += 30
            reasons.append('address_match')
        elif matches == 1:
            score += 15
            reasons.append('partial_address_match')
    
    if website:
        domain = urlparse(website if website.startswith('http') else f'https://{website}').netloc
        domain_name = domain.replace('www.', '').split('.')[0]
        
        name_clean = re.sub(r'[^a-z0-9]', '', company_name.lower())
        sim = similarity(name_clean, domain_name)
        
        if sim > 0.7:
            score += 20
            reasons.append('high_name_similarity')
        elif sim > 0.5:
            score += 10
            reasons.append('moderate_name_similarity')
    
    if website and company_name:
        name_words = [w for w in company_name.lower().split() if len(w) > 3]
        domain_lower = website.lower()
        if any(word in domain_lower for word in name_words):
            score += 10
            reasons.append('domain_name_match')
    
    return min(score, 100), ', '.join(reasons) if reasons else 'basic_match'

def enrich_single_business(row, config):
    """Enrich a single business"""
    
    with progress_lock:
        progress_counter['current'] += 1
        current = progress_counter['current']
        total = progress_counter['total']
        batch = progress_counter['batch']
    
    name = row.get(config['name_col'], 'Unknown')
    company_id = row.get(config['id_col'], '')
    
    address_parts = [row.get(col, '') for col in config['address_cols'] if row.get(col, '')]
    address = ', '.join(address_parts)
    
    country = ''
    folder_lower = config.get('folder', '').lower()
    if 'ireland' in folder_lower:
        country = 'Ireland'
    elif 'uk' in folder_lower:
        country = 'UK'
    elif 'australia' in folder_lower:
        country = 'Australia'
    elif 'zealand' in folder_lower:
        country = 'New Zealand'
    
    business = {
        'name': name,
        'id': company_id,
        'address': address
    }
    
    print(f"[Batch {batch}] [{current}/{total}] {name[:50]}")
    
    website = find_website_via_search(name, company_id, country)
    
    scraped_data = None
    emails = []
    phones = []
    social = {}
    
    if website:
        scraped_data = scrape_website(website)
        
        if scraped_data:
            emails = scraped_data.get('emails', [])
            phones = scraped_data.get('phones', [])
            social = scraped_data.get('social', {})
    
    if website and not emails:
        domain = urlparse(website if website.startswith('http') else f'https://{website}').netloc
        patterns = generate_email_patterns(domain)
        
        for pattern in patterns:
            if verify_email_domain(pattern):
                smtp_result = verify_email_smtp(pattern)
                if smtp_result or smtp_result is None:
                    emails.append(pattern)
                    break
    
    verified_emails = [e for e in emails if verify_email_domain(e)]
    
    if not scraped_data and not verified_emails:
        row.update({
            'email': '',
            'phone': '',
            'website': website if website else '',
            'facebook': '',
            'linkedin': '',
            'instagram': '',
            'twitter': '',
            'confidence_score': 0,
            'match_reasons': 'not_found'
        })
        return row, False
    
    confidence, reasons = calculate_confidence(business, scraped_data, website)
    
    row.update({
        'email': '; '.join(verified_emails[:3]) if verified_emails else '',
        'phone': '; '.join(phones[:3]) if phones else '',
        'website': website if website else '',
        'facebook': social.get('facebook', ''),
        'linkedin': social.get('linkedin', ''),
        'instagram': social.get('instagram', ''),
        'twitter': social.get('twitter', ''),
        'confidence_score': confidence,
        'match_reasons': reasons
    })
    
    success = bool(verified_emails or phones or website)
    
    if success:
        print(f"    ✅ {len(verified_emails)}e, {len(phones)}p, {confidence}%")
    
    return row, success

def load_archive_ids(archive_path, id_col):
    """Load company IDs from archive"""
    if not archive_path.exists():
        return set()
    
    ids = set()
    try:
        with open(archive_path, 'r', encoding='utf-8', errors='ignore') as f:
            reader = csv.DictReader(f)
            for row in reader:
                company_id = str(row.get(id_col, '')).strip()
                if company_id:
                    ids.add(company_id)
    except:
        pass
    
    return ids

def save_progress(output_path, all_results, fieldnames):
    """Save current progress"""
    def get_score(row):
        score = row.get('confidence_score', 0)
        try:
            return int(score) if score else 0
        except:
            return 0
    
    all_results_sorted = sorted(all_results, key=get_score, reverse=True)
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_results_sorted)

def enrich_country(country_name, config):
    """Enrich all businesses for a country in batches with resume"""
    print(f"\n{'='*80}")
    print(f"🌍 {country_name.upper()}")
    print(f"{'='*80}\n")
    
    month_code = get_month_code()
    output_filename = f"{country_name}_businesses_ENRICHED_{month_code}.csv"
    output_path = ENRICHED_DIR / output_filename
    
    country_folder = BASE_DIR / config['folder']
    last_30_path = country_folder / config['last_30']
    archive_path = country_folder / config['archive']
    
    if not last_30_path.exists():
        print(f"❌ Not found: {last_30_path}")
        return
    
    print("Checking for existing progress...")
    already_enriched_ids, existing_enriched_rows = load_already_enriched_ids(output_path, config['id_col'])
    
    if already_enriched_ids:
        print(f"Found {len(already_enriched_ids)} already enriched ✓")
    
    print("Checking archive...")
    archive_ids = load_archive_ids(archive_path, config['id_col'])
    print(f"Archive: {len(archive_ids)} companies\n")
    
    with open(last_30_path, 'r', encoding='utf-8', errors='ignore') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        original_fieldnames = reader.fieldnames
    
    new_rows = []
    for row in rows:
        company_id = str(row.get(config['id_col'], '')).strip()
        if company_id and company_id not in archive_ids and company_id not in already_enriched_ids:
            new_rows.append(row)
    
    print(f"Total: {len(rows)}")
    print(f"Already enriched: {len(already_enriched_ids)}")
    print(f"In archive: {len([r for r in rows if str(r.get(config['id_col'], '')).strip() in archive_ids])}")
    print(f"Remaining to enrich: {len(new_rows)}\n")
    
    if not new_rows:
        print("✅ Nothing new to enrich\n")
        return
    
    enriched_fieldnames = list(original_fieldnames) + [
        'email', 'phone', 'website',
        'facebook', 'linkedin', 'instagram', 'twitter',
        'confidence_score', 'match_reasons'
    ]
    
    progress_counter['current'] = 0
    progress_counter['total'] = len(new_rows)
    progress_counter['batch'] = 0
    
    all_results = existing_enriched_rows.copy()
    enriched_count = len(already_enriched_ids)
    
    num_batches = (len(new_rows) + BATCH_SIZE - 1) // BATCH_SIZE
    
    for batch_num in range(num_batches):
        start_idx = batch_num * BATCH_SIZE
        end_idx = min(start_idx + BATCH_SIZE, len(new_rows))
        batch_rows = new_rows[start_idx:end_idx]
        
        progress_counter['batch'] = batch_num + 1
        
        print(f"\n{'─'*80}")
        print(f"📦 BATCH {batch_num + 1}/{num_batches} ({len(batch_rows)} businesses)")
        print(f"{'─'*80}\n")
        
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(enrich_single_business, row, config): row
                for row in batch_rows
            }
            
            for future in as_completed(futures):
                try:
                    row, success = future.result()
                    all_results.append(row)
                    if success:
                        enriched_count += 1
                except Exception as e:
                    print(f"    Error: {e}")
        
        save_progress(output_path, all_results, enriched_fieldnames)
        total_processed = len(already_enriched_ids) + len(all_results) - len(existing_enriched_rows)
        print(f"\n💾 Progress saved ({total_processed}/{len(rows)} total processed)")
        
        if batch_num < num_batches - 1:
            print(f"⏸️  Waiting {BATCH_DELAY} seconds before next batch...")
            time.sleep(BATCH_DELAY)
    
    total_rows = len(rows)
    success_rate = (enriched_count / total_rows * 100) if total_rows > 0 else 0
    
    print(f"\n✅ Total enriched: {enriched_count}/{total_rows} ({success_rate:.1f}%)")
    print(f"💾 Saved: {output_filename}\n")
    
    enrichment_results.append({
        'country': country_name,
        'total': total_rows,
        'enriched': enriched_count,
        'rate': success_rate
    })

def print_summary():
    """Print final summary"""
    print("\n" + "="*80)
    print("📊 SUMMARY")
    print("="*80)
    
    if not enrichment_results:
        print("\nNo new countries enriched this session")
        return
    
    total_businesses = sum(r['total'] for r in enrichment_results)
    total_enriched = sum(r['enriched'] for r in enrichment_results)
    overall_rate = (total_enriched / total_businesses * 100) if total_businesses > 0 else 0
    
    print(f"\n{'Country':<20} {'Total':<12} {'Enriched':<12} {'Success':<12}")
    print("-" * 80)
    
    for result in enrichment_results:
        print(f"{result['country'].title():<20} {result['total']:<12} {result['enriched']:<12} {result['rate']:.1f}%")
    
    print("-" * 80)
    print(f"{'TOTAL':<20} {total_businesses:<12} {total_enriched:<12} {overall_rate:.1f}%")
    print("="*80 + "\n")

def main():
    print("="*80)
    print("🔧 LEAD ENRICHER - FINAL FIXED VERSION")
    print("="*80)
    print(f"\n⚡ {MAX_WORKERS} threads per batch")
    print(f"📦 Batch size: {BATCH_SIZE} businesses")
    print(f"⏸️  Delay: {BATCH_DELAY}s between batches")
    print(f"♻️  Resume: Continues from where stopped")
    print(f"📞 Fixed: Strict phone validation")
    print(f"📅 {get_month_code()}")
    print(f"📂 {ENRICHED_DIR}\n")
    
    print("Counting remaining leads per country...")
    country_sizes = []
    for country_name, config in COUNTRIES.items():
        count = count_new_leads(country_name, config)
        if count > 0:
            country_sizes.append((country_name, config, count))
            print(f"  {country_name.title()}: {count:,} remaining")
    
    if not country_sizes:
        print("\n✅ All countries fully enriched!")
        return
    
    country_sizes.sort(key=lambda x: x[2])
    
    print(f"\nProcessing order (smallest → largest):")
    for i, (country_name, _, count) in enumerate(country_sizes, 1):
        print(f"  {i}. {country_name.title()}: {count:,} remaining")
    
    print()
    input("Press Enter to start/resume...")
    print("="*80)
    
    start_time = time.time()
    
    for country_name, config, _ in country_sizes:
        try:
            enrich_country(country_name, config)
            print(f"\n⏸️  10s break before next country...\n")
            time.sleep(10)
        except KeyboardInterrupt:
            print("\n\n⚠️  Stopped by user")
            print("Progress saved. Run again to resume.\n")
            break
        except Exception as e:
            print(f"\n❌ Error: {e}\n")
            continue
    
    elapsed = time.time() - start_time
    
    print_summary()
    
    print(f"⏱️  Time: {elapsed/60:.1f} min")
    print(f"📂 {ENRICHED_DIR}\n")

if __name__ == "__main__":
    main()