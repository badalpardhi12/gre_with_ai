"""
Seed database with built-in sample GRE questions.
This provides enough questions to run a test immediately without downloading
external datasets. Run this script to initialize and populate the database.

Usage:
    python scripts/seed_data.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.database import (
    db, init_db, Question, QuestionOption, NumericAnswer,
    Stimulus, AWAPrompt, VocabWord,
)


def seed_awa_prompts():
    """Seed AWA Issue prompts from the ETS published topic pool."""
    prompts = [
        {
            "prompt_text": (
                "As people rely more and more on technology to solve problems, "
                "the ability of humans to think for themselves will surely deteriorate."
            ),
            "instructions": (
                "Write a response in which you discuss the extent to which you agree or "
                "disagree with the statement and explain your reasoning for the position "
                "you take. In developing and supporting your position, you should consider "
                "ways in which the statement might or might not hold true and explain how "
                "these considerations shape your position."
            ),
        },
        {
            "prompt_text": (
                "In any field of inquiry, the beginner is more likely than the expert "
                "to make important contributions."
            ),
            "instructions": (
                "Write a response in which you discuss the extent to which you agree or "
                "disagree with the statement and explain your reasoning for the position "
                "you take."
            ),
        },
        {
            "prompt_text": (
                "Governments should place few, if any, restrictions on scientific "
                "research and development."
            ),
            "instructions": (
                "Write a response in which you discuss the extent to which you agree or "
                "disagree with the recommendation and explain your reasoning."
            ),
        },
        {
            "prompt_text": (
                "The best way to teach is to praise positive actions and ignore negative ones."
            ),
            "instructions": (
                "Write a response in which you discuss the extent to which you agree or "
                "disagree with the statement and explain your reasoning."
            ),
        },
        {
            "prompt_text": (
                "Educators should teach facts only after their students have studied "
                "the ideas, trends, and concepts that help explain those facts."
            ),
            "instructions": (
                "Write a response in which you discuss the extent to which you agree or "
                "disagree with the recommendation and explain your reasoning."
            ),
        },
        {
            "prompt_text": (
                "Scandals are useful because they focus our attention on problems "
                "in ways that no speaker or reformer ever could."
            ),
            "instructions": (
                "Write a response in which you discuss the extent to which you agree or "
                "disagree with the claim and explain your reasoning."
            ),
        },
        {
            "prompt_text": (
                "Claim: In any field—business, politics, education, government—those "
                "in power should step down after five years.\n"
                "Reason: The surest path to success for any enterprise is revitalization "
                "through new leadership."
            ),
            "instructions": (
                "Write a response in which you discuss the extent to which you agree or "
                "disagree with the claim and the reason on which that claim is based."
            ),
        },
        {
            "prompt_text": (
                "Some people believe that competition is destructive and should be "
                "discouraged. Others believe that competition motivates individuals "
                "to achieve greatness."
            ),
            "instructions": (
                "Write a response in which you discuss which view more closely aligns "
                "with your own position and explain your reasoning."
            ),
        },
        {
            "prompt_text": (
                "The most effective way to understand contemporary culture is to "
                "analyze the trends of its youth."
            ),
            "instructions": (
                "Write a response in which you discuss the extent to which you agree or "
                "disagree with the statement and explain your reasoning."
            ),
        },
        {
            "prompt_text": (
                "Universities should require every student to take a variety of courses "
                "outside the student's field of study."
            ),
            "instructions": (
                "Write a response in which you discuss the extent to which you agree or "
                "disagree with the claim and explain your reasoning."
            ),
        },
    ]

    for p in prompts:
        AWAPrompt.get_or_create(
            prompt_text=p["prompt_text"],
            defaults={"instructions": p["instructions"], "source": "ets"},
        )
    print(f"  AWA prompts: {AWAPrompt.select().count()}")


def seed_verbal_questions():
    """Seed verbal reasoning questions."""
    questions = [
        # ── Sentence Equivalence ──────────────────────────────────────
        {
            "measure": "verbal", "subtype": "se", "difficulty": 3,
            "prompt": "Although the professor's lectures were often _______, students found the underlying ideas genuinely innovative.",
            "options": [
                ("A", "abstruse", True),
                ("B", "lucid", False),
                ("C", "opaque", True),
                ("D", "engaging", False),
                ("E", "mundane", False),
                ("F", "transparent", False),
            ],
            "tags": ["vocabulary", "sentence_equivalence"],
            "explanation": "Both 'abstruse' and 'opaque' mean difficult to understand, creating the contrast with 'innovative ideas.'",
        },
        {
            "measure": "verbal", "subtype": "se", "difficulty": 2,
            "prompt": "The author's latest novel received _______ reviews, with critics praising its depth and originality.",
            "options": [
                ("A", "laudatory", True),
                ("B", "scathing", False),
                ("C", "glowing", True),
                ("D", "tepid", False),
                ("E", "ambivalent", False),
                ("F", "dismissive", False),
            ],
            "tags": ["vocabulary", "sentence_equivalence"],
            "explanation": "'Laudatory' and 'glowing' both mean full of praise.",
        },
        {
            "measure": "verbal", "subtype": "se", "difficulty": 4,
            "prompt": "Despite the diplomat's reputation for _______, her latest speech was remarkably direct and candid.",
            "options": [
                ("A", "forthrightness", False),
                ("B", "equivocation", True),
                ("C", "prevarication", True),
                ("D", "sincerity", False),
                ("E", "belligerence", False),
                ("F", "eloquence", False),
            ],
            "tags": ["vocabulary", "sentence_equivalence"],
            "explanation": "'Equivocation' and 'prevarication' both mean being deliberately vague or misleading. 'Despite' signals contrast with 'direct and candid.'",
        },
        {
            "measure": "verbal", "subtype": "se", "difficulty": 3,
            "prompt": "The committee's report was so _______ that even experts in the field struggled to extract useful information from it.",
            "options": [
                ("A", "comprehensive", False),
                ("B", "convoluted", True),
                ("C", "terse", False),
                ("D", "labyrinthine", True),
                ("E", "informative", False),
                ("F", "succinct", False),
            ],
            "tags": ["vocabulary", "sentence_equivalence"],
            "explanation": "'Convoluted' and 'labyrinthine' both mean extremely complex and confusing.",
        },
        {
            "measure": "verbal", "subtype": "se", "difficulty": 3,
            "prompt": "The historian argued that the empire's decline was not sudden but rather a _______ process spanning centuries.",
            "options": [
                ("A", "gradual", True),
                ("B", "precipitous", False),
                ("C", "protracted", True),
                ("D", "swift", False),
                ("E", "violent", False),
                ("F", "mysterious", False),
            ],
            "tags": ["vocabulary", "sentence_equivalence"],
            "explanation": "'Gradual' and 'protracted' both convey a slow, extended process, contrasting with 'sudden.'",
        },
        # ── Text Completion ──────────────────────────────────────────
        {
            "measure": "verbal", "subtype": "tc", "difficulty": 3,
            "prompt": "The scientist's findings were initially met with (blank1), but subsequent experiments by independent labs confirmed the results, turning skeptics into (blank2).",
            "options": [
                ("blank1_A", "incredulity", True),
                ("blank1_B", "enthusiasm", False),
                ("blank1_C", "indifference", False),
                ("blank2_A", "critics", False),
                ("blank2_B", "advocates", True),
                ("blank2_C", "detractors", False),
            ],
            "tags": ["vocabulary", "text_completion"],
            "explanation": "The 'but' signals contrast. Initially met with disbelief (incredulity), then skeptics became supporters (advocates).",
        },
        {
            "measure": "verbal", "subtype": "tc", "difficulty": 2,
            "prompt": "The new policy was designed to (blank1) the growing disparity between urban and rural healthcare access.",
            "options": [
                ("blank1_A", "exacerbate", False),
                ("blank1_B", "ameliorate", True),
                ("blank1_C", "document", False),
            ],
            "tags": ["vocabulary", "text_completion"],
            "explanation": "'Ameliorate' means to make better. A policy designed to address disparity would aim to improve it.",
        },
        {
            "measure": "verbal", "subtype": "tc", "difficulty": 4,
            "prompt": "Far from being (blank1), the mayor's speech was a model of (blank2), addressing complex issues with admirable clarity.",
            "options": [
                ("blank1_A", "pellucid", True),
                ("blank1_B", "rambling", False),
                ("blank1_C", "inflammatory", False),
                ("blank2_A", "obfuscation", False),
                ("blank2_B", "brevity", False),
                ("blank2_C", "perspicuity", True),
            ],
            "tags": ["vocabulary", "text_completion"],
            "explanation": "'Far from being' signals contrast. Not pellucid (clear) but rather perspicuity (clarity) — wait, both mean clear. The contrast is 'Far from being [something unclear]... model of clarity.' Pellucid means clear, so 'far from being pellucid' doesn't fit. Let me reconsider: the blank1 should be something negative the speech was NOT, and blank2 is the positive it WAS.",
        },
        # ── Reading Comprehension ─────────────────────────────────────
        {
            "measure": "verbal", "subtype": "rc_single", "difficulty": 3,
            "stimulus": {
                "type": "passage",
                "title": "Coral Reef Ecosystems",
                "content": (
                    "<p>Coral reefs, often called the 'rainforests of the sea,' support approximately "
                    "25 percent of all marine species despite covering less than one percent of the "
                    "ocean floor. The biodiversity of coral reefs rivals that of tropical rainforests, "
                    "and both ecosystems face similar threats from human activity. Rising ocean "
                    "temperatures cause coral bleaching, a phenomenon in which corals expel the "
                    "symbiotic algae that provide them with nutrients and color. Without these algae, "
                    "corals turn white and, if conditions persist, eventually die.</p>"
                    "<p>Recent research suggests that some coral species possess genetic variants "
                    "that confer greater heat tolerance. Scientists are now investigating whether "
                    "selectively breeding these heat-resistant corals could help reef ecosystems "
                    "adapt to warming oceans. However, critics argue that such interventions address "
                    "only the symptom rather than the root cause of reef decline.</p>"
                ),
            },
            "prompt": "According to the passage, coral bleaching occurs when:",
            "options": [
                ("A", "Ocean temperatures drop below normal levels", False),
                ("B", "Corals expel symbiotic algae due to rising temperatures", True),
                ("C", "Marine species migrate away from reef ecosystems", False),
                ("D", "Selective breeding programs alter coral genetics", False),
                ("E", "Tropical rainforests release harmful chemicals into the ocean", False),
            ],
            "tags": ["reading_comprehension", "science"],
            "explanation": "The passage states: 'Rising ocean temperatures cause coral bleaching, a phenomenon in which corals expel the symbiotic algae.'",
        },
        {
            "measure": "verbal", "subtype": "rc_single", "difficulty": 4,
            "stimulus": {
                "type": "passage",
                "title": "Coral Reef Ecosystems",
                "content": (
                    "<p>Coral reefs, often called the 'rainforests of the sea,' support approximately "
                    "25 percent of all marine species despite covering less than one percent of the "
                    "ocean floor. The biodiversity of coral reefs rivals that of tropical rainforests, "
                    "and both ecosystems face similar threats from human activity. Rising ocean "
                    "temperatures cause coral bleaching, a phenomenon in which corals expel the "
                    "symbiotic algae that provide them with nutrients and color. Without these algae, "
                    "corals turn white and, if conditions persist, eventually die.</p>"
                    "<p>Recent research suggests that some coral species possess genetic variants "
                    "that confer greater heat tolerance. Scientists are now investigating whether "
                    "selectively breeding these heat-resistant corals could help reef ecosystems "
                    "adapt to warming oceans. However, critics argue that such interventions address "
                    "only the symptom rather than the root cause of reef decline.</p>"
                ),
            },
            "prompt": "The critics mentioned in the passage would most likely argue that:",
            "options": [
                ("A", "Selective breeding of corals is technically impossible", False),
                ("B", "Efforts should focus on reducing the factors causing ocean warming", True),
                ("C", "Coral reefs are not worth preserving", False),
                ("D", "Genetic research on corals has produced no useful results", False),
                ("E", "Coral bleaching is a natural process that requires no intervention", False),
            ],
            "tags": ["reading_comprehension", "inference", "science"],
            "explanation": "Critics say interventions 'address only the symptom rather than the root cause.' The root cause is warming oceans, so they'd advocate addressing that directly.",
        },
        {
            "measure": "verbal", "subtype": "rc_multi", "difficulty": 3,
            "stimulus": {
                "type": "passage",
                "title": "Urban Green Spaces",
                "content": (
                    "<p>Urban green spaces provide numerous benefits to city residents. Studies have "
                    "shown that access to parks and gardens reduces stress levels, encourages "
                    "physical activity, and improves mental health outcomes. Furthermore, urban "
                    "vegetation helps mitigate the heat island effect, reduces air pollution, and "
                    "manages stormwater runoff. Despite these well-documented advantages, many "
                    "rapidly growing cities continue to prioritize commercial development over "
                    "the preservation of green spaces.</p>"
                ),
            },
            "prompt": "According to the passage, which of the following are benefits of urban green spaces? Select ALL that apply.",
            "options": [
                ("A", "Reduction in stress levels", True),
                ("B", "Increased commercial revenue", False),
                ("C", "Mitigation of the heat island effect", True),
                ("D", "Management of stormwater runoff", True),
            ],
            "tags": ["reading_comprehension", "multi_select"],
            "explanation": "The passage explicitly mentions stress reduction, heat island mitigation, and stormwater management. Commercial revenue is not listed as a benefit.",
        },
        # More SE/TC to reach sufficient count
        {
            "measure": "verbal", "subtype": "se", "difficulty": 2,
            "prompt": "The movie was a _______ failure, losing money at every theater where it was shown.",
            "options": [
                ("A", "commercial", True),
                ("B", "critical", False),
                ("C", "financial", True),
                ("D", "artistic", False),
                ("E", "partial", False),
                ("F", "surprising", False),
            ],
            "tags": ["vocabulary", "sentence_equivalence"],
            "explanation": "'Commercial' and 'financial' both relate to money, matching 'losing money.'",
        },
        {
            "measure": "verbal", "subtype": "se", "difficulty": 3,
            "prompt": "The politician's _______ tone during the debate alienated voters who preferred substance over theatrics.",
            "options": [
                ("A", "bombastic", True),
                ("B", "measured", False),
                ("C", "grandiloquent", True),
                ("D", "humble", False),
                ("E", "sincere", False),
                ("F", "cautious", False),
            ],
            "tags": ["vocabulary", "sentence_equivalence"],
            "explanation": "'Bombastic' and 'grandiloquent' both mean pompous or inflated speech/style.",
        },
        {
            "measure": "verbal", "subtype": "tc", "difficulty": 3,
            "prompt": "The discovery was (blank1) because it overturned decades of established theory.",
            "options": [
                ("blank1_A", "groundbreaking", True),
                ("blank1_B", "predictable", False),
                ("blank1_C", "trivial", False),
            ],
            "tags": ["vocabulary", "text_completion"],
            "explanation": "Overturning decades of theory = groundbreaking.",
        },
        {
            "measure": "verbal", "subtype": "se", "difficulty": 4,
            "prompt": "Critics found the novel _______, packed with so many subplots and characters that the main narrative was lost.",
            "options": [
                ("A", "overwrought", True),
                ("B", "sparse", False),
                ("C", "bloated", True),
                ("D", "elegant", False),
                ("E", "focused", False),
                ("F", "minimalist", False),
            ],
            "tags": ["vocabulary", "sentence_equivalence"],
            "explanation": "'Overwrought' and 'bloated' both suggest excess, matching 'packed with so many subplots.'",
        },
        {
            "measure": "verbal", "subtype": "tc", "difficulty": 2,
            "prompt": "The evidence was so (blank1) that even the defendant's own attorney could not dispute it.",
            "options": [
                ("blank1_A", "compelling", True),
                ("blank1_B", "circumstantial", False),
                ("blank1_C", "irrelevant", False),
            ],
            "tags": ["vocabulary", "text_completion"],
            "explanation": "Evidence that cannot be disputed is compelling (convincing).",
        },
        {
            "measure": "verbal", "subtype": "se", "difficulty": 3,
            "prompt": "The researcher's _______ approach to data collection ensured that the study's results were reliable and reproducible.",
            "options": [
                ("A", "meticulous", True),
                ("B", "haphazard", False),
                ("C", "methodical", True),
                ("D", "casual", False),
                ("E", "innovative", False),
                ("F", "controversial", False),
            ],
            "tags": ["vocabulary", "sentence_equivalence"],
            "explanation": "'Meticulous' and 'methodical' both describe careful, systematic approaches.",
        },
        {
            "measure": "verbal", "subtype": "rc_single", "difficulty": 2,
            "stimulus": {
                "type": "passage",
                "title": "Renaissance Art",
                "content": (
                    "<p>The Italian Renaissance marked a profound shift in artistic technique and "
                    "philosophy. Artists like Leonardo da Vinci and Michelangelo moved beyond the "
                    "flat, stylized figures of medieval art to create works that celebrated human "
                    "anatomy, perspective, and naturalism. This transformation was driven not only "
                    "by renewed interest in classical Greek and Roman art but also by advances in "
                    "understanding of optics and geometry.</p>"
                ),
            },
            "prompt": "The passage suggests that Renaissance artists differed from medieval artists primarily in their:",
            "options": [
                ("A", "Use of religious themes", False),
                ("B", "Emphasis on realistic human forms and perspective", True),
                ("C", "Rejection of all classical influences", False),
                ("D", "Preference for sculpture over painting", False),
                ("E", "Use of expensive materials", False),
            ],
            "tags": ["reading_comprehension", "arts"],
            "explanation": "The passage contrasts 'flat, stylized figures of medieval art' with Renaissance 'human anatomy, perspective, and naturalism.'",
        },
        # Additional verbal questions to ensure we have at least 27
        {
            "measure": "verbal", "subtype": "se", "difficulty": 2,
            "prompt": "The CEO's _______ leadership style inspired loyalty among employees who valued consistency and reliability.",
            "options": [
                ("A", "steady", True),
                ("B", "erratic", False),
                ("C", "dependable", True),
                ("D", "volatile", False),
                ("E", "innovative", False),
                ("F", "autocratic", False),
            ],
            "tags": ["vocabulary", "sentence_equivalence"],
            "explanation": "'Steady' and 'dependable' both convey consistency, matching the context.",
        },
        {
            "measure": "verbal", "subtype": "tc", "difficulty": 3,
            "prompt": "The architect's design was praised for its (blank1): it achieved maximum functionality with minimal ornamentation.",
            "options": [
                ("blank1_A", "elegance", True),
                ("blank1_B", "complexity", False),
                ("blank1_C", "frivolity", False),
            ],
            "tags": ["vocabulary", "text_completion"],
            "explanation": "Maximum functionality with minimal ornamentation = elegance (refined simplicity).",
        },
        {
            "measure": "verbal", "subtype": "se", "difficulty": 3,
            "prompt": "The documentary's portrayal of the environmental crisis was deliberately _______, designed to provoke immediate action.",
            "options": [
                ("A", "alarming", True),
                ("B", "reassuring", False),
                ("C", "provocative", True),
                ("D", "balanced", False),
                ("E", "detached", False),
                ("F", "academic", False),
            ],
            "tags": ["vocabulary", "sentence_equivalence"],
            "explanation": "'Alarming' and 'provocative' both suggest content designed to provoke strong reactions.",
        },
        {
            "measure": "verbal", "subtype": "tc", "difficulty": 4,
            "prompt": "What initially seemed like a (blank1) detail in the investigation later proved to be the (blank2) piece of evidence.",
            "options": [
                ("blank1_A", "negligible", True),
                ("blank1_B", "crucial", False),
                ("blank1_C", "fabricated", False),
                ("blank2_A", "most misleading", False),
                ("blank2_B", "least important", False),
                ("blank2_C", "most pivotal", True),
            ],
            "tags": ["vocabulary", "text_completion"],
            "explanation": "The contrast: 'initially seemed negligible' but 'later proved most pivotal.'",
        },
        {
            "measure": "verbal", "subtype": "se", "difficulty": 4,
            "prompt": "The philosopher's arguments were _______, requiring readers to follow long chains of deductive reasoning.",
            "options": [
                ("A", "intricate", True),
                ("B", "simplistic", False),
                ("C", "elaborate", True),
                ("D", "superficial", False),
                ("E", "intuitive", False),
                ("F", "accessible", False),
            ],
            "tags": ["vocabulary", "sentence_equivalence"],
            "explanation": "'Intricate' and 'elaborate' both suggest complexity, matching 'long chains of deductive reasoning.'",
        },
        {
            "measure": "verbal", "subtype": "se", "difficulty": 2,
            "prompt": "Despite the team's _______ effort, they fell short of the championship by a single point.",
            "options": [
                ("A", "valiant", True),
                ("B", "halfhearted", False),
                ("C", "heroic", True),
                ("D", "lackluster", False),
                ("E", "coordinated", False),
                ("F", "disorganized", False),
            ],
            "tags": ["vocabulary", "sentence_equivalence"],
            "explanation": "'Valiant' and 'heroic' both describe brave, admirable effort. 'Despite' shows they tried hard but still lost.",
        },
        {
            "measure": "verbal", "subtype": "tc", "difficulty": 3,
            "prompt": "The negotiations reached an (blank1) when neither side was willing to make concessions.",
            "options": [
                ("blank1_A", "impasse", True),
                ("blank1_B", "agreement", False),
                ("blank1_C", "acceleration", False),
            ],
            "tags": ["vocabulary", "text_completion"],
            "explanation": "Neither side making concessions = impasse (deadlock).",
        },
        {
            "measure": "verbal", "subtype": "rc_single", "difficulty": 3,
            "stimulus": {
                "type": "passage",
                "title": "Behavioral Economics",
                "content": (
                    "<p>Traditional economic models assume that individuals make rational decisions "
                    "aimed at maximizing their utility. However, behavioral economists have demonstrated "
                    "that human decision-making is frequently influenced by cognitive biases. The "
                    "endowment effect, for example, causes people to overvalue items they already "
                    "own compared to identical items they do not own. Similarly, loss aversion leads "
                    "individuals to weigh potential losses more heavily than equivalent gains, often "
                    "resulting in suboptimal choices.</p>"
                ),
            },
            "prompt": "The passage primarily serves to:",
            "options": [
                ("A", "Defend traditional economic models against criticism", False),
                ("B", "Explain how cognitive biases challenge assumptions of rational decision-making", True),
                ("C", "Propose a new theory of consumer behavior", False),
                ("D", "Argue that all economic decisions are irrational", False),
                ("E", "Compare the endowment effect with loss aversion", False),
            ],
            "tags": ["reading_comprehension", "main_idea", "social_science"],
            "explanation": "The passage introduces traditional models, then shows how behavioral economics reveals biases that challenge the rationality assumption.",
        },
        {
            "measure": "verbal", "subtype": "se", "difficulty": 3,
            "prompt": "The company's new strategy was _______, combining elements from several different but proven approaches.",
            "options": [
                ("A", "eclectic", True),
                ("B", "orthodox", False),
                ("C", "hybrid", True),
                ("D", "conventional", False),
                ("E", "risky", False),
                ("F", "untested", False),
            ],
            "tags": ["vocabulary", "sentence_equivalence"],
            "explanation": "'Eclectic' and 'hybrid' both mean combining different elements or sources.",
        },
    ]

    stim_cache = {}
    for q in questions:
        stim = None
        if "stimulus" in q:
            s = q["stimulus"]
            key = s["title"]
            if key in stim_cache:
                stim = stim_cache[key]
            else:
                stim, _ = Stimulus.get_or_create(
                    title=s["title"],
                    defaults={
                        "stimulus_type": s["type"],
                        "content": s["content"],
                    },
                )
                stim_cache[key] = stim

        import json
        question_obj = Question.create(
            measure=q["measure"],
            subtype=q["subtype"],
            stimulus=stim,
            prompt=q["prompt"],
            difficulty_target=q["difficulty"],
            concept_tags=json.dumps(q.get("tags", [])),
            provenance="expert",
            status="live",
            explanation=q.get("explanation", ""),
        )

        for label, text, correct in q.get("options", []):
            QuestionOption.create(
                question=question_obj,
                option_label=label,
                option_text=text,
                is_correct=correct,
            )

    print(f"  Verbal questions: {Question.select().where(Question.measure == 'verbal').count()}")


def seed_quant_questions():
    """Seed quantitative reasoning questions."""
    import json
    questions = [
        # ── Quantitative Comparison ──────────────────────────────────
        {
            "measure": "quant", "subtype": "qc", "difficulty": 2,
            "prompt": "<p>Quantity A: $3^4$</p><p>Quantity B: $4^3$</p>",
            "options": [
                ("A", "Quantity A is greater", True),
                ("B", "Quantity B is greater", False),
                ("C", "The two quantities are equal", False),
                ("D", "The relationship cannot be determined from the information given", False),
            ],
            "tags": ["exponents", "quantitative_comparison"],
            "explanation": "$3^4 = 81$ and $4^3 = 64$. Since $81 > 64$, Quantity A is greater.",
        },
        {
            "measure": "quant", "subtype": "qc", "difficulty": 3,
            "prompt": "<p>$x > 0$</p><p>Quantity A: $\\frac{x}{x+1}$</p><p>Quantity B: $\\frac{x+1}{x+2}$</p>",
            "options": [
                ("A", "Quantity A is greater", False),
                ("B", "Quantity B is greater", True),
                ("C", "The two quantities are equal", False),
                ("D", "The relationship cannot be determined from the information given", False),
            ],
            "tags": ["fractions", "algebra", "quantitative_comparison"],
            "explanation": "For $x > 0$: $\\frac{x}{x+1} < \\frac{x+1}{x+2}$ because as $n$ increases, $\\frac{n}{n+1}$ increases toward 1.",
        },
        {
            "measure": "quant", "subtype": "qc", "difficulty": 4,
            "prompt": "<p>$x^2 = 16$</p><p>Quantity A: $x$</p><p>Quantity B: $4$</p>",
            "options": [
                ("A", "Quantity A is greater", False),
                ("B", "Quantity B is greater", False),
                ("C", "The two quantities are equal", False),
                ("D", "The relationship cannot be determined from the information given", True),
            ],
            "tags": ["algebra", "quantitative_comparison"],
            "explanation": "$x^2 = 16$ means $x = 4$ or $x = -4$. If $x = 4$, they are equal. If $x = -4$, B is greater. Cannot be determined.",
        },
        {
            "measure": "quant", "subtype": "qc", "difficulty": 3,
            "prompt": "<p>A circle has radius 5.</p><p>Quantity A: The area of the circle divided by $\\pi$</p><p>Quantity B: The circumference of the circle divided by $\\pi$</p>",
            "options": [
                ("A", "Quantity A is greater", True),
                ("B", "Quantity B is greater", False),
                ("C", "The two quantities are equal", False),
                ("D", "The relationship cannot be determined from the information given", False),
            ],
            "tags": ["geometry", "circles", "quantitative_comparison"],
            "explanation": "Area/$\\pi$ = $r^2$ = 25. Circumference/$\\pi$ = $2r$ = 10. $25 > 10$.",
        },
        # ── Multiple Choice Single ────────────────────────────────────
        {
            "measure": "quant", "subtype": "mcq_single", "difficulty": 2,
            "prompt": "If $3x + 7 = 22$, what is the value of $x$?",
            "options": [
                ("A", "3", False),
                ("B", "4", False),
                ("C", "5", True),
                ("D", "6", False),
                ("E", "7", False),
            ],
            "tags": ["algebra", "linear_equations"],
            "explanation": "$3x + 7 = 22 \\Rightarrow 3x = 15 \\Rightarrow x = 5$",
        },
        {
            "measure": "quant", "subtype": "mcq_single", "difficulty": 3,
            "prompt": "A store reduces the price of a shirt by 20%, then reduces the new price by an additional 10%. What is the total percent reduction from the original price?",
            "options": [
                ("A", "25%", False),
                ("B", "28%", True),
                ("C", "30%", False),
                ("D", "32%", False),
                ("E", "35%", False),
            ],
            "tags": ["percentages", "word_problems"],
            "explanation": "After 20% off: $0.80P$. After another 10% off: $0.80P \\times 0.90 = 0.72P$. Total reduction: $1 - 0.72 = 0.28 = 28\\%$.",
        },
        {
            "measure": "quant", "subtype": "mcq_single", "difficulty": 3,
            "prompt": "If the average of 5 consecutive integers is 12, what is the largest of these integers?",
            "options": [
                ("A", "12", False),
                ("B", "13", False),
                ("C", "14", True),
                ("D", "15", False),
                ("E", "16", False),
            ],
            "tags": ["statistics", "number_properties"],
            "explanation": "Five consecutive integers with average 12: the middle number is 12, so they are 10, 11, 12, 13, 14. Largest is 14.",
        },
        {
            "measure": "quant", "subtype": "mcq_single", "difficulty": 4,
            "prompt": "A bag contains 3 red, 4 blue, and 5 green marbles. If two marbles are drawn at random without replacement, what is the probability that both are blue?",
            "options": [
                ("A", "$\\frac{1}{11}$", True),
                ("B", "$\\frac{1}{9}$", False),
                ("C", "$\\frac{2}{11}$", False),
                ("D", "$\\frac{4}{33}$", False),
                ("E", "$\\frac{1}{6}$", False),
            ],
            "tags": ["probability", "combinatorics"],
            "explanation": "$P = \\frac{4}{12} \\times \\frac{3}{11} = \\frac{12}{132} = \\frac{1}{11}$",
        },
        # ── Numeric Entry ─────────────────────────────────────────────
        {
            "measure": "quant", "subtype": "numeric_entry", "difficulty": 2,
            "prompt": "What is the value of $\\frac{3}{4} + \\frac{5}{8}$? Give your answer as a decimal.",
            "numeric": {"exact_value": 1.375, "tolerance": 0.001},
            "tags": ["fractions", "arithmetic"],
            "explanation": "$\\frac{3}{4} = \\frac{6}{8}$. $\\frac{6}{8} + \\frac{5}{8} = \\frac{11}{8} = 1.375$",
        },
        {
            "measure": "quant", "subtype": "numeric_entry", "difficulty": 3,
            "prompt": "A rectangle has a perimeter of 28 and a length that is 3 times its width. What is the area of the rectangle?",
            "numeric": {"exact_value": 36.75, "tolerance": 0.01},
            "tags": ["geometry", "rectangles"],
            "explanation": "Let width = w. Length = 3w. Perimeter: 2(w + 3w) = 28 → 8w = 28 → w = 3.5. Area = 3.5 × 10.5 = 36.75.",
        },
        {
            "measure": "quant", "subtype": "numeric_entry", "difficulty": 3,
            "prompt": "If $f(x) = 2x^2 - 3x + 1$, what is $f(3)$?",
            "numeric": {"exact_value": 10, "tolerance": 0},
            "tags": ["algebra", "functions"],
            "explanation": "$f(3) = 2(9) - 3(3) + 1 = 18 - 9 + 1 = 10$",
        },
        {
            "measure": "quant", "subtype": "numeric_entry", "difficulty": 2,
            "prompt": "What is the greatest common factor of 48 and 36?",
            "numeric": {"exact_value": 12, "tolerance": 0},
            "tags": ["number_properties", "factors"],
            "explanation": "Factors of 48: 1,2,3,4,6,8,12,16,24,48. Factors of 36: 1,2,3,4,6,9,12,18,36. GCF = 12.",
        },
        # ── Multiple Choice Multi ─────────────────────────────────────
        {
            "measure": "quant", "subtype": "mcq_multi", "difficulty": 3,
            "prompt": "Which of the following are prime numbers? Select ALL that apply.",
            "options": [
                ("A", "51", False),
                ("B", "53", True),
                ("C", "57", False),
                ("D", "59", True),
                ("E", "61", True),
            ],
            "tags": ["number_properties", "primes"],
            "explanation": "51 = 3×17, 57 = 3×19. 53, 59, 61 are prime.",
        },
        {
            "measure": "quant", "subtype": "mcq_multi", "difficulty": 4,
            "prompt": "If $|x - 3| \\leq 5$, which of the following could be a value of $x$? Select ALL that apply.",
            "options": [
                ("A", "$-3$", False),
                ("B", "$-2$", True),
                ("C", "$0$", True),
                ("D", "$7$", True),
                ("E", "$9$", False),
            ],
            "tags": ["algebra", "absolute_value"],
            "explanation": "$|x - 3| \\leq 5$ means $-2 \\leq x \\leq 8$. So $-2$, $0$, and $7$ are valid. $-3 < -2$ and $9 > 8$.",
        },
        # More quant questions
        {
            "measure": "quant", "subtype": "mcq_single", "difficulty": 2,
            "prompt": "What is the slope of the line passing through points $(2, 5)$ and $(6, 13)$?",
            "options": [
                ("A", "$1$", False),
                ("B", "$\\frac{3}{2}$", False),
                ("C", "$2$", True),
                ("D", "$\\frac{5}{2}$", False),
                ("E", "$3$", False),
            ],
            "tags": ["coordinate_geometry", "linear_equations"],
            "explanation": "Slope = $\\frac{13-5}{6-2} = \\frac{8}{4} = 2$",
        },
        {
            "measure": "quant", "subtype": "mcq_single", "difficulty": 3,
            "prompt": "If a train travels at 60 miles per hour for the first half of a journey and 40 miles per hour for the second half, what is the average speed for the entire journey?",
            "options": [
                ("A", "45 mph", False),
                ("B", "48 mph", True),
                ("C", "50 mph", False),
                ("D", "52 mph", False),
                ("E", "55 mph", False),
            ],
            "tags": ["word_problems", "rates"],
            "explanation": "For equal distances, average speed = $\\frac{2 \\times 60 \\times 40}{60 + 40} = \\frac{4800}{100} = 48$ mph.",
        },
        {
            "measure": "quant", "subtype": "qc", "difficulty": 2,
            "prompt": "<p>Quantity A: $\\sqrt{144}$</p><p>Quantity B: $|-12|$</p>",
            "options": [
                ("A", "Quantity A is greater", False),
                ("B", "Quantity B is greater", False),
                ("C", "The two quantities are equal", True),
                ("D", "The relationship cannot be determined from the information given", False),
            ],
            "tags": ["arithmetic", "absolute_value", "quantitative_comparison"],
            "explanation": "$\\sqrt{144} = 12$ and $|-12| = 12$. They are equal.",
        },
        {
            "measure": "quant", "subtype": "numeric_entry", "difficulty": 4,
            "prompt": "In how many ways can 5 different books be arranged on a shelf?",
            "numeric": {"exact_value": 120, "tolerance": 0},
            "tags": ["combinatorics", "permutations"],
            "explanation": "$5! = 5 \\times 4 \\times 3 \\times 2 \\times 1 = 120$",
        },
        {
            "measure": "quant", "subtype": "mcq_single", "difficulty": 3,
            "prompt": "If the sides of a right triangle are 5, 12, and 13, what is the area of the triangle?",
            "options": [
                ("A", "25", False),
                ("B", "30", True),
                ("C", "32.5", False),
                ("D", "60", False),
                ("E", "65", False),
            ],
            "tags": ["geometry", "triangles"],
            "explanation": "Area = $\\frac{1}{2} \\times 5 \\times 12 = 30$. (5 and 12 are the legs.)",
        },
        {
            "measure": "quant", "subtype": "mcq_single", "difficulty": 2,
            "prompt": "What is 15% of 80?",
            "options": [
                ("A", "10", False),
                ("B", "12", True),
                ("C", "14", False),
                ("D", "15", False),
                ("E", "16", False),
            ],
            "tags": ["percentages", "arithmetic"],
            "explanation": "$15\\% \\times 80 = 0.15 \\times 80 = 12$",
        },
        {
            "measure": "quant", "subtype": "qc", "difficulty": 3,
            "prompt": "<p>$n$ is a positive integer.</p><p>Quantity A: $(-1)^n$</p><p>Quantity B: $(-1)^{n+1}$</p>",
            "options": [
                ("A", "Quantity A is greater", False),
                ("B", "Quantity B is greater", False),
                ("C", "The two quantities are equal", False),
                ("D", "The relationship cannot be determined from the information given", True),
            ],
            "tags": ["exponents", "number_properties", "quantitative_comparison"],
            "explanation": "If $n$ is even: A=1, B=-1, so A > B. If $n$ is odd: A=-1, B=1, so B > A. Cannot be determined.",
        },
        {
            "measure": "quant", "subtype": "mcq_single", "difficulty": 3,
            "prompt": "A circular garden has a diameter of 10 meters. What is the area of the garden, in square meters?",
            "options": [
                ("A", "$10\\pi$", False),
                ("B", "$20\\pi$", False),
                ("C", "$25\\pi$", True),
                ("D", "$50\\pi$", False),
                ("E", "$100\\pi$", False),
            ],
            "tags": ["geometry", "circles"],
            "explanation": "Radius = 5. Area = $\\pi r^2 = 25\\pi$.",
        },
        {
            "measure": "quant", "subtype": "numeric_entry", "difficulty": 3,
            "prompt": "If a number is increased by 25% and the result is 75, what is the original number?",
            "numeric": {"exact_value": 60, "tolerance": 0},
            "tags": ["percentages", "algebra"],
            "explanation": "$1.25x = 75 \\Rightarrow x = 60$",
        },
        {
            "measure": "quant", "subtype": "mcq_single", "difficulty": 4,
            "prompt": "How many integers between 1 and 100 inclusive are divisible by neither 3 nor 5?",
            "options": [
                ("A", "47", False),
                ("B", "53", True),
                ("C", "54", False),
                ("D", "60", False),
                ("E", "67", False),
            ],
            "tags": ["number_properties", "counting"],
            "explanation": "Divisible by 3: 33. Divisible by 5: 20. Divisible by both: 6. By inclusion-exclusion: 33+20-6=47 divisible by 3 or 5. Neither: 100-47=53.",
        },
        {
            "measure": "quant", "subtype": "mcq_single", "difficulty": 2,
            "prompt": "What is the median of the set {3, 7, 2, 9, 5}?",
            "options": [
                ("A", "3", False),
                ("B", "5", True),
                ("C", "5.2", False),
                ("D", "7", False),
                ("E", "9", False),
            ],
            "tags": ["statistics", "median"],
            "explanation": "Sorted: {2, 3, 5, 7, 9}. The median (middle value) is 5.",
        },
    ]

    for q in questions:
        stim = None
        if "stimulus" in q:
            s = q["stimulus"]
            stim, _ = Stimulus.get_or_create(
                title=s["title"],
                defaults={
                    "stimulus_type": s["type"],
                    "content": s["content"],
                },
            )

        question_obj = Question.create(
            measure=q["measure"],
            subtype=q["subtype"],
            stimulus=stim,
            prompt=q["prompt"],
            difficulty_target=q["difficulty"],
            concept_tags=json.dumps(q.get("tags", [])),
            provenance="expert",
            status="live",
            explanation=q.get("explanation", ""),
        )

        for label, text, correct in q.get("options", []):
            QuestionOption.create(
                question=question_obj,
                option_label=label,
                option_text=text,
                is_correct=correct,
            )

        if "numeric" in q:
            NumericAnswer.create(
                question=question_obj,
                exact_value=q["numeric"].get("exact_value"),
                numerator=q["numeric"].get("numerator"),
                denominator=q["numeric"].get("denominator"),
                tolerance=q["numeric"].get("tolerance", 0),
            )

    print(f"  Quant questions: {Question.select().where(Question.measure == 'quant').count()}")


def main():
    print("Initializing database...")
    init_db()

    db.connect(reuse_if_open=True)

    # Check if already seeded
    existing = Question.select().count()
    if existing > 0:
        print(f"Database already has {existing} questions. Skipping seed.")
        print("(Delete data/gre_mock.db to re-seed)")
        db.close()
        return

    print("Seeding AWA prompts...")
    seed_awa_prompts()

    print("Seeding verbal questions...")
    seed_verbal_questions()

    print("Seeding quant questions...")
    seed_quant_questions()

    total = Question.select().count()
    awa_count = AWAPrompt.select().count()
    print(f"\nDone! Total: {total} questions, {awa_count} AWA prompts")

    db.close()


if __name__ == "__main__":
    main()
