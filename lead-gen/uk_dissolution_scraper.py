"""
UK Company Dissolution Scraper - FULLY AUTOMATIC
Auto-finds and downloads latest monthly snapshot
Filters by status (no dissolution date available)
Month-on-month tracking to find NEW dissolutions
"""

import csv
import zipfile
import requests
from pathlib import Path
from datetime import datetime
from bs4 import BeautifulSoup

# Configuration
BASE_DIR = Path("D:/Desktop/Company Dissolutions/2 - UK Data")
BASE_DIR.mkdir(parents=True, exist_ok=True)

DOWNLOADS_DIR = BASE_DIR / "Downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

COMPANIES_HOUSE_PAGE = "https://download.companieshouse.gov.uk/en_output.html"
BATCH_SIZE = 100_000

# Dissolution statuses
DISSOLUTION_STATUSES = [
    'Dissolved',
    'Liquidation',
    'In Administration',
    'Receiver Manager',
    'Receivership',
    'Converted / Closed',
    'Voluntary Arrangement'
]

def get_latest_download_url():
    """Auto-find the latest download URL"""
    print("🔍 Finding latest UK data file...")
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    try:
        response = requests.get(COMPANIES_HOUSE_PAGE, headers=headers, timeout=30)
        
        if response.status_code != 200:
            print(f"❌ Failed to load page: {response.status_code}")
            return None
        
        soup = BeautifulSoup(response.text, 'html.parser')
        links = soup.find_all('a', href=True)
        
        for link in links:
            href = link['href']
            if 'BasicCompanyDataAsOneFile' in href and href.endswith('.zip'):
                if not href.startswith('http'):
                    href = href.lstrip('/')
                    href = f"https://download.companieshouse.gov.uk/{href}"
                
                filename = href.split('/')[-1]
                print(f"✅ Found: {filename}")
                print(f"   URL: {href}\n")
                return href
        
        print("❌ Could not find download link")
        return None
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return None

def download_file(url):
    """Download the ZIP file"""
    filename = url.split('/')[-1]
    output_path = DOWNLOADS_DIR / filename
    
    if output_path.exists():
        size_mb = output_path.stat().st_size / (1024 * 1024)
        print(f"✅ Already downloaded: {filename} ({size_mb:.0f}MB)")
        return output_path
    
    print(f"📥 Downloading: {filename}")
    print(f"   This will take 5-10 minutes...\n")
    
    try:
        response = requests.get(url, stream=True, timeout=600)
        
        if response.status_code != 200:
            print(f"❌ Download failed: HTTP {response.status_code}")
            return None
        
        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0
        chunk_size = 1024 * 1024
        
        with open(output_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    
                    if total_size > 0:
                        percent = (downloaded / total_size) * 100
                        mb_downloaded = downloaded / (1024 * 1024)
                        mb_total = total_size / (1024 * 1024)
                        print(f"\r   Progress: {percent:.1f}% ({mb_downloaded:.0f}MB / {mb_total:.0f}MB)", end='', flush=True)
        
        print(f"\n✅ Download complete")
        return output_path
        
    except Exception as e:
        print(f"\n❌ Download failed: {e}")
        if output_path.exists():
            output_path.unlink()
        return None

def is_dissolved(status):
    """Check if company status indicates dissolution"""
    if not status:
        return False
    
    status_lower = status.lower()
    return any(dissolved_status.lower() in status_lower for dissolved_status in DISSOLUTION_STATUSES)

def load_full_ids():
    """Load company IDs from previous FULL file"""
    full_path = BASE_DIR / "uk_dissolutions_FULL.csv"
    
    if not full_path.exists():
        return set()
    
    full_ids = set()
    
    try:
        print("📋 Loading previous FULL IDs...")
        with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
            reader = csv.DictReader(f)
            for row in reader:
                company_num = row.get(' CompanyNumber', '').strip()
                if company_num:
                    full_ids.add(company_num)
        
        print(f"   ✅ Loaded {len(full_ids):,} IDs from previous month")
    except Exception as e:
        print(f"   ⚠️  Error loading FULL: {e}")
    
    return full_ids

def archive_previous_full():
    """Archive previous FULL file"""
    full_path = BASE_DIR / "uk_dissolutions_FULL.csv"
    archive_path = BASE_DIR / "uk_dissolutions_ARCHIVE.csv"
    
    if not full_path.exists():
        print("📋 No previous FULL to archive (first run)")
        return
    
    print("📋 Archiving previous FULL...")
    
    try:
        import shutil
        shutil.copy(full_path, archive_path)
        print(f"   ✅ Archived to uk_dissolutions_ARCHIVE.csv")
    except Exception as e:
        print(f"   ❌ Error archiving: {e}")

def process_zip_file(zip_path):
    """Extract and filter dissolutions from ZIP"""
    print(f"\n{'='*80}")
    print(f"📦 PROCESSING UK DISSOLUTIONS")
    print(f"{'='*80}\n")
    
    # Archive previous FULL
    archive_previous_full()
    
    # Load previous month's dissolved IDs
    previous_dissolved_ids = load_full_ids()
    
    print(f"\n📂 Opening ZIP: {zip_path.name}...")
    
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            csv_files = [f for f in zip_ref.namelist() if f.endswith('.csv')]
            
            if not csv_files:
                print("❌ No CSV found in ZIP")
                return 0
            
            csv_filename = csv_files[0]
            print(f"   CSV file: {csv_filename}")
            
            all_dissolved = []
            new_dissolved = []
            total_processed = 0
            batch_num = 0
            
            print("\n📊 Processing in batches...\n")
            
            with zip_ref.open(csv_filename, 'r') as csv_file:
                import io
                text_wrapper = io.TextIOWrapper(csv_file, encoding='utf-8', errors='ignore')
                reader = csv.DictReader(text_wrapper)
                fieldnames = reader.fieldnames
                
                batch = []
                
                for row in reader:
                    batch.append(row)
                    total_processed += 1
                    
                    if len(batch) >= BATCH_SIZE:
                        batch_num += 1
                        
                        for company in batch:
                            status = company.get('CompanyStatus', '')
                            
                            if is_dissolved(status):
                                all_dissolved.append(company)
                                
                                # Check if NEW (not in previous month)
                                company_num = company.get(' CompanyNumber', '').strip()
                                if company_num and company_num not in previous_dissolved_ids:
                                    new_dissolved.append(company)
                        
                        print(f"📦 Batch {batch_num}: {total_processed:,} processed | {len(all_dissolved):,} dissolved | {len(new_dissolved):,} NEW")
                        batch = []
                
                if batch:
                    batch_num += 1
                    
                    for company in batch:
                        status = company.get('CompanyStatus', '')
                        
                        if is_dissolved(status):
                            all_dissolved.append(company)
                            
                            company_num = company.get(' CompanyNumber', '').strip()
                            if company_num and company_num not in previous_dissolved_ids:
                                new_dissolved.append(company)
                    
                    print(f"📦 Final batch {batch_num}: {total_processed:,} processed | {len(all_dissolved):,} dissolved | {len(new_dissolved):,} NEW")
            
            print(f"\n{'─'*80}")
            print(f"✅ Total companies processed: {total_processed:,}")
            print(f"✅ Total dissolved found: {len(all_dissolved):,}")
            print(f"✅ NEW dissolutions (vs last month): {len(new_dissolved):,}")
            print(f"{'─'*80}\n")
            
            # Save FULL (all dissolved)
            print(f"💾 Saving FULL (all dissolved)...")
            full_path = BASE_DIR / "uk_dissolutions_FULL.csv"
            with open(full_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(all_dissolved)
            print(f"   ✅ Saved {len(all_dissolved):,} dissolved companies")
            
            # Save LAST_30_DAYS (NEW only)
            print(f"💾 Saving LAST_30_DAYS (NEW dissolutions)...")
            last_30_path = BASE_DIR / "uk_dissolutions_LAST_30_DAYS.csv"
            with open(last_30_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(new_dissolved)
            print(f"   ✅ Saved {len(new_dissolved):,} NEW dissolutions")
            
            return len(new_dissolved)
            
    except Exception as e:
        print(f"❌ Error processing ZIP: {e}")
        import traceback
        traceback.print_exc()
        return 0

def main():
    print("="*80)
    print("🇬🇧 UK DISSOLUTION SCRAPER - AUTOMATIC")
    print("="*80)
    print()
    
    # Auto-find download URL
    download_url = get_latest_download_url()
    
    if not download_url:
        print("\n❌ Could not find download URL")
        return
    
    # Download file
    zip_path = download_file(download_url)
    
    if not zip_path:
        print("\n❌ Download failed")
        return
    
    # Process file
    count = process_zip_file(zip_path)
    
    # Summary
    print("\n" + "="*80)
    print("📊 FINAL SUMMARY")
    print("="*80)
    print(f"NEW dissolutions (vs last month): {count:,}")
    print(f"\nNote: UK doesn't have dissolution dates")
    print(f"NEW = Companies that are dissolved THIS month but weren't last month")
    print(f"\nFiles created:")
    print(f"  - FULL: uk_dissolutions_FULL.csv (all dissolved)")
    print(f"  - LAST_30_DAYS: uk_dissolutions_LAST_30_DAYS.csv (NEW only)")
    print(f"  - ARCHIVE: uk_dissolutions_ARCHIVE.csv (previous month)")
    print("="*80 + "\n")

if __name__ == "__main__":
    main()