import csv

input_file = r"D:\Desktop\Podcast\results\enriched_podcasts.csv"
output_file = r"D:\Desktop\Podcast\results\valid_podcasts.csv"

total = 0
valid = 0

with open(input_file, 'r', encoding='utf-8') as infile, \
     open(output_file, 'w', encoding='utf-8', newline='') as outfile:
    
    reader = csv.DictReader(infile)
    writer = csv.DictWriter(outfile, fieldnames=reader.fieldnames)
    writer.writeheader()
    
    for row in reader:
        total += 1
        if row.get('email_status', '').strip().lower() == 'valid':
            writer.writerow(row)
            valid += 1

print(f"Total rows: {total:,}")
print(f"Valid emails: {valid:,}")
print(f"Saved to: {output_file}")