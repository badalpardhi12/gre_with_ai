"""
GRE topic taxonomy — single source of truth for the question bank organization.

Three levels: subject (verbal/quant/awa) → topic → subtopic.

Each subtopic has metadata used by:
- Question generation (target_count for filling the bank)
- Adaptive selection (frequency_weight for prioritization)
- Lesson system (one lesson per subtopic)
- Mastery tracking (mastery score per subtopic)
"""

# ── Quant Taxonomy ───────────────────────────────────────────────────

QUANT_TAXONOMY = {
    "arithmetic": {
        "display_name": "Arithmetic",
        "subtopics": {
            "integers_number_properties": {
                "display_name": "Integers & Number Properties",
                "target_count": 80,
                "frequency_weight": 1.0,
                "concepts": ["divisibility", "primes", "factors", "GCD/LCM",
                             "even/odd", "consecutive integers"],
            },
            "fractions_decimals": {
                "display_name": "Fractions & Decimals",
                "target_count": 70,
                "frequency_weight": 0.9,
                "concepts": ["operations", "comparing", "conversions", "repeating decimals"],
            },
            "exponents_roots": {
                "display_name": "Exponents & Roots",
                "target_count": 60,
                "frequency_weight": 0.85,
                "concepts": ["rules", "negative/fractional exponents", "simplifying radicals",
                             "scientific notation"],
            },
            "percents": {
                "display_name": "Percents",
                "target_count": 80,
                "frequency_weight": 1.0,
                "concepts": ["percent of", "percent change", "reverse percent",
                             "successive percents"],
            },
            "ratios_proportions": {
                "display_name": "Ratios & Proportions",
                "target_count": 60,
                "frequency_weight": 0.85,
                "concepts": ["direct variation", "inverse variation", "scaling"],
            },
            "sequences": {
                "display_name": "Sequences & Series",
                "target_count": 30,
                "frequency_weight": 0.6,
                "concepts": ["arithmetic", "geometric", "recursive"],
            },
        },
    },
    "algebra": {
        "display_name": "Algebra",
        "subtopics": {
            "linear_equations_systems": {
                "display_name": "Linear Equations & Systems",
                "target_count": 80,
                "frequency_weight": 1.0,
                "concepts": ["one variable", "systems of two", "elimination", "substitution"],
            },
            "quadratics": {
                "display_name": "Quadratic Equations",
                "target_count": 60,
                "frequency_weight": 0.85,
                "concepts": ["factoring", "quadratic formula", "completing the square",
                             "discriminant"],
            },
            "inequalities": {
                "display_name": "Inequalities",
                "target_count": 50,
                "frequency_weight": 0.8,
                "concepts": ["linear", "compound", "absolute value"],
            },
            "absolute_value": {
                "display_name": "Absolute Value",
                "target_count": 30,
                "frequency_weight": 0.6,
                "concepts": ["equations", "inequalities", "distance interpretation"],
            },
            "functions": {
                "display_name": "Functions",
                "target_count": 50,
                "frequency_weight": 0.8,
                "concepts": ["notation", "domain/range", "composition", "transformations"],
            },
            "coordinate_geometry": {
                "display_name": "Coordinate Geometry",
                "target_count": 70,
                "frequency_weight": 0.9,
                "concepts": ["slope", "distance", "midpoint", "line equations",
                             "parabolas", "circles in xy-plane"],
            },
            "word_problems": {
                "display_name": "Word Problems",
                "target_count": 80,
                "frequency_weight": 1.0,
                "concepts": ["rate", "work", "mixture", "age", "distance/speed/time", "interest"],
            },
        },
    },
    "geometry": {
        "display_name": "Geometry",
        "subtopics": {
            "lines_angles": {
                "display_name": "Lines & Angles",
                "target_count": 50,
                "frequency_weight": 0.8,
                "concepts": ["parallel", "perpendicular", "transversals", "angle relationships"],
            },
            "triangles": {
                "display_name": "Triangles",
                "target_count": 80,
                "frequency_weight": 1.0,
                "concepts": ["angle sum", "types", "Pythagorean", "special right (30-60-90, 45-45-90)",
                             "similarity", "congruence", "area"],
            },
            "quadrilaterals_polygons": {
                "display_name": "Quadrilaterals & Polygons",
                "target_count": 50,
                "frequency_weight": 0.8,
                "concepts": ["squares", "rectangles", "parallelograms", "trapezoids",
                             "interior/exterior angles", "regular polygons"],
            },
            "circles": {
                "display_name": "Circles",
                "target_count": 60,
                "frequency_weight": 0.85,
                "concepts": ["circumference", "area", "arcs", "sectors",
                             "inscribed/central angles", "tangents", "chords"],
            },
            "solids_3d": {
                "display_name": "3D Solids",
                "target_count": 30,
                "frequency_weight": 0.6,
                "concepts": ["rectangular solids", "cubes", "cylinders",
                             "surface area", "volume"],
            },
        },
    },
    "data_analysis": {
        "display_name": "Data Analysis & Statistics",
        "subtopics": {
            "descriptive_stats": {
                "display_name": "Descriptive Statistics",
                "target_count": 60,
                "frequency_weight": 0.85,
                "concepts": ["mean", "median", "mode", "range", "weighted average"],
            },
            "spread_distributions": {
                "display_name": "Spread & Distributions",
                "target_count": 50,
                "frequency_weight": 0.8,
                "concepts": ["standard deviation", "variance", "IQR", "quartiles",
                             "percentiles", "normal distribution", "histograms"],
            },
            "counting_combinatorics": {
                "display_name": "Counting & Combinatorics",
                "target_count": 50,
                "frequency_weight": 0.8,
                "concepts": ["permutations", "combinations", "fundamental counting principle"],
            },
            "probability": {
                "display_name": "Probability",
                "target_count": 60,
                "frequency_weight": 0.85,
                "concepts": ["simple", "compound", "conditional", "mutually exclusive",
                             "independent"],
            },
            "sets": {
                "display_name": "Sets",
                "target_count": 30,
                "frequency_weight": 0.6,
                "concepts": ["Venn diagrams", "union/intersection", "complement"],
            },
            "data_interpretation": {
                "display_name": "Data Interpretation",
                "target_count": 80,
                "frequency_weight": 1.0,
                "concepts": ["table", "bar chart", "line chart", "pie chart",
                             "scatter plot", "multi-chart", "frequency"],
            },
        },
    },
}

# ── Verbal Taxonomy ──────────────────────────────────────────────────

VERBAL_TAXONOMY = {
    "text_completion": {
        "display_name": "Text Completion",
        "subtopics": {
            "tc_1_blank": {
                "display_name": "Text Completion — 1 Blank",
                "target_count": 100,
                "frequency_weight": 0.9,
                "concepts": ["vocab-driven", "context-clue inference"],
            },
            "tc_2_blank": {
                "display_name": "Text Completion — 2 Blanks",
                "target_count": 130,
                "frequency_weight": 1.0,
                "concepts": ["logical relationship", "clue/contrast linkage"],
            },
            "tc_3_blank": {
                "display_name": "Text Completion — 3 Blanks",
                "target_count": 100,
                "frequency_weight": 0.9,
                "concepts": ["chained inference", "multi-step coherence"],
            },
        },
    },
    "sentence_equivalence": {
        "display_name": "Sentence Equivalence",
        "subtopics": {
            "se_synonyms": {
                "display_name": "Synonyms — Positive/Negative Valence",
                "target_count": 80,
                "frequency_weight": 0.9,
                "concepts": ["paired synonyms", "valence matching"],
            },
            "se_contrast": {
                "display_name": "Contextual Contrast & Cause-Effect",
                "target_count": 60,
                "frequency_weight": 0.85,
                "concepts": ["definition cues", "contrast cues", "cause/effect"],
            },
        },
    },
    "reading_comprehension": {
        "display_name": "Reading Comprehension",
        "subtopics": {
            "rc_main_idea": {
                "display_name": "RC — Main Idea / Primary Purpose",
                "target_count": 60,
                "frequency_weight": 0.9,
                "concepts": ["thesis", "central claim", "primary purpose"],
            },
            "rc_detail": {
                "display_name": "RC — Detail / Specific Information",
                "target_count": 50,
                "frequency_weight": 0.8,
                "concepts": ["specific facts", "explicit content"],
            },
            "rc_inference": {
                "display_name": "RC — Inference",
                "target_count": 80,
                "frequency_weight": 1.0,
                "concepts": ["implicit conclusions", "must-be-true"],
            },
            "rc_tone_attitude": {
                "display_name": "RC — Tone & Author's Attitude",
                "target_count": 40,
                "frequency_weight": 0.7,
                "concepts": ["author voice", "evaluative stance"],
            },
            "rc_structure_function": {
                "display_name": "RC — Structure / Function",
                "target_count": 40,
                "frequency_weight": 0.7,
                "concepts": ["paragraph role", "sentence function"],
            },
            "rc_vocab_in_context": {
                "display_name": "RC — Vocabulary in Context",
                "target_count": 30,
                "frequency_weight": 0.6,
                "concepts": ["word meaning given context"],
            },
            "rc_select_sentence": {
                "display_name": "RC — Select Sentence",
                "target_count": 30,
                "frequency_weight": 0.6,
                "concepts": ["highlighted-sentence selection"],
            },
            "rc_multi_answer": {
                "display_name": "RC — Multi-Answer (Select All That Apply)",
                "target_count": 50,
                "frequency_weight": 0.8,
                "concepts": ["all-true selection"],
            },
        },
    },
    "critical_reasoning": {
        "display_name": "Critical Reasoning",
        "subtopics": {
            "cr_assumption": {
                "display_name": "CR — Assumption",
                "target_count": 30,
                "frequency_weight": 0.7,
                "concepts": ["necessary assumption", "sufficient assumption"],
            },
            "cr_strengthen": {
                "display_name": "CR — Strengthen",
                "target_count": 30,
                "frequency_weight": 0.7,
                "concepts": ["best-supports argument"],
            },
            "cr_weaken": {
                "display_name": "CR — Weaken",
                "target_count": 30,
                "frequency_weight": 0.7,
                "concepts": ["undermines conclusion"],
            },
            "cr_evaluate": {
                "display_name": "CR — Evaluate / Paradox / Flaw",
                "target_count": 30,
                "frequency_weight": 0.7,
                "concepts": ["evaluate argument", "resolve paradox", "identify flaw"],
            },
        },
    },
}

# ── AWA Taxonomy ─────────────────────────────────────────────────────

AWA_TAXONOMY = {
    "issue_task": {
        "display_name": "Analyze an Issue",
        "subtopics": {
            "issue_education": {"display_name": "Issue — Education", "target_count": 15},
            "issue_technology": {"display_name": "Issue — Technology", "target_count": 15},
            "issue_government": {"display_name": "Issue — Government & Politics", "target_count": 15},
            "issue_ethics": {"display_name": "Issue — Ethics", "target_count": 15},
            "issue_science": {"display_name": "Issue — Science", "target_count": 10},
            "issue_arts": {"display_name": "Issue — Arts & Humanities", "target_count": 10},
            "issue_business": {"display_name": "Issue — Business & Society", "target_count": 10},
        },
    },
}

# ── Helpers ──────────────────────────────────────────────────────────

ALL_SUBTOPICS_QUANT = [
    (topic, sub) for topic, td in QUANT_TAXONOMY.items()
    for sub in td["subtopics"].keys()
]
ALL_SUBTOPICS_VERBAL = [
    (topic, sub) for topic, td in VERBAL_TAXONOMY.items()
    for sub in td["subtopics"].keys()
]


def get_subtopic_meta(measure, subtopic):
    """Return metadata dict for a (measure, subtopic) pair."""
    if measure == "quant":
        for topic, td in QUANT_TAXONOMY.items():
            if subtopic in td["subtopics"]:
                return {**td["subtopics"][subtopic], "topic": topic}
    elif measure == "verbal":
        for topic, td in VERBAL_TAXONOMY.items():
            if subtopic in td["subtopics"]:
                return {**td["subtopics"][subtopic], "topic": topic}
    elif measure == "awa":
        for topic, td in AWA_TAXONOMY.items():
            if subtopic in td["subtopics"]:
                return {**td["subtopics"][subtopic], "topic": topic}
    return None


def total_target_count():
    """Sum of all target_count values across the taxonomy. Should be ~4,000."""
    total = 0
    for taxonomy in (QUANT_TAXONOMY, VERBAL_TAXONOMY):
        for _, td in taxonomy.items():
            for _, sd in td["subtopics"].items():
                total += sd["target_count"]
    awa_total = sum(sd["target_count"]
                    for _, td in AWA_TAXONOMY.items()
                    for _, sd in td["subtopics"].items())
    return total + awa_total


def get_taxonomy_summary():
    """Return a flat list of all (measure, topic, subtopic, target_count) tuples."""
    rows = []
    for topic, td in QUANT_TAXONOMY.items():
        for sub, sd in td["subtopics"].items():
            rows.append(("quant", topic, sub, sd["target_count"]))
    for topic, td in VERBAL_TAXONOMY.items():
        for sub, sd in td["subtopics"].items():
            rows.append(("verbal", topic, sub, sd["target_count"]))
    for topic, td in AWA_TAXONOMY.items():
        for sub, sd in td["subtopics"].items():
            rows.append(("awa", topic, sub, sd["target_count"]))
    return rows


if __name__ == "__main__":
    rows = get_taxonomy_summary()
    print(f"Taxonomy summary: {len(rows)} subtopics, target total: {total_target_count()}")
    for measure, topic, sub, count in rows:
        print(f"  {measure:6s} | {topic:25s} | {sub:35s} | {count}")
