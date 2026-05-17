#!/usr/bin/env python3
"""
Care Provider Lead Generation Pipeline v3
==========================================
Press play -> get leads.

Output structure:
    output/YYYYMMDD/
        UK/
            UK_CQC_POORLY_RATED.csv
            UK_CQC_NEW_REGISTRATIONS.csv
            UK_CQC_DOWNGRADES.csv (from 2nd run onward)
        Ireland/
            IRL_HIQA_ALL_CENTRES.csv
            IRL_HIQA_EXPIRING_90_DAYS.csv
            IRL_HIQA_NEW_REGISTRATIONS.csv (from 2nd run onward)

REQUIREMENTS:
    pip install pandas requests odfpy
    LibreOffice installed (for fast ODS conversion)
"""

import os, sys, csv, json, re, shutil, subprocess, zipfile
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET
import pandas as pd
import requests

# ============================================================
# CONFIG
# ============================================================
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
SNAPSHOTS_DIR = BASE_DIR / "snapshots"
OUTPUT_DIR = BASE_DIR / "output"

for d in [DATA_DIR/"uk", DATA_DIR/"ireland", SNAPSHOTS_DIR, OUTPUT_DIR]:
    d.mkdir(parents=True, exist_ok=True)

HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
CQC_PAGE = "https://www.cqc.org.uk/about-us/transparency/using-cqc-data"
HIQA_OLDER = "https://www.hiqa.ie/centre/export/older_persons_register.csv?_format=csv"
HIQA_DISABILITY = "https://www.hiqa.ie/centre/export/disability_register.csv?_format=csv"

BAD_RATINGS = {"Inadequate", "Requires improvement", "Requires Improvement"}
RATING_SCORE = {"Inadequate":1, "Requires improvement":2, "Requires Improvement":2, "Good":3, "Outstanding":4}


# ============================================================
# HELPERS
# ============================================================
def download(url, dest, label=""):
    if dest.exists() and dest.stat().st_size > 1000:
        print(f"  Already have: {dest.name} ({dest.stat().st_size:,} bytes)")
        return True
    print(f"  Downloading {label or dest.name}...")
    try:
        r = requests.get(url, headers=HDR, timeout=600, stream=True)
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        got = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk); got += len(chunk)
                if total: print(f"\r  {got:,}/{total:,} ({got*100//total}%)", end="", flush=True)
        print(f"\n  Saved: {dest.name} ({dest.stat().st_size:,} bytes)")
        return True
    except Exception as e:
        print(f"\n  Download failed: {e}")
        return False

def find_col(df, candidates):
    for c in candidates:
        if c in df.columns: return c
    for c in candidates:
        for col in df.columns:
            if c.lower() in col.lower(): return col
    return None

def safe(row, col):
    if not col: return ""
    v = row.get(col, "")
    return "" if pd.isna(v) else str(v).strip()

def latest_file(directory, prefix, ext="*"):
    files = sorted(Path(directory).glob(f"{prefix}*.{ext}"), key=lambda x: x.stat().st_mtime, reverse=True)
    return files[0] if files else None


# ============================================================
# STEP 1: DOWNLOAD EVERYTHING
# ============================================================
def download_all():
    print("\n" + "="*60)
    print("STEP 1: DOWNLOADING DATA")
    print("="*60)
    today = datetime.now().strftime("%Y%m%d")

    # --- CQC ---
    print("\n--- UK (CQC) ---")
    # Find URLs from CQC page
    ratings_url = csv_url = filters_url = None
    try:
        r = requests.get(CQC_PAGE, headers=HDR, timeout=30); r.raise_for_status()
        ratings_url = (re.findall(r'(https://www\.cqc\.org\.uk/sites/default/files/[^"]+Latest_ratings\.ods)', r.text) or [None])[0]
        csv_url = (re.findall(r'(https://www\.cqc\.org\.uk/sites/default/files/[^"]+CQC_directory\.csv)', r.text) or [None])[0]
        filters_url = (re.findall(r'(https://www\.cqc\.org\.uk/sites/default/files/[^"]+HSCA_Active_Locations\.ods)', r.text) or [None])[0]
    except: pass

    ratings_url = ratings_url or "https://www.cqc.org.uk/sites/default/files/2026-02/01_February_2026_Latest_ratings.ods"
    filters_url = filters_url or "https://www.cqc.org.uk/sites/default/files/2026-02/01_February_2026_HSCA_Active_Locations.ods"
    download(ratings_url, DATA_DIR/"uk"/f"cqc_ratings_{today}.ods", "CQC Ratings ODS (~24MB)")
    download(filters_url, DATA_DIR/"uk"/f"cqc_filters_{today}.ods", "CQC Directory with Filters ODS (~25MB) [has beds & website]")

    if csv_url:
        download(csv_url, DATA_DIR/"uk"/f"cqc_directory_{today}.csv", "CQC Directory CSV")
    
    # Jan CSV for diff
    jan = DATA_DIR/"uk"/"cqc_directory_20260121.csv"
    if not jan.exists():
        download("https://www.cqc.org.uk/sites/default/files/2026-01/21_January_2026_CQC_directory.csv", jan, "CQC Jan CSV")

    # --- HIQA ---
    print("\n--- Ireland (HIQA) ---")
    download(HIQA_OLDER, DATA_DIR/"ireland"/f"hiqa_older_{today}.csv", "HIQA Older Persons")
    download(HIQA_DISABILITY, DATA_DIR/"ireland"/f"hiqa_disability_{today}.csv", "HIQA Disability")


# ============================================================
# STEP 2A: UK RATINGS
# ============================================================
def list_ods_sheets(path):
    try:
        with zipfile.ZipFile(path) as z:
            with z.open("content.xml") as f:
                tree = ET.parse(f)
        ns = {"table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0"}
        return [{"name": t.get(f"{{{ns['table']}}}name"), "rows": len(t.findall("table:table-row", ns))}
                for t in tree.findall(".//table:table", ns)]
    except: return []

def ods_to_csv(ods_path):
    sheets = list_ods_sheets(ods_path)
    if not sheets: return None
    
    print(f"  Sheets: {[(s['name'], s['rows']) for s in sheets]}")
    
    # Find data sheet (biggest, or named 'Locations')
    best_idx = 0
    for i, s in enumerate(sheets):
        if any(k in s["name"].lower() for k in ["location", "hsca", "active"]):
            best_idx = i; break
    else:
        best_idx = max(range(len(sheets)), key=lambda i: sheets[i]["rows"])
    
    print(f"  Using sheet [{best_idx}]: '{sheets[best_idx]['name']}'")
    csv_path = ods_path.with_name(ods_path.stem + f"_s{best_idx}.csv")
    
    if csv_path.exists() and csv_path.stat().st_size > 10000:
        print(f"  CSV exists: {csv_path.name} ({csv_path.stat().st_size:,} bytes)")
        return csv_path

    # Try LibreOffice
    lo = None
    for cmd in ["soffice", "libreoffice", "C:\\Program Files\\LibreOffice\\program\\soffice.exe",
                 "C:\\Program Files (x86)\\LibreOffice\\program\\soffice.exe"]:
        if shutil.which(cmd) or os.path.exists(cmd): lo = cmd; break
    
    if lo:
        print(f"  Converting with LibreOffice...")
        try:
            filt = f'csv:"Text - txt - csv (StarCalc)":44,34,UTF8,1,,0,false,true,false,false,false,{best_idx}'
            subprocess.run([lo, "--headless", "--convert-to", filt, "--outdir", str(ods_path.parent), str(ods_path)],
                          capture_output=True, text=True, timeout=300)
            lo_out = ods_path.with_suffix(".csv")
            if lo_out.exists() and lo_out.stat().st_size > 10000:
                shutil.copy2(lo_out, csv_path); os.remove(lo_out)
                print(f"  Converted: {csv_path.name} ({csv_path.stat().st_size:,} bytes)")
                return csv_path
            if lo_out.exists(): os.remove(lo_out)
            
            # Basic conversion fallback
            subprocess.run([lo, "--headless", "--convert-to", "csv", "--outdir", str(ods_path.parent), str(ods_path)],
                          capture_output=True, text=True, timeout=300)
            if lo_out.exists() and lo_out.stat().st_size > 10000:
                shutil.copy2(lo_out, csv_path); os.remove(lo_out)
                print(f"  Converted (basic): {csv_path.name}")
                return csv_path
            if lo_out.exists(): os.remove(lo_out)
        except Exception as e:
            print(f"  LibreOffice error: {e}")

    # Pandas fallback (SLOW)
    print(f"  Using pandas (15-20 min)... go grab a coffee")
    import time; start = time.time()
    df = pd.read_excel(ods_path, engine="odf", sheet_name=sheets[best_idx]["name"])
    print(f"  Read in {time.time()-start:.0f}s ({len(df):,} rows)")
    df.to_csv(csv_path, index=False)
    return csv_path


def process_uk():
    print("\n" + "="*60)
    print("STEP 2A: UK RATINGS")
    print("="*60)

    ods = latest_file(DATA_DIR/"uk", "cqc_ratings_", "ods")
    if not ods: print("  No ODS file!"); return None

    csv_file = ods_to_csv(ods)
    if not csv_file: return None

    print(f"\n  Reading {csv_file.name}...")
    df = pd.read_csv(csv_file, low_memory=False, on_bad_lines="skip")
    df.columns = df.columns.str.strip()

    # Skip metadata rows
    for i in range(min(20, len(df))):
        vals = [str(v).strip().lower() for v in df.iloc[i].values if pd.notna(v)]
        if any("location" in v and ("name" in v or "id" in v) for v in vals):
            df.columns = [str(h).strip() for h in df.iloc[i].values]
            df = df.iloc[i+1:].reset_index(drop=True)
            break

    print(f"  Rows: {len(df):,}")

    # Columns
    rc = find_col(df, ["Latest Rating", "Location Latest Overall Rating", "Overall Rating"])
    ic = find_col(df, ["Location ID", "location_id"])
    nc = find_col(df, ["Location Name", "location_name"])
    pc = find_col(df, ["Provider Name", "provider_name"])
    ph = find_col(df, ["Location Telephone Number", "location_telephone_number"])
    wc = find_col(df, ["Location Web Address", "location_web_address"])
    tc = find_col(df, ["Location Type", "Location Type/Sector"])
    ipc = find_col(df, ["Location Primary Inspection Category"])
    rgc = find_col(df, ["Location Region", "location_region"])
    lac = find_col(df, ["Location Local Authority"])
    ac = find_col(df, ["Location Street Address"])
    cc = find_col(df, ["Location City"])
    poc = find_col(df, ["Location Post Code", "Location Postal Code"])
    bc = find_col(df, ["Care homes beds", "care_homes_beds"])
    chc = find_col(df, ["Care Home?", "Care Home"])

    if not rc:
        print(f"  COULD NOT FIND RATING COLUMN! Columns: {list(df.columns)}")
        return None

    print(f"  Rating col: '{rc}'")
    print(f"\n  Rating breakdown:")
    for r, cnt in df[rc].value_counts().items():
        print(f"    {r}: {cnt:,}{' <-- LEADS' if r in BAD_RATINGS else ''}")

    care_df = df[df[chc].astype(str).str.upper()=="Y"] if chc else df
    poor = care_df[care_df[rc].isin(BAD_RATINGS)]
    
    # Deduplicate — ODS has multiple rows per location (one per inspection category)
    if ic:
        before = len(poor)
        poor = poor.drop_duplicates(subset=[ic], keep="first")
        print(f"\n  Care homes: {len(care_df):,}")
        print(f"  Poorly rated (raw): {before:,}")
        print(f"  Poorly rated (deduplicated): {len(poor):,}")
        if before != len(poor):
            print(f"  Removed {before - len(poor):,} duplicates")
    else:
        print(f"\n  Care homes: {len(care_df):,}")
        print(f"  Poorly rated: {len(poor):,}")

    # Snapshot
    today = datetime.now().strftime("%Y%m%d")
    snap = {}
    if ic:
        for _, row in df.iterrows():
            lid = str(row.get(ic,"")).strip(); rat = str(row.get(rc,"")).strip()
            if lid and rat and lid!="nan" and rat!="nan": snap[lid] = rat
        with open(SNAPSHOTS_DIR/f"uk_ratings_{today}.json","w") as f:
            json.dump({"date":today,"ratings":snap},f)

    # Downgrades
    downgrades = []
    prev = sorted([f for f in SNAPSHOTS_DIR.glob("uk_ratings_*.json") if f.name!=f"uk_ratings_{today}.json"], reverse=True)
    if prev and ic:
        with open(prev[0]) as f: pr = json.load(f).get("ratings",{})
        for lid, cur in snap.items():
            p = pr.get(lid)
            if p and p!=cur and 0 < RATING_SCORE.get(cur,0) < RATING_SCORE.get(p,0):
                rows = df[df[ic].astype(str).str.strip()==lid]
                if len(rows):
                    rec = rows.iloc[0].to_dict(); rec["_prev"]=p; rec["_curr"]=cur
                    downgrades.append(rec)
        print(f"  NEW downgrades: {len(downgrades)}")
    else:
        print(f"  First run. Downgrades from next month.")

    # New registrations (CSV diff)
    new_regs = pd.DataFrame()
    csvs = sorted((DATA_DIR/"uk").glob("cqc_directory_*.csv"), key=lambda x: x.stat().st_mtime, reverse=True)
    if len(csvs) >= 2:
        try:
            dn = pd.read_csv(csvs[0], low_memory=False, on_bad_lines="skip")
            do = pd.read_csv(csvs[1], low_memory=False, on_bad_lines="skip")
            cid = find_col(dn, ["CQC Location ID (for office use only)","Location ID"])
            if cid:
                added = set(dn[cid].dropna().astype(str)) - set(do[cid].dropna().astype(str))
                if added: new_regs = dn[dn[cid].astype(str).isin(added)]
        except: pass

    cols = {"rc":rc,"ic":ic,"nc":nc,"pc":pc,"ph":ph,"wc":wc,"tc":tc,"ipc":ipc,
            "rgc":rgc,"lac":lac,"ac":ac,"cc":cc,"poc":poc,"bc":bc}
    return {"poor":poor, "downgrades":downgrades, "new_regs":new_regs, "cols":cols, "df":df}


# ============================================================
# STEP 2B: IRELAND
# ============================================================
def process_ireland():
    print("\n" + "="*60)
    print("STEP 2B: IRELAND (HIQA)")
    print("="*60)

    today = datetime.now().strftime("%Y%m%d")
    now = datetime.now()
    all_centres = []
    expiring = []
    new_regs_all = []

    for prefix, label in [("hiqa_older","Older Persons"), ("hiqa_disability","Disability")]:
        f = latest_file(DATA_DIR/"ireland", prefix, "csv")
        if not f: print(f"  No {label} file"); continue

        df = pd.read_csv(f, on_bad_lines="skip")
        print(f"\n  {label}: {len(df):,} centres")
        print(f"  Columns: {df.columns.tolist()}")

        # Add source column
        df["Source"] = label

        # All centres
        all_centres.append(df)

        # Expiring within 90 days
        exp_col = "Registration_Expiry_Date"
        if exp_col in df.columns:
            threshold = now + timedelta(days=90)
            for _, row in df.iterrows():
                try:
                    exp_str = str(row[exp_col]).strip()
                    exp_date = datetime.strptime(exp_str, "%d-%m-%Y")
                    if now <= exp_date <= threshold:
                        rec = row.to_dict()
                        rec["Days_Until_Expiry"] = (exp_date - now).days
                        expiring.append(rec)
                except:
                    continue
            print(f"  Expiring in 90 days: {sum(1 for e in expiring if e.get('Source')==label)}")

        # Snapshot for future diffs
        ids = set(df["Centre_ID"].dropna().astype(str)) if "Centre_ID" in df.columns else set()
        snap_path = SNAPSHOTS_DIR / f"irl_{prefix}_{today}.json"
        with open(snap_path, "w") as f2:
            json.dump({"date": today, "ids": list(ids)}, f2)

        # New registrations (diff with previous)
        prev = sorted([p for p in SNAPSHOTS_DIR.glob(f"irl_{prefix}_*.json") if p.name != snap_path.name], reverse=True)
        if prev:
            with open(prev[0]) as f2:
                prev_ids = set(json.load(f2).get("ids", []))
            new_ids = ids - prev_ids
            if new_ids and "Centre_ID" in df.columns:
                new_df = df[df["Centre_ID"].astype(str).isin(new_ids)]
                new_regs_all.append(new_df)
                print(f"  New registrations: {len(new_ids)}")
        else:
            print(f"  First run. New registrations from next run.")

    combined = pd.concat(all_centres, ignore_index=True) if all_centres else pd.DataFrame()
    exp_df = pd.DataFrame(expiring) if expiring else pd.DataFrame()
    new_df = pd.concat(new_regs_all, ignore_index=True) if new_regs_all else pd.DataFrame()

    print(f"\n  TOTAL: {len(combined):,} centres, {len(exp_df):,} expiring, {len(new_df):,} new")
    return {"all": combined, "expiring": exp_df, "new": new_df}


# ============================================================
# STEP 2C: ENRICH UK DATA (CQC API + Website Email Scraping)
# ============================================================
def enrich_from_api(loc_id):
    """Pull all details from CQC public API — returns dict or None"""
    try:
        url = f"https://api.cqc.org.uk/public/v1/locations/{loc_id}"
        r = requests.get(url, headers={"User-Agent": "CareDataEnricher/1.0"}, timeout=15)
        if r.status_code == 200:
            data = r.json()
            return {
                "phone": data.get("postalAddressLine1", "") and data.get("mainPhoneNumber", "") or data.get("mainPhoneNumber", ""),
                "website": data.get("website", ""),
                "beds": data.get("numberOfBeds", ""),
                "provider": data.get("providerName", ""),
                "manager": data.get("registeredManagerName", "") or "",
                "nominated_individual": data.get("nominatedIndividualName", "") or "",
                "last_inspection": data.get("lastInspection", {}).get("date", "") if data.get("lastInspection") else "",
                "specialisms": ", ".join(data.get("specialisms", [])) if data.get("specialisms") else "",
                "source": "api",
            }
    except:
        pass
    return None


def enrich_from_html(loc_id):
    """Scrape CQC profile pages for all available details.
    Fetches 3 pages: overview, /contact (website), /registration-info (beds)"""
    try:
        result = {"source": "html"}
        
        # === PAGE 1: Overview (phone, provider, manager, nominated, inspection, specialisms) ===
        url = f"https://www.cqc.org.uk/location/{loc_id}"
        r = requests.get(url, headers=HDR, timeout=15)
        if r.status_code != 200:
            return None
        html = r.text
        
        # Phone
        m = re.search(r'class="service-header__contact-info--phone"[^>]*>([^<]+)', html)
        result["phone"] = m.group(1).strip() if m else ""
        
        # Provider
        m = re.search(r'Provided and run by:.*?<a[^>]*>([^<]+)', html, re.DOTALL)
        result["provider"] = m.group(1).strip() if m else ""
        
        # Manager — <p class="...who-runs-service">Name<br/>Registered Manager</p>
        m = re.search(r'who-runs-service">([^<]+)<br\s*/?>Registered Manager', html)
        result["manager"] = m.group(1).strip() if m else ""
        
        # Nominated Individual
        m = re.search(r'who-runs-service">([^<]+)<br\s*/?>Nominated Individual', html)
        result["nominated_individual"] = m.group(1).strip() if m else ""
        
        # Last inspection/assessment date
        m = re.search(r'Latest (?:inspection|assessment):\s*\n?\s*(\d+\s+\w+\s+\d{4})', html)
        result["last_inspection"] = m.group(1).strip() if m else ""
        
        # Specialisms
        specs = re.findall(r'list-item--specialisms"[^>]*>(?:<span[^>]*>)?([^<]+)', html)
        result["specialisms"] = ", ".join(s.strip() for s in specs) if specs else ""
        
        # === PAGE 2: /contact (website) ===
        try:
            r2 = requests.get(f"{url}/contact", headers=HDR, timeout=15)
            if r2.status_code == 200:
                html2 = r2.text
                m = re.search(r'<h3>Website</h3>\s*<p>\s*<a[^>]*href="([^"]+)"', html2)
                result["website"] = m.group(1).strip() if m else ""
            else:
                result["website"] = ""
        except:
            result["website"] = ""
        
        # === PAGE 3: /registration-info (beds) ===
        try:
            r3 = requests.get(f"{url}/registration-info", headers=HDR, timeout=15)
            if r3.status_code == 200:
                html3 = r3.text
                m = re.search(r'[Aa]ccommodate a maximum of\s+(\d+)\s+service users', html3)
                result["beds"] = m.group(1) if m else ""
            else:
                result["beds"] = ""
        except:
            result["beds"] = ""
        
        return result
    except:
        return None


def scrape_email_from_website(website_url):
    """Visit a care home's website and try to find email addresses"""
    if not website_url or "cqc.org.uk" in website_url:
        return ""
    
    emails_found = set()
    
    # Common email regex
    email_re = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
    
    # Pages to check
    urls_to_try = [website_url]
    
    # Add common contact page paths
    base = website_url.rstrip("/")
    for path in ["/contact", "/contact-us", "/contactus", "/about", "/about-us"]:
        urls_to_try.append(base + path)
    
    for url in urls_to_try[:3]:  # Max 3 pages
        try:
            r = requests.get(url, headers=HDR, timeout=10, allow_redirects=True)
            if r.status_code == 200:
                found = email_re.findall(r.text)
                for e in found:
                    e = e.lower()
                    # Filter out junk
                    if any(skip in e for skip in ["@example", "@sentry", "@wixpress", "@wordpress",
                                                   "@jquery", "@google", "@facebook", ".png", ".jpg",
                                                   "@media", "@import", "@font", "@keyframe"]):
                        continue
                    emails_found.add(e)
        except:
            continue
        import time as _t
        _t.sleep(0.5)
    
    if emails_found:
        # Prefer info@, contact@, admin@, enquiries@, hello@
        preferred = ["info@", "contact@", "admin@", "enquir", "hello@", "reception@", "office@"]
        for pref in preferred:
            for e in emails_found:
                if pref in e:
                    return e
        return list(emails_found)[0]
    
    return ""


def enrich_uk(uk_data):
    """Enrich UK data with phone, website, beds, manager, email"""
    if not uk_data:
        return uk_data
    
    import time as _time
    
    print("\n" + "="*60)
    print("STEP 2C: ENRICHING UK DATA")
    print("="*60)
    
    c = uk_data["cols"]
    poor = uk_data["poor"].copy()
    
    if not c["ic"]:
        print("  No Location ID column — cannot enrich")
        return uk_data
    
    # Add new columns if they don't exist
    for col in ["Phone", "Website", "Email", "Beds", "Provider_Name", "Registered_Manager",
                 "Nominated_Individual", "Last_Inspection", "Specialisms"]:
        if col not in poor.columns:
            poor[col] = ""
    
    # Load cache
    cache_file = DATA_DIR / "uk" / "enrichment_cache_v2.json"
    cache = {}
    if cache_file.exists():
        try:
            with open(cache_file) as f:
                cache = json.load(f)
            print(f"  Loaded cache: {len(cache):,} entries")
        except:
            cache = {}
    
    # Find what needs enrichment
    needs_enrichment = []
    for idx, row in poor.iterrows():
        loc_id = str(row.get(c["ic"], "")).strip()
        if loc_id and loc_id != "nan" and loc_id not in cache:
            needs_enrichment.append((idx, loc_id))
    
    print(f"  Total homes: {len(poor):,}")
    print(f"  Already cached: {len(cache):,}")
    print(f"  Need to fetch: {len(needs_enrichment):,}")
    
    if needs_enrichment:
        print(f"\n  Fetching from CQC (3 pages per home)... (~{len(needs_enrichment) * 1.8 / 60:.0f} min)")
        
        api_ok = 0
        html_ok = 0
        errors = 0
        
        for i, (idx, loc_id) in enumerate(needs_enrichment):
            if i % 100 == 0 and i > 0:
                print(f"    {i:,}/{len(needs_enrichment):,} ({i*100//len(needs_enrichment)}%) — API: {api_ok}, HTML: {html_ok}, errors: {errors}")
                with open(cache_file, "w") as f:
                    json.dump(cache, f)
            
            # Try API first
            result = enrich_from_api(loc_id)
            if result and result.get("phone"):
                cache[loc_id] = result
                api_ok += 1
                _time.sleep(0.3)
                continue
            
            # Fallback to HTML
            result = enrich_from_html(loc_id)
            if result:
                cache[loc_id] = result
                html_ok += 1
            else:
                cache[loc_id] = {"phone": "", "website": "", "beds": "", "provider": "",
                                  "manager": "", "nominated_individual": "", "last_inspection": "",
                                  "specialisms": "", "source": "failed"}
                errors += 1
            
            _time.sleep(0.5)
        
        # Save cache
        with open(cache_file, "w") as f:
            json.dump(cache, f)
        print(f"\n  Fetching done: API={api_ok}, HTML={html_ok}, errors={errors}")
    
    # --- Now scrape emails from websites ---
    websites_to_scrape = []
    for idx, row in poor.iterrows():
        loc_id = str(row.get(c["ic"], "")).strip()
        if loc_id in cache:
            website = cache[loc_id].get("website", "")
            email = cache[loc_id].get("email", "")
            if website and not email:
                websites_to_scrape.append((loc_id, website))
    
    if websites_to_scrape:
        print(f"\n  Scraping emails from {len(websites_to_scrape):,} websites...")
        emails_found = 0
        
        for i, (loc_id, website) in enumerate(websites_to_scrape):
            if i % 50 == 0 and i > 0:
                print(f"    {i:,}/{len(websites_to_scrape):,} — {emails_found} emails found")
                with open(cache_file, "w") as f:
                    json.dump(cache, f)
            
            email = scrape_email_from_website(website)
            if email:
                cache[loc_id]["email"] = email
                emails_found += 1
        
        with open(cache_file, "w") as f:
            json.dump(cache, f)
        print(f"  Email scraping done: {emails_found:,} emails found")
    
    # --- Apply cache to dataframe ---
    applied = 0
    for idx, row in poor.iterrows():
        loc_id = str(row.get(c["ic"], "")).strip()
        if loc_id in cache:
            d = cache[loc_id]
            poor.at[idx, "Phone"] = d.get("phone", "")
            poor.at[idx, "Website"] = d.get("website", "")
            poor.at[idx, "Email"] = d.get("email", "")
            poor.at[idx, "Beds"] = str(d.get("beds", "")) if d.get("beds") else ""
            poor.at[idx, "Provider_Name"] = d.get("provider", "")
            poor.at[idx, "Registered_Manager"] = d.get("manager", "")
            poor.at[idx, "Nominated_Individual"] = d.get("nominated_individual", "")
            poor.at[idx, "Last_Inspection"] = d.get("last_inspection", "")
            poor.at[idx, "Specialisms"] = d.get("specialisms", "")
            applied += 1
    
    # Stats
    has_phone = (poor["Phone"].astype(str).str.strip() != "") & (poor["Phone"].astype(str) != "nan")
    has_web = (poor["Website"].astype(str).str.strip() != "") & (poor["Website"].astype(str) != "nan")
    has_email = (poor["Email"].astype(str).str.strip() != "") & (poor["Email"].astype(str) != "nan")
    has_beds = (poor["Beds"].astype(str).str.strip() != "") & (poor["Beds"].astype(str) != "nan") & (poor["Beds"].astype(str) != "0")
    has_mgr = (poor["Registered_Manager"].astype(str).str.strip() != "") & (poor["Registered_Manager"].astype(str) != "nan")
    
    print(f"\n  ENRICHMENT RESULTS:")
    print(f"  Phone:    {has_phone.sum():,}/{len(poor):,} ({has_phone.sum()*100//len(poor)}%)")
    print(f"  Website:  {has_web.sum():,}/{len(poor):,} ({has_web.sum()*100//len(poor)}%)")
    print(f"  Email:    {has_email.sum():,}/{len(poor):,} ({has_email.sum()*100//len(poor)}%)")
    print(f"  Beds:     {has_beds.sum():,}/{len(poor):,} ({has_beds.sum()*100//len(poor)}%)")
    print(f"  Manager:  {has_mgr.sum():,}/{len(poor):,} ({has_mgr.sum()*100//len(poor)}%)")
    
    uk_data["poor"] = poor
    return uk_data


# ============================================================
# STEP 3: OUTPUT
# ============================================================
def generate_output(uk, irl):
    print("\n" + "="*60)
    print("STEP 3: GENERATING REPORTS")
    print("="*60)

    today = datetime.now().strftime("%Y%m%d")
    uk_dir = OUTPUT_DIR / today / "UK"
    irl_dir = OUTPUT_DIR / today / "Ireland"
    uk_dir.mkdir(parents=True, exist_ok=True)
    irl_dir.mkdir(parents=True, exist_ok=True)

    # ---- UK ----
    uk_count = 0
    if uk:
        c = uk["cols"]
        fields = ["Name","Provider","Registered_Manager","Nominated_Individual",
                  "Address","City","Postcode","Phone","Email","Website",
                  "Rating","Service_Type","Region","Local_Authority",
                  "Beds","Last_Inspection","Specialisms","Location_ID"]

        # Poorly rated
        path = uk_dir / "UK_CQC_POORLY_RATED.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
            for _, row in uk["poor"].iterrows():
                # Use enriched columns if available, fall back to ODS columns
                phone = safe(row, "Phone") or safe(row, c["ph"])
                website = safe(row, "Website") or safe(row, c["wc"])
                beds = safe(row, "Beds") or safe(row, c["bc"])
                provider = safe(row, "Provider_Name") or safe(row, c["pc"])
                
                w.writerow({
                    "Name": safe(row, c["nc"]),
                    "Provider": provider,
                    "Registered_Manager": safe(row, "Registered_Manager"),
                    "Nominated_Individual": safe(row, "Nominated_Individual"),
                    "Address": safe(row, c["ac"]),
                    "City": safe(row, c["cc"]),
                    "Postcode": safe(row, c["poc"]),
                    "Phone": phone,
                    "Email": safe(row, "Email"),
                    "Website": website,
                    "Rating": safe(row, c["rc"]),
                    "Service_Type": safe(row, c["ipc"] or c["tc"]),
                    "Region": safe(row, c["rgc"]),
                    "Local_Authority": safe(row, c["lac"]),
                    "Beds": beds,
                    "Last_Inspection": safe(row, "Last_Inspection"),
                    "Specialisms": safe(row, "Specialisms"),
                    "Location_ID": safe(row, c["ic"]),
                }); uk_count += 1
        print(f"  UK_CQC_POORLY_RATED.csv: {uk_count:,} leads")

        # Downgrades
        if uk["downgrades"]:
            dg_path = uk_dir / "UK_CQC_DOWNGRADES.csv"
            dg_fields = fields + ["Previous_Rating", "Rating_Drop"]
            with open(dg_path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=dg_fields); w.writeheader()
                for rec in uk["downgrades"]:
                    w.writerow({
                        "Name": safe(rec,c["nc"]), "Provider": safe(rec,c["pc"]),
                        "Registered_Manager": "", "Nominated_Individual": "",
                        "Address": safe(rec,c["ac"]), "City": safe(rec,c["cc"]),
                        "Postcode": safe(rec,c["poc"]), "Phone": safe(rec,c["ph"]),
                        "Email": "", "Website": safe(rec,c["wc"]),
                        "Rating": rec.get("_curr",""),
                        "Service_Type": safe(rec, c["ipc"] or c["tc"]),
                        "Region": safe(rec,c["rgc"]), "Local_Authority": safe(rec,c["lac"]),
                        "Beds": safe(rec,c["bc"]),
                        "Last_Inspection": "", "Specialisms": "",
                        "Location_ID": safe(rec,c["ic"]),
                        "Previous_Rating": rec["_prev"],
                        "Rating_Drop": f"{rec['_prev']} -> {rec['_curr']}",
                    })
            print(f"  UK_CQC_DOWNGRADES.csv: {len(uk['downgrades']):,}")

        # New registrations
        if len(uk["new_regs"]) > 0:
            uk["new_regs"].to_csv(uk_dir/"UK_CQC_NEW_REGISTRATIONS.csv", index=False)
            print(f"  UK_CQC_NEW_REGISTRATIONS.csv: {len(uk['new_regs']):,}")

    # ---- IRELAND ----
    irl_count = 0
    if irl:
        irl_fields = ["Centre_ID","Centre_Title","Centre_Address","County",
                      "Centre_Phone","Person_in_Charge","Person_in_Charge_Phone",
                      "Registration_Provider","Registration_Provider_Phone",
                      "Maximum_Occupancy","Registration_Date","Registration_Expiry_Date",
                      "Management_Contacts","Registration_Number","URL","Source"]

        # All centres
        if len(irl["all"]) > 0:
            out_cols = [c for c in irl_fields if c in irl["all"].columns]
            irl["all"][out_cols].to_csv(irl_dir/"IRL_HIQA_ALL_CENTRES.csv", index=False)
            irl_count = len(irl["all"])
            print(f"  IRL_HIQA_ALL_CENTRES.csv: {irl_count:,} centres")

        # Expiring
        if len(irl["expiring"]) > 0:
            exp_fields = irl_fields + ["Days_Until_Expiry"]
            out_cols = [c for c in exp_fields if c in irl["expiring"].columns]
            irl["expiring"][out_cols].to_csv(irl_dir/"IRL_HIQA_EXPIRING_90_DAYS.csv", index=False)
            print(f"  IRL_HIQA_EXPIRING_90_DAYS.csv: {len(irl['expiring']):,} expiring")

        # New registrations
        if len(irl["new"]) > 0:
            out_cols = [c for c in irl_fields if c in irl["new"].columns]
            irl["new"][out_cols].to_csv(irl_dir/"IRL_HIQA_NEW_REGISTRATIONS.csv", index=False)
            print(f"  IRL_HIQA_NEW_REGISTRATIONS.csv: {len(irl['new']):,} new")

    # ---- SUMMARY ----
    print(f"\n{'='*60}")
    print(f"DONE")
    print(f"{'='*60}")

    print(f"\n  UK:")
    for f in sorted(uk_dir.glob("*")):
        print(f"    -> {f.name} ({f.stat().st_size:,} bytes)")

    print(f"\n  Ireland:")
    for f in sorted(irl_dir.glob("*")):
        print(f"    -> {f.name} ({f.stat().st_size:,} bytes)")

    total = uk_count + irl_count
    print(f"\n  TOTAL LEADS: {total:,}")
    print(f"  UK poorly rated: {uk_count:,}")
    print(f"  Ireland centres: {irl_count:,}")
    if irl and len(irl["expiring"]) > 0:
        print(f"  Ireland expiring: {len(irl['expiring']):,}")

    print(f"\n  Revenue potential (UK poorly rated):")
    print(f"  {uk_count:,} x £30-50/lead x 4 buyer types")
    print(f"  = £{uk_count*30*2:,} - £{uk_count*50*4:,}/month")
    print()


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    print("="*60)
    print(f"  CARE PROVIDER LEAD PIPELINE v4")
    print(f"  {datetime.now():%Y-%m-%d %H:%M}")
    print("="*60)

    download_all()
    uk = process_uk()
    uk = enrich_uk(uk)
    irl = process_ireland()
    generate_output(uk, irl)
