"""Inspect xlsx question content format."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.import_cr_questions import parse_xlsx_file

qs = parse_xlsx_file("data/external/gre_questions1.csv")
# Print full text of first 3 questions
for i, (text, ans) in enumerate(qs[:3]):
    print(f"\n=== Q{i+1} (answer={ans}) ===")
    print(text)
    print("=" * 60)
