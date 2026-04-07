"""Debug failures in CR question parsing."""
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.import_cr_questions import parse_csv_file, parse_xlsx_file, split_question_text

all_qs = []
all_qs.extend(parse_csv_file("data/external/gre_questions.csv"))
all_qs.extend(parse_xlsx_file("data/external/gre_questions1.csv"))  
all_qs.extend(parse_xlsx_file("data/external/gre_questions2.csv"))

failures = []
for text, answer in all_qs:
    parsed = split_question_text(text)
    if parsed is None:
        failures.append((text, answer))

print(f"Fails: {len(failures)} / {len(all_qs)}")

# Show FULL text of first 2 failing questions
for i in [0, 6]:
    if i < len(failures):
        text, ans = failures[i]
        norm = re.sub(r"[ \t]+", " ", text)
        print(f"\n{'='*60}\nFailure {i} (ans={ans})\n{'='*60}")
        print(norm)
