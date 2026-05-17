import csv
import os

# ============================================================
# LEAD TIER SEPARATOR & CLEANER
# Takes enriched business registration data and creates
# clean, sellable files sorted into pricing tiers
# ============================================================

# SETTINGS - Update paths to match your folder structure
input_folder = r"D:\Desktop\Business Registrations\5 - Enriched"
output_folder = r"D:\Desktop\Business Registrations\Ready To Sell"

# Auto-detect enriched CSV files in the input folder
# Matches any file containing "ENRICHED" in the name
files = {}
if os.path.exists(input_folder):
    for f in os.listdir(input_folder):
        if f.endswith(".csv") and "ENRICHED" in f.upper():
            # Extract country name from filename (first part before _)
            name = f.split("_")[0].capitalize()
            # Handle common naming
            if name.lower() in ["ireland", "irl"]:
                name = "Ireland"
            elif name.lower() in ["uk", "united"]:
                name = "UK"
            elif name.lower() in ["australia", "aus"]:
                name = "Australia"
            elif name.lower() in ["nz", "new"]:
                name = "NZ"
            files[name] = f
    print(f"  📂 Auto-detected {len(files)} enriched file(s):")
    for country, fname in files.items():
        print(f"     • {country}: {fname}")
else:
    print(f"  ❌ Input folder not found: {input_folder}")
    exit()

# Columns to KEEP in the clean output
# The script will try each name and keep whichever exists in the CSV
keep_columns = {
    "company_name": ["company_name", "CompanyName", "company_name_ascii", "entityName"],
    "address": ["company_address_1", "RegAddress.AddressLine1", "address", "registeredAddress"],
    "address_2": ["company_address_2", "RegAddress.AddressLine2", "address_2"],
    "address_3": ["company_address_3", "RegAddress.PostCode", "address_3"],
    "address_4": ["company_address_4", "RegAddress.Country", "address_4"],
    "registration_date": ["company_reg_date", "IncorporationDate", "registration_date", "incorporationDate"],
    "industry_code": ["nace_v2_code", "SICCode.SicText_1", "industry_code", "industryCode"],
    "email": ["email"],
    "phone": ["phone"],
    "website": ["website"],
    "facebook": ["facebook"],
    "linkedin": ["linkedin"],
    "instagram": ["instagram"],
    "twitter": ["twitter"],
    "confidence_score": ["confidence_score"],
}

# Clean output column names
output_headers = [
    "company_name",
    "address",
    "address_2",
    "address_3",
    "address_4",
    "registration_date",
    "industry_code",
    "email",
    "phone",
    "website",
    "facebook",
    "linkedin",
    "instagram",
    "twitter",
    "confidence_score",
]


def find_column(row, possible_names):
    """Find the first matching column name from a list of possibilities."""
    for name in possible_names:
        if name in row and row[name].strip():
            return row[name].strip()
    return ""


def clean_row(row):
    """Extract only the sellable columns from a row."""
    cleaned = {}
    for output_name, possible_names in keep_columns.items():
        cleaned[output_name] = find_column(row, possible_names)
    return cleaned


def get_tier(row):
    """Determine pricing tier based on available contact info."""
    has_email = bool(row.get("email", "").strip())
    has_phone = bool(row.get("phone", "").strip())

    if has_email and has_phone:
        return "PREMIUM"
    elif has_email:
        return "STANDARD"
    elif has_phone:
        return "BASIC"
    else:
        return "RAW"


def save_tier(filepath, headers, rows):
    """Save a tier to CSV, sorted by confidence score."""
    rows.sort(key=lambda x: float(x.get("confidence_score") or 0), reverse=True)
    with open(filepath, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


# ============================================================
# MAIN PROCESSING
# ============================================================

print("=" * 60)
print("  LEAD TIER SEPARATOR & CLEANER")
print("=" * 60)

grand_totals = {"PREMIUM": 0, "STANDARD": 0, "BASIC": 0, "RAW": 0}
grand_total = 0

for country, filename in files.items():
    filepath = os.path.join(input_folder, filename)

    if not os.path.exists(filepath):
        print(f"\n⚠️  File not found: {filename} — skipping")
        continue

    print(f"\n{'='*60}")
    print(f"  Processing: {country}")
    print(f"{'='*60}")

    # Create country output folder
    country_folder = os.path.join(output_folder, country)
    os.makedirs(country_folder, exist_ok=True)

    # Read and process
    tiers = {"PREMIUM": [], "STANDARD": [], "BASIC": [], "RAW": []}

    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            cleaned = clean_row(row)
            tier = get_tier(cleaned)
            tiers[tier].append(cleaned)

    total = sum(len(v) for v in tiers.values())
    grand_total += total

    # Save each tier
    country_lower = country.lower().replace(" ", "_")
    for tier_name, tier_data in tiers.items():
        if tier_data:
            outpath = os.path.join(
                country_folder, f"{country_lower}_{tier_name}.csv"
            )
            save_tier(outpath, output_headers, tier_data)

        count = len(tier_data)
        pct = (count / total * 100) if total > 0 else 0
        grand_totals[tier_name] += count

        icon = {"PREMIUM": "💎", "STANDARD": "📧", "BASIC": "📞", "RAW": "📄"}
        print(f"  {icon[tier_name]} {tier_name}: {count:,} leads ({pct:.1f}%)")

    print(f"  📊 Total: {total:,} leads")
    print(f"  📁 Saved to: {country_folder}")

# ============================================================
# GRAND SUMMARY
# ============================================================

print(f"\n{'='*60}")
print("  GRAND SUMMARY")
print(f"{'='*60}")
print(f"\n  💎 PREMIUM (Email + Phone):  {grand_totals['PREMIUM']:,}")
print(f"  📧 STANDARD (Email only):    {grand_totals['STANDARD']:,}")
print(f"  📞 BASIC (Phone only):       {grand_totals['BASIC']:,}")
print(f"  📄 RAW (No contact):         {grand_totals['RAW']:,}")
print(f"\n  📊 TOTAL ACROSS ALL COUNTRIES: {grand_total:,}")

print(f"\n  📁 Output folder: {output_folder}")
print(f"\n{'='*60}")
print("  ✅ DONE — Ready to sell!")
print(f"{'='*60}\n")