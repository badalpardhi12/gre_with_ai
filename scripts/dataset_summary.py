#!/usr/bin/env python3
"""Print dataset summary."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from models.database import db, init_db, Question, QuestionOption, VocabWord, AWAPrompt, Stimulus
from peewee import fn

init_db()
db.connect(reuse_if_open=True)

print("=== DATASET SUMMARY ===\n")

for measure in ["verbal", "quant", "awa"]:
    count = Question.select().where(Question.measure == measure).count()
    print(f"{measure.upper()} questions: {count}")

print("\nVerbal breakdown:")
for row in (Question.select(Question.subtype, fn.COUNT(Question.id).alias("cnt"))
            .where(Question.measure == "verbal")
            .group_by(Question.subtype)):
    print(f"  {row.subtype}: {row.cnt}")

print("\nQuant breakdown:")
for row in (Question.select(Question.subtype, fn.COUNT(Question.id).alias("cnt"))
            .where(Question.measure == "quant")
            .group_by(Question.subtype)):
    print(f"  {row.subtype}: {row.cnt}")

print(f"\nAWA prompts: {AWAPrompt.select().count()}")
print(f"Vocab words: {VocabWord.select().count()}")
with_def = VocabWord.select().where(VocabWord.definition != "").count()
print(f"  With definitions: {with_def}")
print(f"  Without definitions: {VocabWord.select().count() - with_def}")
print(f"Stimuli: {Stimulus.select().count()}")
print(f"Question options: {QuestionOption.select().count()}")
print(f"\nTOTAL questions: {Question.select().count()}")
