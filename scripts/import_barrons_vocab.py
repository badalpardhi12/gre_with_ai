#!/usr/bin/env python3
"""Import Barrons 800 and 333 GRE vocabulary words with definitions,
plus new words from the 9566-word combined list."""

import sys, os, csv, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.database import db, VocabWord, init_db

BARRONS_800 = "data/external/barrons800_words.csv"
BARRONS_333 = "data/external/barrons333_words.csv"
COMBINED_WORDS = "data/external/vocab_combined.csv"


def parse_barrons800(path):
    """Parse Barrons 800 CSV: word,definition (first row is header)."""
    words = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 2:
                continue
            word = row[0].strip().lower()
            defn = row[1].strip()
            if not word or not defn:
                continue
            # Skip header row
            if word.startswith("barron") or word.startswith(","):
                continue
            words[word] = defn
    return words


def parse_barrons333(path):
    """Parse Barrons 333 CSV: word,definition,mnemonics..."""
    words = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 2:
                continue
            word = row[0].strip().lower()
            defn = row[1].strip()
            if not word or not defn:
                continue
            # Clean definition: remove leading/trailing whitespace, collapse spaces
            defn = " ".join(defn.split())
            words[word] = defn
    return words


def parse_combined_words(path):
    """Parse the combined word list (one word per line)."""
    words = set()
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            word = line.strip().lower()
            if word and word.isalpha():
                words.add(word)
    return words


def main():
    init_db()
    db.connect(reuse_if_open=True)

    # Load existing words from DB
    existing = set()
    for vw in VocabWord.select(VocabWord.word):
        existing.add(vw.word.lower())
    print(f"Existing vocab words in DB: {len(existing)}")

    # Parse all sources
    b800 = parse_barrons800(BARRONS_800) if os.path.exists(BARRONS_800) else {}
    b333 = parse_barrons333(BARRONS_333) if os.path.exists(BARRONS_333) else {}
    combined = parse_combined_words(COMBINED_WORDS) if os.path.exists(COMBINED_WORDS) else set()

    print(f"Barrons 800: {len(b800)} words with definitions")
    print(f"Barrons 333: {len(b333)} words with definitions")
    print(f"Combined word list: {len(combined)} words")

    # Merge definitions: prefer Barrons 333 (higher quality), then 800
    all_defs = {}
    all_defs.update(b800)
    all_defs.update(b333)  # 333 overwrites 800 where overlap exists

    inserted = 0
    skipped = 0

    with db.atomic():
        # Import words with definitions first
        for word, defn in all_defs.items():
            if word in existing:
                skipped += 1
                continue
            try:
                VocabWord.create(
                    word=word,
                    definition=defn,
                    source="barrons",
                    difficulty=3,
                )
                inserted += 1
                existing.add(word)
            except Exception:
                skipped += 1

    print(f"\nBarrons import: {inserted} inserted, {skipped} skipped (duplicates)")

    # Now import missing words from the combined list without definitions
    new_words_inserted = 0
    new_words_skipped = 0

    with db.atomic():
        for word in sorted(combined):
            if word in existing:
                new_words_skipped += 1
                continue
            try:
                VocabWord.create(
                    word=word,
                    definition="",
                    source="gre_word_collection",
                    difficulty=3,
                )
                new_words_inserted += 1
                existing.add(word)
            except Exception:
                new_words_skipped += 1

    print(f"Combined list import: {new_words_inserted} inserted, {new_words_skipped} skipped")

    total = VocabWord.select().count()
    print(f"\nTotal vocab words in DB: {total}")

    # Show breakdown by source
    for source in ["pervasive_gre", "barrons", "gre_word_collection"]:
        count = VocabWord.select().where(VocabWord.source == source).count()
        print(f"  {source}: {count}")


if __name__ == "__main__":
    main()
