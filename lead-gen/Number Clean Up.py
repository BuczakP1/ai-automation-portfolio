"""
Phone Number Cleanup Script
Removes garbage phone numbers from enriched files
Keeps only valid phone numbers
"""

import csv
import re
from pathlib import Path

# Configuration
ENRICHED_DIR = Path("D:/Desktop/Business Registrations/5 - Enriched")
BACKUP_DIR = ENRICHED_DIR / "Backup_Before_Phone_Cleanup"
BACKUP_DIR.mkdir(exist_ok=True)

def is_valid_phone(phone_str):
    """Check if a phone number is valid"""
    if not phone_str or phone_str.strip() == '':
        return False
    
    phone = phone_str.strip()
    digits_only = re.sub(r'\D', '', phone)
    
    # Must have 10-15 digits
    if not (10 <= len(digits_only) <= 15):
        return False
    
    # REJECT: Max integer values (parsing errors)
    if digits_only in ['2147483647', '2147483646', '2147483645', '9999999999']:
        return False
    
    # REJECT: Starts with date pattern (20XX)
    if digits_only.startswith('20') and len(digits_only) >= 8:
        return False
    
    # REJECT: All same digit (1111111111)
    if len(set(digits_only)) <= 2:
        return False
    
    # REJECT: Repeating patterns (123123123)
    if len(digits_only) >= 9:
        chunk_size = 3
        chunk = digits_only[:chunk_size]
        if digits_only == chunk * (len(digits_only) // chunk_size):
            return False
    
    # Must have formatting (spaces, dashes) OR start with + OR have parentheses
    has_formatting = (
        ' ' in phone or 
        '-' in phone or 
        '.' in phone or 
        phone.startswith('+') or 
        '(' in phone
    )
    
    if not has_formatting:
        return False
    
    # Country-specific validation
    
    # Irish numbers (+353 or 0)
    if digits_only.startswith('353'):
        local = digits_only[3:]
        if not local.startswith('0'):
            if 8 <= len(local) <= 9:
                return True
        return False
    
    elif digits_only.startswith('0'):
        # Irish/UK local
        if 10 <= len(digits_only) <= 11:
            return True
        return False
    
    # UK numbers (+44)
    elif digits_only.startswith('44'):
        local = digits_only[2:]
        if 10 <= len(local) <= 11:
            return True
        return False
    
    # Australian numbers (+61)
    elif digits_only.startswith('61'):
        local = digits_only[2:]
        if 9 <= len(local) <= 10:
            return True
        return False
    
    # New Zealand numbers (+64)
    elif digits_only.startswith('64'):
        local = digits_only[2:]
        if 8 <= len(local) <= 10:
            return True
        return False
    
    # Australian toll-free (1300, 1800)
    elif digits_only.startswith(('1300', '1800')):
        if len(digits_only) == 10:
            return True
        return False
    
    # Unknown format - reject for safety
    return False

def clean_phone_field(phone_field):
    """Clean phone field - keep only valid phones"""
    if not phone_field or phone_field.strip() == '':
        return ''
    
    # Split by semicolon (multiple phones)
    phones = [p.strip() for p in phone_field.split(';')]
    
    # Filter to valid only
    valid_phones = [p for p in phones if is_valid_phone(p)]
    
    # Return up to 3 valid phones
    return '; '.join(valid_phones[:3])

def clean_enriched_file(filepath):
    """Clean phone numbers in an enriched file"""
    print(f"\n{'='*80}")
    print(f"Processing: {filepath.name}")
    print(f"{'='*80}\n")
    
    # Backup original
    backup_path = BACKUP_DIR / filepath.name
    print(f"📋 Creating backup: {backup_path.name}")
    
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    
    with open(backup_path, 'w', encoding='utf-8') as f:
        f.write(content)
    
    print(f"✅ Backup created\n")
    
    # Read file
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)
    
    total = len(rows)
    phones_before = sum(1 for row in rows if row.get('phone', '').strip())
    
    print(f"Total rows: {total:,}")
    print(f"Rows with phones before: {phones_before:,}\n")
    
    # Clean phones
    cleaned_count = 0
    removed_count = 0
    
    for row in rows:
        phone_before = row.get('phone', '')
        phone_after = clean_phone_field(phone_before)
        
        if phone_before != phone_after:
            cleaned_count += 1
            
            if phone_before and not phone_after:
                removed_count += 1
        
        row['phone'] = phone_after
    
    phones_after = sum(1 for row in rows if row.get('phone', '').strip())
    
    # Save cleaned file
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    
    print(f"✅ Cleaned {cleaned_count:,} phone fields")
    print(f"   ❌ Removed {removed_count:,} completely invalid phone entries")
    print(f"   📞 Rows with phones after: {phones_after:,}")
    print(f"   📊 Change: {phones_before:,} → {phones_after:,} ({phones_after - phones_before:+,})")
    
    return {
        'file': filepath.name,
        'total': total,
        'phones_before': phones_before,
        'phones_after': phones_after,
        'cleaned': cleaned_count,
        'removed': removed_count
    }

def main():
    print("="*80)
    print("🔧 PHONE NUMBER CLEANUP TOOL")
    print("="*80)
    print()
    print(f"📂 Enriched folder: {ENRICHED_DIR}")
    print(f"💾 Backups folder: {BACKUP_DIR}")
    print()
    
    # Find all enriched files
    enriched_files = list(ENRICHED_DIR.glob("*_ENRICHED_*.csv"))
    
    if not enriched_files:
        print("❌ No enriched files found!")
        return
    
    print(f"Found {len(enriched_files)} enriched files:")
    for f in enriched_files:
        print(f"  • {f.name}")
    
    print()
    confirm = input("⚠️  This will modify all enriched files. Backups will be created. Continue? (yes/no): ")
    
    if confirm.lower() != 'yes':
        print("\n❌ Cancelled")
        return
    
    print("\n" + "="*80)
    print("Starting cleanup...")
    print("="*80)
    
    results = []
    
    for filepath in enriched_files:
        result = clean_enriched_file(filepath)
        results.append(result)
    
    # Summary
    print("\n" + "="*80)
    print("📊 CLEANUP SUMMARY")
    print("="*80)
    
    total_rows = sum(r['total'] for r in results)
    total_phones_before = sum(r['phones_before'] for r in results)
    total_phones_after = sum(r['phones_after'] for r in results)
    total_cleaned = sum(r['cleaned'] for r in results)
    total_removed = sum(r['removed'] for r in results)
    
    print(f"\nTotal rows processed: {total_rows:,}")
    print(f"Phones before: {total_phones_before:,}")
    print(f"Phones after: {total_phones_after:,}")
    print(f"Fields cleaned: {total_cleaned:,}")
    print(f"Invalid phones removed: {total_removed:,}")
    print(f"Change: {total_phones_before - total_phones_after:,} phones removed")
    
    print("\n" + "="*80)
    print("Per-file breakdown:")
    print("="*80)
    
    print(f"\n{'File':<45} {'Before':<10} {'After':<10} {'Change':<10}")
    print("-"*80)
    
    for r in results:
        change = r['phones_after'] - r['phones_before']
        print(f"{r['file']:<45} {r['phones_before']:<10,} {r['phones_after']:<10,} {change:+,}")
    
    print("="*80)
    print()
    print(f"💾 Original files backed up to: {BACKUP_DIR}")
    print(f"✅ Cleanup complete!")
    print()

if __name__ == "__main__":
    main()