"""
UK Business Registration Scraper - FULLY AUTOMATIC
Auto-finds and downloads latest monthly snapshot
"""

import csv
import zipfile
import requests
from pathlib import Path
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# Configuration
BASE_DIR = Path("D:/Desktop/Business Registrations/2 - UK Data")
BASE_DIR.mkdir(parents=True, exist_ok=True)

DOWNLOADS_DIR = BASE_DIR / "Downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

COMPANIES_HOUSE_PAGE = "https://download.companieshouse.gov.uk/en_output.html"
BATCH_SIZE = 100_000

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
        
        # Find the BasicCompanyDataAsOneFile link
        for link in links:
            href = link['href']
            if 'BasicCompanyDataAsOneFile' in href and href.endswith('.zip'):
                # Fix URL if needed
                if not href.startswith('http'):
                    # Remove leading slash if present
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
    
    # Check if already downloaded
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
        chunk_size = 1024 * 1024  # 1MB chunks
        
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
        
        print(f"\n✅ Download complete: {filename}")
        return output_path
        
    except Exception as e:
        print(f"\n❌ Download failed: {e}")
        if output_path.exists():
            output_path.unlink()
        return None

def parse_uk_date(date_str):
    """Convert UK date DD/MM/YYYY to datetime"""
    if not date_str or date_str.strip() == '':
        return None
    
    try:
        return datetime.strptime(date_str.strip(), '%d/%m/%Y')
    except:
        return None

def load_archive_ids():
    """Load company numbers from archive"""
    archive_path = BASE_DIR / "uk_businesses_ARCHIVE.csv"
    
    if not archive_path.exists():
        return set()
    
    archive_ids = set()
    
    try:
        print("📋 Loading archive IDs...")
        with open(archive_path, 'r', encoding='utf-8', errors='ignore') as f:
            reader = csv.DictReader(f)
            for row in reader:
                company_num = row.get(' CompanyNumber', '').strip()
                if company_num:
                    archive_ids.add(company_num)
        
        print(f"   ✅ Loaded {len(archive_ids):,} archived company IDs")
    except Exception as e:
        print(f"   ⚠️  Error loading archive: {e}")
    
    return archive_ids

def archive_previous_last_30():
    """Move previous LAST_30_DAYS to ARCHIVE"""
    last_30_path = BASE_DIR / "uk_businesses_LAST_30_DAYS.csv"
    archive_path = BASE_DIR / "uk_businesses_ARCHIVE.csv"
    
    if not last_30_path.exists():
        print("📋 No previous LAST_30_DAYS to archive")
        return
    
    print("📋 Archiving previous LAST_30_DAYS...")
    
    try:
        with open(last_30_path, 'r', encoding='utf-8', errors='ignore') as f:
            reader = csv.DictReader(f)
            previous_rows = list(reader)
            fieldnames = reader.fieldnames
        
        if not previous_rows:
            print("   ⚠️  Previous LAST_30_DAYS is empty, skipping")
            return
        
        if archive_path.exists():
            with open(archive_path, 'a', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writerows(previous_rows)
            print(f"   ✅ Appended {len(previous_rows):,} rows to archive")
        else:
            with open(archive_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(previous_rows)
            print(f"   ✅ Created archive with {len(previous_rows):,} rows")
        
    except Exception as e:
        print(f"   ❌ Error archiving: {e}")

def process_zip_file(zip_path):
    """Extract and filter companies from ZIP"""
    print(f"\n{'='*80}")
    print(f"📦 PROCESSING UK DATA")
    print(f"{'='*80}\n")
    
    cutoff_date = datetime.now() - timedelta(days=30)
    print(f"📅 Cutoff date: {cutoff_date.strftime('%d/%m/%Y')}")
    print(f"   (Companies incorporated after this date)\n")
    
    archive_previous_last_30()
    print()
    archive_ids = load_archive_ids()
    
    print(f"\n📂 Opening ZIP: {zip_path.name}...")
    
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            csv_files = [f for f in zip_ref.namelist() if f.endswith('.csv')]
            
            if not csv_files:
                print("❌ No CSV found in ZIP")
                return 0
            
            csv_filename = csv_files[0]
            print(f"   CSV file: {csv_filename}")
            
            new_companies = []
            all_companies = []
            total_processed = 0
            batch_num = 0
            
            print("\n📊 Processing companies in batches...\n")
            
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
                            all_companies.append(company)
                            
                            inc_date_str = company.get('IncorporationDate', '').strip()
                            inc_date = parse_uk_date(inc_date_str)
                            
                            if inc_date and inc_date >= cutoff_date:
                                company_num = company.get(' CompanyNumber', '').strip()
                                
                                if company_num and company_num not in archive_ids:
                                    new_companies.append(company)
                        
                        print(f"📦 Batch {batch_num}: {total_processed:,} processed | {len(new_companies):,} new found")
                        batch = []
                
                if batch:
                    batch_num += 1
                    
                    for company in batch:
                        all_companies.append(company)
                        
                        inc_date_str = company.get('IncorporationDate', '').strip()
                        inc_date = parse_uk_date(inc_date_str)
                        
                        if inc_date and inc_date >= cutoff_date:
                            company_num = company.get(' CompanyNumber', '').strip()
                            
                            if company_num and company_num not in archive_ids:
                                new_companies.append(company)
                    
                    print(f"📦 Final batch {batch_num}: {total_processed:,} processed | {len(new_companies):,} new found")
            
            print(f"\n{'─'*80}")
            print(f"✅ Total companies processed: {total_processed:,}")
            print(f"✅ NEW companies (last 30 days, not in archive): {len(new_companies):,}")
            print(f"{'─'*80}\n")
            
            print(f"💾 Saving FULL database...")
            full_path = BASE_DIR / "uk_businesses_FULL.csv"
            with open(full_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(all_companies)
            print(f"   ✅ Saved {len(all_companies):,} companies")
            
            print(f"💾 Saving LAST_30_DAYS (NEW only)...")
            last_30_path = BASE_DIR / "uk_businesses_LAST_30_DAYS.csv"
            with open(last_30_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(new_companies)
            print(f"   ✅ Saved {len(new_companies):,} NEW companies")
            
            return len(new_companies)
            
    except Exception as e:
        print(f"❌ Error processing ZIP: {e}")
        import traceback
        traceback.print_exc()
        return 0

def main():
    print("="*80)
    print("🇬🇧 UK BUSINESS REGISTRATION SCRAPER - AUTOMATIC")
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
    print(f"NEW companies (last 30 days, deduped): {count:,}")
    print(f"\nFiles created:")
    print(f"  - FULL: uk_businesses_FULL.csv (all companies)")
    print(f"  - LAST_30_DAYS: uk_businesses_LAST_30_DAYS.csv (NEW only)")
    print(f"  - ARCHIVE: uk_businesses_ARCHIVE.csv (previous months)")
    print("="*80 + "\n")

if __name__ == "__main__":
    main()