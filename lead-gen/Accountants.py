"""
Party Scraper - Accountants
Finds accountants across Ireland, UK, Australia, and New Zealand using Google Maps
Outputs separate CSV files per country
"""

import requests
import csv
import time
from pathlib import Path

# Configuration
OUTPUT_DIR = Path("D:/Desktop/Party Scrapers")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Cities to scrape per country
CITIES = {
    'ireland': [
        'Dublin', 'Cork', 'Galway', 'Limerick', 'Waterford',
        'Drogheda', 'Dundalk', 'Kilkenny', 'Sligo', 'Tralee'
    ],
    'uk': [
        'London', 'Manchester', 'Birmingham', 'Leeds', 'Glasgow', 'Edinburgh',
        'Liverpool', 'Bristol', 'Sheffield', 'Newcastle', 'Nottingham',
        'Leicester', 'Cardiff', 'Belfast', 'Brighton', 'Reading',
        'Plymouth', 'Aberdeen', 'Southampton', 'Coventry'
    ],
    'australia': [
        'Sydney', 'Melbourne', 'Brisbane', 'Perth', 'Adelaide', 'Canberra',
        'Gold Coast', 'Newcastle', 'Wollongong', 'Hobart', 'Geelong',
        'Townsville', 'Cairns', 'Darwin', 'Toowoomba'
    ],
    'new_zealand': [
        'Auckland', 'Wellington', 'Christchurch', 'Hamilton',
        'Tauranga', 'Dunedin', 'Palmerston North', 'Napier'
    ]
}

# Search keyword variations
KEYWORDS = [
    'accountants',
    'accounting firms',
    'chartered accountants',
    'bookkeepers',
    'tax accountants'
]

def get_serper_api_key():
    """Prompt user for Serper API key"""
    print("\n" + "="*70)
    print("SERPER API KEY REQUIRED")
    print("="*70)
    print("\nPlease paste your Serper API key:")
    print("(Get it from: https://serper.dev/dashboard)")
    print()
    api_key = input("API Key: ").strip()
    return api_key

def search_google_maps(query, api_key):
    """Search Google Maps using Serper API"""
    url = "https://google.serper.dev/maps"
    
    headers = {
        'X-API-KEY': api_key,
        'Content-Type': 'application/json'
    }
    
    payload = {
        'q': query,
        'num': 100  # Get up to 100 results per search
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"   ⚠️  Error searching '{query}': {e}")
        return None

def extract_social_media(result):
    """Try to extract social media links from various possible fields"""
    social = {
        'facebook': '',
        'linkedin': '',
        'instagram': '',
        'twitter': ''
    }
    
    # Check if there's a socialProfiles field
    if 'socialProfiles' in result and result['socialProfiles']:
        profiles = result['socialProfiles']
        if isinstance(profiles, dict):
            social['facebook'] = profiles.get('facebook', '')
            social['linkedin'] = profiles.get('linkedin', '')
            social['instagram'] = profiles.get('instagram', '')
            social['twitter'] = profiles.get('twitter', '')
    
    # Check if there's a links array
    if 'links' in result and result['links']:
        links = result['links']
        if isinstance(links, list):
            for link in links:
                if not link:
                    continue
                link_lower = str(link).lower()
                if 'facebook.com' in link_lower and not social['facebook']:
                    social['facebook'] = link
                elif 'linkedin.com' in link_lower and not social['linkedin']:
                    social['linkedin'] = link
                elif 'instagram.com' in link_lower and not social['instagram']:
                    social['instagram'] = link
                elif ('twitter.com' in link_lower or 'x.com' in link_lower) and not social['twitter']:
                    social['twitter'] = link
    
    # Sometimes social links are in the website field or other fields
    # Check all string fields for social media URLs
    for key, value in result.items():
        if isinstance(value, str) and value:
            value_lower = value.lower()
            if 'facebook.com' in value_lower and not social['facebook']:
                social['facebook'] = value
            elif 'linkedin.com' in value_lower and not social['linkedin']:
                social['linkedin'] = value
            elif 'instagram.com' in value_lower and not social['instagram']:
                social['instagram'] = value
            elif ('twitter.com' in value_lower or 'x.com' in value_lower) and not social['twitter']:
                social['twitter'] = value
    
    return social

def extract_business_info(result):
    """Extract relevant business information from search result"""
    # Get social media
    social = extract_social_media(result)
    
    # Build complete business info
    info = {
        'name': result.get('title', ''),
        'address': result.get('address', ''),
        'phone': result.get('phoneNumber', ''),
        'website': result.get('website', ''),
        'email': '',  # Will be filled by enricher
        'facebook': social['facebook'],
        'linkedin': social['linkedin'],
        'instagram': social['instagram'],
        'twitter': social['twitter'],
        'rating': result.get('rating', ''),
        'reviews': result.get('reviews', ''),
        'category': result.get('category', ''),
        'place_id': result.get('placeId', '')  # Useful for deduplication
    }
    
    return info

def scrape_country(country, cities, api_key):
    """Scrape all cities in a country"""
    print(f"\n{'='*70}")
    print(f"🌍 SCRAPING {country.upper()}")
    print(f"{'='*70}\n")
    
    all_results = []
    seen_ids = set()  # Track duplicates by place_id
    seen_names = set()  # Fallback: track by name
    
    total_searches = len(cities) * len(KEYWORDS)
    current_search = 0
    
    for city in cities:
        for keyword in KEYWORDS:
            current_search += 1
            query = f"{keyword} {city} {country}"
            
            print(f"[{current_search}/{total_searches}] Searching: {query}")
            
            data = search_google_maps(query, api_key)
            
            if data and 'places' in data:
                for place in data['places']:
                    business = extract_business_info(place)
                    
                    # Skip duplicates
                    place_id = business.get('place_id', '')
                    name = business.get('name', '')
                    
                    if place_id and place_id in seen_ids:
                        continue
                    if not place_id and name and name in seen_names:
                        continue
                    
                    # Track this business
                    if place_id:
                        seen_ids.add(place_id)
                    if name:
                        seen_names.add(name)
                    
                    all_results.append(business)
                
                print(f"   ✅ Found {len(data['places'])} results (Total unique: {len(all_results)})")
            else:
                print(f"   ⚠️  No results")
            
            # Small delay to avoid rate limiting
            time.sleep(0.5)
    
    print(f"\n✅ Total unique businesses found: {len(all_results)}")
    return all_results

def save_to_csv(data, filename):
    """Save results to CSV file"""
    filepath = OUTPUT_DIR / filename
    
    if not data:
        print(f"⚠️  No data to save for {filename}")
        return
    
    fieldnames = [
        'name', 'address', 'phone', 'website', 'email',
        'facebook', 'linkedin', 'instagram', 'twitter',
        'rating', 'reviews', 'category', 'place_id'
    ]
    
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(data)
    
    file_size = filepath.stat().st_size / 1024  # KB
    print(f"💾 Saved: {filename} ({len(data)} records, {file_size:.1f} KB)")

def main():
    print("="*70)
    print("🔍 PARTY SCRAPER - ACCOUNTANTS")
    print("="*70)
    print("\nThis will scrape accountants from Google Maps across 4 countries")
    print("Estimated searches: ~265")
    print("Estimated time: 5-10 minutes")
    print()
    
    # Get API key
    api_key = get_serper_api_key()
    
    if not api_key:
        print("❌ No API key provided. Exiting.")
        return
    
    # Scrape each country
    for country, cities in CITIES.items():
        results = scrape_country(country, cities, api_key)
        
        # Save to CSV
        filename = f"accountants_{country}.csv"
        save_to_csv(results, filename)
        
        print()
    
    # Summary
    print("="*70)
    print("✅ SCRAPING COMPLETE!")
    print("="*70)
    print(f"\n📂 Files saved to: {OUTPUT_DIR}")
    print("\nFiles created:")
    print("   1. accountants_ireland.csv")
    print("   2. accountants_uk.csv")
    print("   3. accountants_australia.csv")
    print("   4. accountants_new_zealand.csv")
    print("\nColumns included:")
    print("   - name, address, phone, website")
    print("   - email (empty - will be filled by enricher)")
    print("   - facebook, linkedin, instagram, twitter")
    print("   - rating, reviews, category, place_id")
    print()

if __name__ == "__main__":
    main()