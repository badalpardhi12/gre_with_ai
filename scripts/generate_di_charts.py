"""
Generate matplotlib charts for Data Interpretation questions that lack stimuli.

Two distinct chart sets need generating:
1. Music downloads (Kaplan QR1 #18-20) — used by Q655, Q656
2. Legal association pie chart (Kaplan QR2 #18-20) — used by Q676, Q677

Each chart is saved as PNG to data/images/ and linked via a Stimulus row.

Usage:
    python scripts/generate_di_charts.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from models.database import db, init_db, Question, Stimulus

IMAGES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "images",
)
os.makedirs(IMAGES_DIR, exist_ok=True)

# Use dark style to match the app's dark theme
plt.style.use("dark_background")


def generate_music_downloads_chart():
    """
    Generate the Kaplan music-downloads chart set:
    - Left: Line graph of Total Digital Music Downloads from Ripster, 2000-2016
    - Right: Stacked bar chart of Ripster Digital Downloads by Genre, 2004-2016

    Data approximated from the original Kaplan question source.
    Returns path to saved PNG.
    """
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    fig.patch.set_facecolor("#1e1e1e")

    # Left: line graph 2000-2016
    years = [2000, 2002, 2004, 2006, 2008, 2010, 2012, 2014, 2016]
    downloads = [2, 4, 8, 11, 14, 18, 26, 30, 32]  # millions
    ax1 = axes[0]
    ax1.plot(years, downloads, marker="o", linewidth=2, color="#4FC3F7", markersize=6)
    ax1.set_title("Digital Music Downloads from Ripster, 2000-2016",
                   fontsize=11, color="white")
    ax1.set_xlabel("Year", color="#cccccc")
    ax1.set_ylabel("Downloads (in millions)", color="#cccccc")
    ax1.set_xticks(range(2000, 2017, 2))
    ax1.set_yticks(range(0, 41, 8))
    ax1.set_ylim(0, 40)
    ax1.grid(True, alpha=0.2)
    ax1.set_facecolor("#252525")
    ax1.tick_params(colors="#cccccc")

    # Right: stacked bar chart by genre, 2004-2016
    bar_years = [2004, 2008, 2012, 2016]
    # Approximate percentages (each year sums to 100%)
    pop = [40, 35, 25, 20]
    country = [10, 10, 12, 13]
    rnb_hiphop = [15, 18, 20, 22]
    latin = [5, 8, 18, 20]
    dance = [10, 12, 15, 15]
    other = [20, 17, 10, 10]

    ax2 = axes[1]
    width = 0.6
    x_pos = np.arange(len(bar_years))
    bottom = np.zeros(len(bar_years))
    colors = ["#FF6B6B", "#4ECDC4", "#95E1D3", "#FCE38A", "#F38181", "#AA96DA"]
    labels = ["Pop", "Country", "R&B/Hip-Hop", "Latin", "Dance", "Other"]

    for series, color, label in zip(
        [pop, country, rnb_hiphop, latin, dance, other], colors, labels
    ):
        ax2.bar(x_pos, series, width, bottom=bottom, label=label, color=color)
        bottom += np.array(series)

    ax2.set_title("Ripster Digital Downloads by Genre, 2004-2016",
                   fontsize=11, color="white")
    ax2.set_xlabel("Year", color="#cccccc")
    ax2.set_ylabel("Percent", color="#cccccc")
    ax2.set_xticks(x_pos)
    ax2.set_xticklabels(bar_years)
    ax2.set_yticks(range(0, 101, 10))
    ax2.set_ylim(0, 100)
    ax2.legend(loc="center left", bbox_to_anchor=(1, 0.5), facecolor="#252525",
               labelcolor="white", framealpha=0.9)
    ax2.set_facecolor("#252525")
    ax2.tick_params(colors="#cccccc")

    plt.tight_layout()
    out_path = os.path.join(IMAGES_DIR, "di_music_downloads.png")
    plt.savefig(out_path, dpi=110, facecolor="#1e1e1e", bbox_inches="tight")
    plt.close()
    return out_path


def generate_legal_association_pie():
    """
    Generate the Kaplan QR2 legal-association pie chart.
    Returns path to saved PNG.
    """
    fig, ax = plt.subplots(figsize=(8, 7))
    fig.patch.set_facecolor("#1e1e1e")

    labels = ["Criminal", "Government", "Other", "Corporate", "Tax",
              "Personal Injury", "Intellectual Property", "Family", "Contract"]
    sizes = [18, 15, 15, 14, 11, 10, 7, 6, 4]
    colors = ["#FF6B6B", "#4ECDC4", "#95E1D3", "#FCE38A", "#F38181",
              "#AA96DA", "#FCBAD3", "#A8D8EA", "#FFAAA5"]

    wedges, texts, autotexts = ax.pie(
        sizes,
        labels=labels,
        colors=colors,
        autopct="%1.0f%%",
        startangle=90,
        textprops={"color": "white", "fontsize": 10},
        wedgeprops={"edgecolor": "#1e1e1e", "linewidth": 1.5},
    )
    for autotext in autotexts:
        autotext.set_color("#1e1e1e")
        autotext.set_fontweight("bold")
        autotext.set_fontsize(10)

    ax.set_title("Members of a National Legal Association by Practice Area, 2015",
                  fontsize=12, color="white", pad=20)

    plt.tight_layout()
    out_path = os.path.join(IMAGES_DIR, "di_legal_association.png")
    plt.savefig(out_path, dpi=110, facecolor="#1e1e1e", bbox_inches="tight")
    plt.close()
    return out_path


def create_stimulus_for_image(image_path, title, stimulus_type="graph"):
    """Create a Stimulus row pointing to the saved image."""
    file_uri = f"file://{image_path}"
    html = (f'<div style="text-align: center; padding: 10px;">'
            f'<img src="{file_uri}" style="max-width: 100%; height: auto;" />'
            f'</div>')
    stim = Stimulus.create(
        stimulus_type=stimulus_type,
        title=title,
        content=html,
    )
    return stim


def main():
    init_db()
    db.connect(reuse_if_open=True)

    # Generate both charts
    print("Generating music downloads chart...")
    music_path = generate_music_downloads_chart()
    print(f"  Saved: {music_path}")

    print("Generating legal association pie chart...")
    legal_path = generate_legal_association_pie()
    print(f"  Saved: {legal_path}")

    # Create stimuli
    music_stim = create_stimulus_for_image(
        music_path,
        "Digital Music Downloads from Ripster (Line + Genre Stacked Bar)",
    )
    legal_stim = create_stimulus_for_image(
        legal_path,
        "Members of a National Legal Association by Practice Area, 2015",
    )

    # Link DI questions to stimuli
    # Q655, Q656 -> music chart
    # Q676, Q677 -> legal pie chart
    music_qids = [655, 656]
    legal_qids = [676, 677]

    updated = 0
    with db.atomic():
        for qid in music_qids:
            try:
                q = Question.get_by_id(qid)
                q.stimulus_id = music_stim.id
                q.save()
                updated += 1
                print(f"  Q{qid}: linked to music chart stimulus {music_stim.id}")
            except Question.DoesNotExist:
                print(f"  Q{qid}: not found (skipping)")

        for qid in legal_qids:
            try:
                q = Question.get_by_id(qid)
                q.stimulus_id = legal_stim.id
                q.save()
                updated += 1
                print(f"  Q{qid}: linked to legal pie chart stimulus {legal_stim.id}")
            except Question.DoesNotExist:
                print(f"  Q{qid}: not found (skipping)")

    print(f"\nDone. Linked {updated} DI questions to chart stimuli.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
