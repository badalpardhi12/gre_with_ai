#!/usr/bin/env python3
"""Import GRE vocabulary words from Pervasive-GRE dictionary into VocabWord table."""

import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from models.database import db, VocabWord, init_db

URL = "https://raw.githubusercontent.com/yiransheng/Pervasive-GRE/master/dictionaries/gre.json"

def main():
    init_db()
    print("Downloading GRE vocabulary JSON...")
    resp = requests.get(URL, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    print(f"Downloaded {len(data)} words")

    inserted = 0
    skipped = 0

    with db.atomic():
        for word, definition in data.items():
            word_clean = word.strip().lower()
            if not word_clean:
                continue
            # Clean up definition whitespace
            defn = " ".join(definition.split())
            try:
                VocabWord.create(
                    word=word_clean,
                    definition=defn,
                    source="pervasive_gre",
                    difficulty=3,
                )
                inserted += 1
            except Exception:
                # Duplicate word
                skipped += 1

    print(f"Done. Inserted: {inserted}, Skipped (duplicates): {skipped}")
    total = VocabWord.select().count()
    print(f"Total vocab words in DB: {total}")

if __name__ == "__main__":
    main()
