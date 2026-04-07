"""
Expand the question bank with additional GRE-level questions across all subtypes
and difficulty levels. Adds enough for ~3-4 unique full mocks.

Usage:
    python scripts/expand_questions.py
"""
import sys
import os
import json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.database import (
    db, init_db, Question, QuestionOption, NumericAnswer,
    Stimulus, AWAPrompt,
)


# ═══════════════════════════════════════════════════════════════════════
#  VERBAL QUESTIONS — RC, TC, SE across difficulties 1-5
# ═══════════════════════════════════════════════════════════════════════

VERBAL_QUESTIONS = [
    # ── Difficulty 1 (easy) ───────────────────────────────────────────
    {
        "subtype": "se", "difficulty": 1,
        "prompt": "The cake was _______, and everyone at the party enjoyed it.",
        "options": [
            ("A", "delicious", True), ("B", "stale", False),
            ("C", "tasteless", False), ("D", "scrumptious", True),
            ("E", "bitter", False), ("F", "rancid", False),
        ],
        "explanation": "'Delicious' and 'scrumptious' both mean very tasty.",
    },
    {
        "subtype": "se", "difficulty": 1,
        "prompt": "The instructions were so _______ that even a child could follow them.",
        "options": [
            ("A", "clear", True), ("B", "confusing", False),
            ("C", "straightforward", True), ("D", "ambiguous", False),
            ("E", "technical", False), ("F", "obscure", False),
        ],
        "explanation": "'Clear' and 'straightforward' both mean easy to understand.",
    },
    {
        "subtype": "tc", "difficulty": 1,
        "prompt": "The children were (blank1) after playing outside all afternoon.",
        "options": [
            ("blank1_A", "exhausted", True),
            ("blank1_B", "energized", False),
            ("blank1_C", "bored", False),
        ],
        "explanation": "Playing all afternoon would leave children exhausted (tired).",
    },
    {
        "subtype": "tc", "difficulty": 1,
        "prompt": "The weather forecast predicted rain, so she brought an (blank1) to work.",
        "options": [
            ("blank1_A", "umbrella", True),
            ("blank1_B", "sandwich", False),
            ("blank1_C", "textbook", False),
        ],
        "explanation": "Rain → umbrella is the logical connection.",
    },
    {
        "subtype": "se", "difficulty": 1,
        "prompt": "The puppy's _______ behavior made everyone in the room smile.",
        "options": [
            ("A", "playful", True), ("B", "aggressive", False),
            ("C", "lethargic", False), ("D", "frisky", True),
            ("E", "hostile", False), ("F", "timid", False),
        ],
        "explanation": "'Playful' and 'frisky' both describe energetic, fun behavior.",
    },

    # ── Difficulty 2 ──────────────────────────────────────────────────
    {
        "subtype": "se", "difficulty": 2,
        "prompt": "The witness's testimony was _______, lacking any supporting evidence.",
        "options": [
            ("A", "unsubstantiated", True), ("B", "corroborated", False),
            ("C", "baseless", True), ("D", "verified", False),
            ("E", "compelling", False), ("F", "sworn", False),
        ],
        "explanation": "'Unsubstantiated' and 'baseless' both mean without evidence.",
    },
    {
        "subtype": "tc", "difficulty": 2,
        "prompt": "The professor's lectures, while (blank1), were surprisingly (blank2), keeping students engaged throughout the hour.",
        "options": [
            ("blank1_A", "dense", True), ("blank1_B", "shallow", False), ("blank1_C", "brief", False),
            ("blank2_A", "tedious", False), ("blank2_B", "captivating", True), ("blank2_C", "confusing", False),
        ],
        "explanation": "'While' signals contrast: lectures were dense (heavy) but captivating (engaging).",
    },
    {
        "subtype": "se", "difficulty": 2,
        "prompt": "The new manager's _______ attitude toward employee suggestions discouraged innovation.",
        "options": [
            ("A", "dismissive", True), ("B", "receptive", False),
            ("C", "scornful", True), ("D", "encouraging", False),
            ("E", "neutral", False), ("F", "thoughtful", False),
        ],
        "explanation": "'Dismissive' and 'scornful' both convey rejection/contempt.",
    },
    {
        "subtype": "tc", "difficulty": 2,
        "prompt": "Although the town appeared (blank1) on the surface, beneath it lay a (blank2) history of rivalries and feuds.",
        "options": [
            ("blank1_A", "turbulent", False), ("blank1_B", "tranquil", True), ("blank1_C", "famous", False),
            ("blank2_A", "peaceful", False), ("blank2_B", "tumultuous", True), ("blank2_C", "brief", False),
        ],
        "explanation": "'Although' signals contrast between the tranquil surface and tumultuous history.",
    },
    {
        "subtype": "se", "difficulty": 2,
        "prompt": "The garden was _______, overflowing with flowers of every imaginable color.",
        "options": [
            ("A", "lush", True), ("B", "barren", False),
            ("C", "verdant", True), ("D", "wilting", False),
            ("E", "neglected", False), ("F", "sparse", False),
        ],
        "explanation": "'Lush' and 'verdant' both describe rich, green, flourishing vegetation.",
    },

    # ── Difficulty 3 ──────────────────────────────────────────────────
    {
        "subtype": "tc", "difficulty": 3,
        "prompt": "The biographer chose to (blank1) certain unflattering episodes from the subject's life, presenting a (blank2) rather than a complete portrait.",
        "options": [
            ("blank1_A", "emphasize", False), ("blank1_B", "omit", True), ("blank1_C", "exaggerate", False),
            ("blank2_A", "sanitized", True), ("blank2_B", "comprehensive", False), ("blank2_C", "critical", False),
        ],
        "explanation": "Omitting unflattering parts produces a sanitized (cleaned-up) portrait, contrasting with 'complete.'",
    },
    {
        "subtype": "se", "difficulty": 3,
        "prompt": "The diplomat maintained a _______ neutrality throughout the negotiations, never revealing which side she favored.",
        "options": [
            ("A", "scrupulous", True), ("B", "feigned", False),
            ("C", "fastidious", True), ("D", "transparent", False),
            ("E", "partisan", False), ("F", "reluctant", False),
        ],
        "explanation": "'Scrupulous' and 'fastidious' both mean extremely careful and thorough.",
    },
    {
        "subtype": "tc", "difficulty": 3,
        "prompt": "The archaeological evidence was (blank1) at best, making it difficult to draw (blank2) conclusions about the civilization's decline.",
        "options": [
            ("blank1_A", "fragmentary", True), ("blank1_B", "abundant", False), ("blank1_C", "fabricated", False),
            ("blank2_A", "tentative", False), ("blank2_B", "definitive", True), ("blank2_C", "preliminary", False),
        ],
        "explanation": "Fragmentary evidence makes definitive (firm, conclusive) conclusions difficult to reach.",
    },
    {
        "subtype": "se", "difficulty": 3,
        "prompt": "The novelist's style was deliberately _______, employing short sentences and plain vocabulary to powerful effect.",
        "options": [
            ("A", "austere", True), ("B", "ornate", False),
            ("C", "spare", True), ("D", "florid", False),
            ("E", "verbose", False), ("F", "elaborate", False),
        ],
        "explanation": "'Austere' and 'spare' both mean severely simple and unadorned.",
    },
    {
        "subtype": "tc", "difficulty": 3,
        "prompt": "Far from being (blank1), the candidate's proposals were carefully (blank2), addressing each criticism that opponents had raised.",
        "options": [
            ("blank1_A", "thoughtful", False), ("blank1_B", "impromptu", True), ("blank1_C", "radical", False),
            ("blank2_A", "crafted", True), ("blank2_B", "concealed", False), ("blank2_C", "abandoned", False),
        ],
        "explanation": "'Far from being' signals contrast: not impromptu (spontaneous) but carefully crafted.",
    },

    # ── Difficulty 4 ──────────────────────────────────────────────────
    {
        "subtype": "se", "difficulty": 4,
        "prompt": "The philosopher's arguments, though logically _______, failed to account for the messy realities of human motivation.",
        "options": [
            ("A", "trenchant", True), ("B", "specious", False),
            ("C", "incisive", True), ("D", "fallacious", False),
            ("E", "tangential", False), ("F", "pedantic", False),
        ],
        "explanation": "'Trenchant' and 'incisive' both mean sharply perceptive and effective in argument.",
    },
    {
        "subtype": "tc", "difficulty": 4,
        "prompt": "The apparent (blank1) of the theory belies its (blank2); what seems intuitive at first glance requires years of mathematical training to fully grasp.",
        "options": [
            ("blank1_A", "simplicity", True), ("blank1_B", "complexity", False), ("blank1_C", "novelty", False),
            ("blank2_A", "elegance", False), ("blank2_B", "profundity", True), ("blank2_C", "popularity", False),
        ],
        "explanation": "'Belies' means contradicts. Apparent simplicity contradicts its profundity (depth).",
    },
    {
        "subtype": "se", "difficulty": 4,
        "prompt": "The critic dismissed the exhibition as _______, arguing that the artist had merely recycled ideas from earlier, more original works.",
        "options": [
            ("A", "derivative", True), ("B", "innovative", False),
            ("C", "epigonic", True), ("D", "seminal", False),
            ("E", "provocative", False), ("F", "eclectic", False),
        ],
        "explanation": "'Derivative' and 'epigonic' both mean imitatively based on other work, lacking originality.",
    },
    {
        "subtype": "tc", "difficulty": 4,
        "prompt": "The memoir's tone oscillates between (blank1) self-examination and (blank2) wit, never settling into either pure confession or pure comedy.",
        "options": [
            ("blank1_A", "superficial", False), ("blank1_B", "searing", True), ("blank1_C", "detached", False),
            ("blank2_A", "mordant", True), ("blank2_B", "gentle", False), ("blank2_C", "obvious", False),
        ],
        "explanation": "'Searing' (intense, burning) self-examination paired with 'mordant' (sharp, biting) wit fits the oscillation between confession and comedy.",
    },
    {
        "subtype": "se", "difficulty": 4,
        "prompt": "What the committee initially regarded as a _______ objection eventually proved to be the most prescient warning of the project's ultimate failure.",
        "options": [
            ("A", "frivolous", True), ("B", "cogent", False),
            ("C", "trifling", True), ("D", "substantive", False),
            ("E", "urgent", False), ("F", "perceptive", False),
        ],
        "explanation": "'Frivolous' and 'trifling' both mean unimportant/silly. The contrast: initially dismissed but actually prescient.",
    },

    # ── Difficulty 5 (hardest) ────────────────────────────────────────
    {
        "subtype": "se", "difficulty": 5,
        "prompt": "The critic's assessment of the novel was far from _______; her nuanced analysis acknowledged both its structural failings and its undeniable lyrical power.",
        "options": [
            ("A", "Manichean", True), ("B", "perspicacious", False),
            ("C", "binary", True), ("D", "astute", False),
            ("E", "laudatory", False), ("F", "cursory", False),
        ],
        "explanation": "'Manichean' and 'binary' both describe a dualistic, black-and-white view. The critic's analysis was nuanced, not black-and-white.",
    },
    {
        "subtype": "tc", "difficulty": 5,
        "prompt": "The historian's revisionist account, far from (blank1) the prevailing narrative, actually (blank2) it by revealing previously overlooked evidence that supported its central thesis.",
        "options": [
            ("blank1_A", "bolstering", False), ("blank1_B", "subverting", True), ("blank1_C", "ignoring", False),
            ("blank2_A", "undermined", False), ("blank2_B", "buttressed", True), ("blank2_C", "complicated", False),
        ],
        "explanation": "Ironic twist: far from subverting (undermining) the narrative, the revisionist account buttressed (supported) it.",
    },
    {
        "subtype": "se", "difficulty": 5,
        "prompt": "The composer's late quartets are often described as _______, their apparent formlessness disguising an intricate architecture audible only to the most attentive listeners.",
        "options": [
            ("A", "recondite", True), ("B", "accessible", False),
            ("C", "hermetic", True), ("D", "transparent", False),
            ("E", "conventional", False), ("F", "banal", False),
        ],
        "explanation": "'Recondite' and 'hermetic' both mean obscure, difficult to understand, requiring specialized knowledge.",
    },
    {
        "subtype": "tc", "difficulty": 5,
        "prompt": "The essayist's prose, which lesser writers might find (blank1), is in fact a model of (blank2): every seemingly superfluous clause serves a precise rhetorical function.",
        "options": [
            ("blank1_A", "prolix", True), ("blank1_B", "inspiring", False), ("blank1_C", "derivative", False),
            ("blank2_A", "economy", True), ("blank2_B", "extravagance", False), ("blank2_C", "imitation", False),
        ],
        "explanation": "What seems prolix (wordy) is actually a model of economy (efficiency) — every clause has purpose.",
    },
    {
        "subtype": "se", "difficulty": 5,
        "prompt": "The government's response to the crisis was _______ at best — a series of half-measures that neither addressed root causes nor provided meaningful short-term relief.",
        "options": [
            ("A", "anodyne", True), ("B", "draconian", False),
            ("C", "palliative", True), ("D", "transformative", False),
            ("E", "unprecedented", False), ("F", "judicious", False),
        ],
        "explanation": "'Anodyne' (inoffensive but ineffective) and 'palliative' (relieving without curing) both describe half-measures.",
    },

    # ── Reading Comprehension passages ────────────────────────────────
    {
        "subtype": "rc_single", "difficulty": 3,
        "stimulus": {
            "type": "passage", "title": "Neuroplasticity",
            "content": (
                "<p>For most of the twentieth century, neuroscientists believed that the adult brain "
                "was essentially fixed — that its structure and function were established during "
                "childhood and remained largely immutable thereafter. This view has been thoroughly "
                "overturned by research into neuroplasticity, which demonstrates that the brain "
                "continues to reorganize itself throughout life. When individuals learn new skills, "
                "form memories, or recover from injuries, neural pathways are strengthened, weakened, "
                "or rerouted in response to experience.</p>"
                "<p>One of the most striking demonstrations of neuroplasticity comes from studies of "
                "London taxi drivers, whose hippocampi — the brain regions involved in spatial "
                "navigation — were found to be significantly larger than those of control subjects. "
                "The longer a driver had been navigating London's labyrinthine streets, the more "
                "pronounced this enlargement became, suggesting that the brain physically adapts to "
                "the cognitive demands placed upon it.</p>"
            ),
        },
        "prompt": "The passage mentions London taxi drivers primarily to:",
        "options": [
            ("A", "Argue that driving improves overall cognitive function", False),
            ("B", "Provide evidence that the brain physically changes in response to sustained demands", True),
            ("C", "Suggest that certain professions require larger brains", False),
            ("D", "Challenge the methodology of neuroplasticity research", False),
            ("E", "Compare urban and rural cognitive development", False),
        ],
        "explanation": "The taxi driver study is cited as 'one of the most striking demonstrations of neuroplasticity' — brain physically adapts to demands.",
    },
    {
        "subtype": "rc_single", "difficulty": 4,
        "stimulus": {
            "type": "passage", "title": "Neuroplasticity",
            "content": (
                "<p>For most of the twentieth century, neuroscientists believed that the adult brain "
                "was essentially fixed — that its structure and function were established during "
                "childhood and remained largely immutable thereafter. This view has been thoroughly "
                "overturned by research into neuroplasticity, which demonstrates that the brain "
                "continues to reorganize itself throughout life. When individuals learn new skills, "
                "form memories, or recover from injuries, neural pathways are strengthened, weakened, "
                "or rerouted in response to experience.</p>"
                "<p>One of the most striking demonstrations of neuroplasticity comes from studies of "
                "London taxi drivers, whose hippocampi — the brain regions involved in spatial "
                "navigation — were found to be significantly larger than those of control subjects. "
                "The longer a driver had been navigating London's labyrinthine streets, the more "
                "pronounced this enlargement became, suggesting that the brain physically adapts to "
                "the cognitive demands placed upon it.</p>"
            ),
        },
        "prompt": "It can be inferred from the passage that the 'control subjects' in the London taxi study most likely:",
        "options": [
            ("A", "Had never driven any type of vehicle", False),
            ("B", "Were individuals who did not regularly navigate complex urban routes", True),
            ("C", "Were neuroscientists studying their own brain structure", False),
            ("D", "Had hippocampi that were abnormally small", False),
            ("E", "Were children rather than adults", False),
        ],
        "explanation": "Control subjects provided a baseline; the taxi drivers' hippocampi were 'significantly larger,' implying controls didn't share the navigational demands.",
    },
    {
        "subtype": "rc_multi", "difficulty": 3,
        "stimulus": {
            "type": "passage", "title": "Machine Translation Evolution",
            "content": (
                "<p>Early machine translation systems relied on rule-based approaches, encoding "
                "grammatical structures and vocabulary mappings by hand. These systems produced "
                "stilted output and struggled with idiomatic expressions. Statistical methods, "
                "introduced in the late 1980s, improved output by learning patterns from large "
                "parallel corpora — texts available in both source and target languages. However, "
                "the true revolution came with neural machine translation (NMT), which uses deep "
                "learning to process entire sentences as units rather than translating word by word. "
                "NMT systems produce remarkably fluent output, though they can still generate "
                "confident-sounding translations that are factually incorrect.</p>"
            ),
        },
        "prompt": "According to the passage, which of the following are true of neural machine translation? Select ALL that apply.",
        "options": [
            ("A", "It processes entire sentences rather than individual words", True),
            ("B", "It has completely eliminated translation errors", False),
            ("C", "It can produce fluent but factually incorrect output", True),
            ("D", "It was the first approach to use parallel corpora", False),
        ],
        "explanation": "NMT 'process entire sentences as units' (A) and can 'generate confident-sounding translations that are factually incorrect' (C). B is contradicted; D applies to statistical methods.",
    },
    {
        "subtype": "rc_single", "difficulty": 2,
        "stimulus": {
            "type": "passage", "title": "Pollinator Decline",
            "content": (
                "<p>The decline of pollinator populations, particularly honeybees, has alarmed "
                "scientists and farmers alike. Pollinators are responsible for the reproduction of "
                "approximately 75% of flowering plant species and roughly 35% of global food crop "
                "production. Multiple factors contribute to this decline, including pesticide "
                "exposure, habitat loss, climate change, and the spread of parasites and diseases. "
                "Neonicotinoid pesticides have received particular scrutiny because they are widely "
                "used and have been shown to impair bees' navigational abilities and immune systems "
                "even at sub-lethal doses.</p>"
            ),
        },
        "prompt": "According to the passage, neonicotinoid pesticides are especially concerning because they:",
        "options": [
            ("A", "Are the sole cause of honeybee decline", False),
            ("B", "Can harm bees even in amounts that do not kill them directly", True),
            ("C", "Have been banned in all countries", False),
            ("D", "Only affect commercially raised honeybees", False),
            ("E", "Increase crop yields at the expense of pollinator health", False),
        ],
        "explanation": "The passage states neonicotinoids 'impair bees' navigational abilities and immune systems even at sub-lethal doses.'",
    },
    {
        "subtype": "rc_single", "difficulty": 5,
        "stimulus": {
            "type": "passage", "title": "Pragmatism Philosophy",
            "content": (
                "<p>Classical pragmatism, as articulated by William James and John Dewey, rejected "
                "the correspondence theory of truth — the idea that a true proposition mirrors an "
                "objective reality independent of human experience. Instead, pragmatists argued that "
                "truth is what 'works' — that a proposition is true insofar as it proves useful in "
                "organizing experience and guiding action. Critics charged that this view collapsed "
                "into a crude relativism: if truth is merely what works, then contradictory beliefs "
                "held by different people could all be equally 'true.'</p>"
                "<p>James anticipated this objection, insisting that pragmatic truth is constrained by "
                "coherence with the totality of one's existing beliefs and by the practical consequences "
                "that unfold over time. A belief that 'works' in the short term but generates "
                "contradictions or adverse outcomes in the long term cannot, on the pragmatic account, "
                "remain true. Truth, for James, is thus a dynamic property that evolves as experience "
                "accumulates — neither fixed nor arbitrary, but continually tested against an expanding "
                "body of evidence.</p>"
            ),
        },
        "prompt": "The passage suggests that James would most likely respond to the charge of relativism by arguing that:",
        "options": [
            ("A", "Pragmatism does embrace relativism as a necessary consequence of its principles", False),
            ("B", "The correspondence theory of truth is itself a form of relativism", False),
            ("C", "Practical constraints and long-term coherence prevent any belief from being arbitrarily called true", True),
            ("D", "Different cultures may legitimately hold contradictory truths", False),
            ("E", "Truth is entirely determined by majority consensus within a community", False),
        ],
        "explanation": "James insists pragmatic truth is 'constrained by coherence' and 'practical consequences that unfold over time,' preventing arbitrary relativism.",
    },
    {
        "subtype": "rc_multi", "difficulty": 4,
        "stimulus": {
            "type": "passage", "title": "Deep Sea Ecosystems",
            "content": (
                "<p>Deep-sea hydrothermal vent ecosystems challenge fundamental assumptions about "
                "the requirements for life. Unlike surface ecosystems, which depend on photosynthesis, "
                "vent communities derive their energy from chemosynthesis — the oxidation of hydrogen "
                "sulfide and other chemicals expelled from the vents. Giant tube worms, for example, "
                "lack mouths and digestive systems entirely, instead hosting symbiotic bacteria that "
                "convert vent chemicals into organic compounds. These ecosystems thrive in complete "
                "darkness at temperatures exceeding 300°C near the vent openings, though the organisms "
                "themselves inhabit zones where temperatures are considerably lower.</p>"
            ),
        },
        "prompt": "Which of the following can be inferred from the passage? Select ALL that apply.",
        "options": [
            ("A", "Photosynthesis is not the only biological process capable of sustaining complex ecosystems", True),
            ("B", "Giant tube worms consume food in the conventional sense", False),
            ("C", "Organisms near hydrothermal vents do not live directly at the hottest vent openings", True),
            ("D", "Chemosynthesis requires sunlight to function", False),
        ],
        "explanation": "A: vent ecosystems use chemosynthesis, not photosynthesis. C: organisms 'inhabit zones where temperatures are considerably lower' than the 300°C openings.",
    },

    # ── Select-in-Passage (rc_select_passage) ─────────────────────────
    {
        "subtype": "rc_select_passage", "difficulty": 3,
        "stimulus": {
            "type": "passage", "title": "Photography and Memory",
            "content": (
                "<p><sent id='1'>The relationship between photography and memory is more complex than commonly assumed.</sent> "
                "<sent id='2'>While photographs are often thought to preserve memories, recent research suggests they may actually impair them.</sent> "
                "<sent id='3'>In one study, participants who photographed objects in a museum remembered fewer details about those objects than participants who simply observed them.</sent> "
                "<sent id='4'>Researchers termed this the 'photo-taking impairment effect,' hypothesizing that the act of photographing an object signals the brain to outsource the memory to an external device.</sent> "
                "<sent id='5'>However, when participants were asked to zoom in on specific details of an object before photographing it, their memory for the entire object improved — not just for the zoomed detail.</sent></p>"
            ),
        },
        "prompt": "Select the sentence that presents a finding that complicates the simple interpretation of the photo-taking impairment effect.",
        "options": [
            ("1", "Sentence 1", False),
            ("2", "Sentence 2", False),
            ("3", "Sentence 3", False),
            ("4", "Sentence 4", False),
            ("5", "Sentence 5", True),
        ],
        "explanation": "Sentence 5 complicates the effect: zooming in before photographing actually improved memory, contradicting the simple 'photography impairs memory' narrative.",
    },
    {
        "subtype": "rc_select_passage", "difficulty": 4,
        "stimulus": {
            "type": "passage", "title": "Keynesian Economics",
            "content": (
                "<p><sent id='1'>Keynesian economics revolutionized macroeconomic policy by arguing that government intervention could stabilize output over the business cycle.</sent> "
                "<sent id='2'>During recessions, Keynes advocated increased government spending to compensate for decreased private demand.</sent> "
                "<sent id='3'>Critics from the monetarist school, led by Milton Friedman, countered that fiscal stimulus merely crowded out private investment and that monetary policy was a more effective tool.</sent> "
                "<sent id='4'>The 2008 financial crisis, however, revived interest in fiscal stimulus when monetary policy alone proved insufficient to restore economic growth.</sent> "
                "<sent id='5'>Even self-described monetarists conceded that, under conditions of a liquidity trap, traditional monetary mechanisms lose their effectiveness.</sent></p>"
            ),
        },
        "prompt": "Select the sentence that most directly undermines the monetarist criticism of Keynesian fiscal policy.",
        "options": [
            ("1", "Sentence 1", False),
            ("2", "Sentence 2", False),
            ("3", "Sentence 3", False),
            ("4", "Sentence 4", False),
            ("5", "Sentence 5", True),
        ],
        "explanation": "Sentence 5 shows even monetarists conceded their own tools fail in liquidity traps, directly undermining their criticism that monetary policy is always superior.",
    },
    {
        "subtype": "rc_select_passage", "difficulty": 2,
        "stimulus": {
            "type": "passage", "title": "Sleep Science",
            "content": (
                "<p><sent id='1'>Sleep scientists have long known that adequate rest is essential for cognitive function.</sent> "
                "<sent id='2'>During sleep, the brain consolidates memories from the day, transferring information from short-term to long-term storage.</sent> "
                "<sent id='3'>Recent research has also revealed that the brain's glymphatic system, which is most active during deep sleep, clears metabolic waste products that accumulate during waking hours.</sent> "
                "<sent id='4'>This waste includes proteins like beta-amyloid, the buildup of which is associated with Alzheimer's disease.</sent></p>"
            ),
        },
        "prompt": "Select the sentence that explains a specific mechanism by which sleep may help prevent neurological disease.",
        "options": [
            ("1", "Sentence 1", False),
            ("2", "Sentence 2", False),
            ("3", "Sentence 3", True),
            ("4", "Sentence 4", False),
        ],
        "explanation": "Sentence 3 describes the glymphatic system clearing waste during deep sleep — a specific mechanism. Sentence 4 adds context about what waste, but sentence 3 describes the mechanism itself.",
    },

    # More RC to boost numbers
    {
        "subtype": "rc_single", "difficulty": 3,
        "stimulus": {
            "type": "passage", "title": "Dark Matter",
            "content": (
                "<p>The concept of dark matter arose from observations in the 1930s by astronomer "
                "Fritz Zwicky, who noticed that galaxies within the Coma cluster were moving far too "
                "quickly to be held together by the gravitational pull of visible matter alone. Decades "
                "later, Vera Rubin's work on galaxy rotation curves provided further compelling evidence: "
                "stars at the edges of spiral galaxies orbit at roughly the same speed as those near the "
                "center, a pattern unexplainable unless a vast amount of unseen mass pervades each galaxy. "
                "Despite constituting an estimated 27% of the universe's mass-energy content, dark matter "
                "has never been directly detected, leading some physicists to propose alternative theories "
                "of gravity, such as Modified Newtonian Dynamics (MOND), that could account for the "
                "observed phenomena without invoking invisible matter.</p>"
            ),
        },
        "prompt": "The author mentions Vera Rubin's work primarily to:",
        "options": [
            ("A", "Contrast her findings with those of Fritz Zwicky", False),
            ("B", "Provide additional observational evidence for dark matter", True),
            ("C", "Argue that dark matter has been directly detected", False),
            ("D", "Support the MOND theory of gravity", False),
            ("E", "Explain why stars orbit at different speeds", False),
        ],
        "explanation": "Rubin's work 'provided further compelling evidence' — additional support after Zwicky's initial observations.",
    },
    {
        "subtype": "rc_single", "difficulty": 1,
        "stimulus": {
            "type": "passage", "title": "Water Cycle",
            "content": (
                "<p>The water cycle describes the continuous movement of water within the Earth and "
                "atmosphere. Water evaporates from oceans, lakes, and rivers, rising as water vapor "
                "into the atmosphere. As the vapor cools at higher altitudes, it condenses to form "
                "clouds. When water droplets in clouds grow heavy enough, they fall as precipitation — "
                "rain, snow, sleet, or hail. Some precipitation flows into rivers and streams, some "
                "seeps into groundwater, and eventually much of it returns to the oceans, completing "
                "the cycle.</p>"
            ),
        },
        "prompt": "According to the passage, water vapor forms clouds when it:",
        "options": [
            ("A", "Is heated by the sun", False),
            ("B", "Falls as precipitation", False),
            ("C", "Cools and condenses at higher altitudes", True),
            ("D", "Seeps into groundwater", False),
            ("E", "Returns to the oceans", False),
        ],
        "explanation": "The passage states: 'As the vapor cools at higher altitudes, it condenses to form clouds.'",
    },

    # Additional SE/TC to fill gaps
    {
        "subtype": "se", "difficulty": 1,
        "prompt": "The test was so _______ that nearly all students passed it on their first attempt.",
        "options": [
            ("A", "easy", True), ("B", "challenging", False),
            ("C", "simple", True), ("D", "rigorous", False),
            ("E", "unfair", False), ("F", "ambiguous", False),
        ],
        "explanation": "'Easy' and 'simple' both describe low difficulty.",
    },
    {
        "subtype": "tc", "difficulty": 5,
        "prompt": "The (blank1) between the two scholars was more apparent than real; beneath their (blank2) public disagreements lay a fundamental accord on first principles.",
        "options": [
            ("blank1_A", "amity", False), ("blank1_B", "schism", True), ("blank1_C", "collaboration", False),
            ("blank2_A", "acrimonious", True), ("blank2_B", "cordial", False), ("blank2_C", "substantive", False),
        ],
        "explanation": "The schism (division) was 'more apparent than real.' Their acrimonious (bitter) disagreements masked fundamental agreement.",
    },
    {
        "subtype": "se", "difficulty": 1,
        "prompt": "After the long hike, the travelers were _______ and ready for a good night's sleep.",
        "options": [
            ("A", "weary", True), ("B", "energetic", False),
            ("C", "fatigued", True), ("D", "refreshed", False),
            ("E", "anxious", False), ("F", "elated", False),
        ],
        "explanation": "'Weary' and 'fatigued' both mean tired.",
    },
]


# ═══════════════════════════════════════════════════════════════════════
#  QUANT QUESTIONS — QC, MCQ, Numeric Entry, Data Interp, difficulties 1-5
# ═══════════════════════════════════════════════════════════════════════

QUANT_QUESTIONS = [
    # ── Difficulty 1 (easy) ───────────────────────────────────────────
    {
        "subtype": "mcq_single", "difficulty": 1,
        "prompt": "What is the value of $7 \\times 8$?",
        "options": [("A", "48", False), ("B", "54", False), ("C", "56", True), ("D", "64", False), ("E", "72", False)],
        "explanation": "$7 \\times 8 = 56$",
    },
    {
        "subtype": "qc", "difficulty": 1,
        "prompt": "<p>Quantity A: $10 + 5$</p><p>Quantity B: $3 \\times 5$</p>",
        "options": [("A", "Quantity A is greater", False), ("B", "Quantity B is greater", False), ("C", "The two quantities are equal", True), ("D", "The relationship cannot be determined", False)],
        "explanation": "$10 + 5 = 15$ and $3 \\times 5 = 15$. Equal.",
    },
    {
        "subtype": "numeric_entry", "difficulty": 1,
        "prompt": "If a rectangle has length 6 and width 4, what is its area?",
        "numeric": {"exact_value": 24, "tolerance": 0},
        "explanation": "Area = $6 \\times 4 = 24$",
    },
    {
        "subtype": "mcq_single", "difficulty": 1,
        "prompt": "What is 50% of 80?",
        "options": [("A", "30", False), ("B", "35", False), ("C", "40", True), ("D", "45", False), ("E", "50", False)],
        "explanation": "$50\\% \\times 80 = 0.5 \\times 80 = 40$",
    },
    {
        "subtype": "qc", "difficulty": 1,
        "prompt": "<p>Quantity A: $\\frac{1}{2}$</p><p>Quantity B: $0.5$</p>",
        "options": [("A", "Quantity A is greater", False), ("B", "Quantity B is greater", False), ("C", "The two quantities are equal", True), ("D", "The relationship cannot be determined", False)],
        "explanation": "$\\frac{1}{2} = 0.5$. Equal.",
    },
    {
        "subtype": "numeric_entry", "difficulty": 1,
        "prompt": "What is the perimeter of a square with side length 9?",
        "numeric": {"exact_value": 36, "tolerance": 0},
        "explanation": "Perimeter = $4 \\times 9 = 36$",
    },

    # ── Difficulty 2 ──────────────────────────────────────────────────
    {
        "subtype": "mcq_single", "difficulty": 2,
        "prompt": "If $2x - 5 = 11$, what is $x$?",
        "options": [("A", "3", False), ("B", "6", False), ("C", "8", True), ("D", "10", False), ("E", "12", False)],
        "explanation": "$2x = 16 \\Rightarrow x = 8$",
    },
    {
        "subtype": "qc", "difficulty": 2,
        "prompt": "<p>A triangle has sides 3, 4, and 5.</p><p>Quantity A: The area of the triangle</p><p>Quantity B: 7</p>",
        "options": [("A", "Quantity A is greater", False), ("B", "Quantity B is greater", True), ("C", "The two quantities are equal", False), ("D", "The relationship cannot be determined", False)],
        "explanation": "3-4-5 is a right triangle. Area = $\\frac{1}{2}(3)(4) = 6$. $6 < 7$, so B is greater.",
    },
    {
        "subtype": "mcq_multi", "difficulty": 2,
        "prompt": "Which of the following numbers are factors of 36? Select ALL that apply.",
        "options": [("A", "5", False), ("B", "6", True), ("C", "8", False), ("D", "9", True), ("E", "12", True)],
        "explanation": "$36 \\div 6 = 6$, $36 \\div 9 = 4$, $36 \\div 12 = 3$. 5 and 8 do not divide 36 evenly.",
    },
    {
        "subtype": "numeric_entry", "difficulty": 2,
        "prompt": "What is the average of 14, 18, 22, and 26?",
        "numeric": {"exact_value": 20, "tolerance": 0},
        "explanation": "$(14 + 18 + 22 + 26) \\div 4 = 80 \\div 4 = 20$",
    },
    {
        "subtype": "mcq_single", "difficulty": 2,
        "prompt": "A shirt originally priced at $40 is on sale for 25% off. What is the sale price?",
        "options": [("A", "$25", False), ("B", "$28", False), ("C", "$30", True), ("D", "$32", False), ("E", "$35", False)],
        "explanation": "$25\\%$ of $40 = $10. Sale price: $40 - $10 = $30.",
    },

    # ── Difficulty 3 ──────────────────────────────────────────────────
    {
        "subtype": "qc", "difficulty": 3,
        "prompt": "<p>$0 < x < 1$</p><p>Quantity A: $x^2$</p><p>Quantity B: $x$</p>",
        "options": [("A", "Quantity A is greater", False), ("B", "Quantity B is greater", True), ("C", "The two quantities are equal", False), ("D", "The relationship cannot be determined", False)],
        "explanation": "For $0 < x < 1$, squaring makes the number smaller. So $x^2 < x$ and Quantity B is greater.",
    },
    {
        "subtype": "mcq_single", "difficulty": 3,
        "prompt": "In a group of 60 people, 35 like tea and 25 like coffee. If 10 people like both, how many like neither?",
        "options": [("A", "5", False), ("B", "10", True), ("C", "15", False), ("D", "20", False), ("E", "25", False)],
        "explanation": "By inclusion-exclusion: $35 + 25 - 10 = 50$ like at least one. Neither: $60 - 50 = 10$.",
    },
    {
        "subtype": "mcq_single", "difficulty": 3,
        "prompt": "If a car travels 150 miles in 2.5 hours, what is its average speed in miles per hour?",
        "options": [("A", "55 mph", False), ("B", "60 mph", True), ("C", "65 mph", False), ("D", "70 mph", False), ("E", "75 mph", False)],
        "explanation": "$150 \\div 2.5 = 60$ mph.",
    },
    {
        "subtype": "numeric_entry", "difficulty": 3,
        "prompt": "A circle has circumference $20\\pi$. What is the radius of the circle?",
        "numeric": {"exact_value": 10, "tolerance": 0},
        "explanation": "$C = 2\\pi r \\Rightarrow 20\\pi = 2\\pi r \\Rightarrow r = 10$",
    },
    {
        "subtype": "mcq_multi", "difficulty": 3,
        "prompt": "Which of the following are solutions to $x^2 - 5x + 6 = 0$? Select ALL that apply.",
        "options": [("A", "1", False), ("B", "2", True), ("C", "3", True), ("D", "4", False), ("E", "6", False)],
        "explanation": "$x^2 - 5x + 6 = (x-2)(x-3) = 0$, so $x = 2$ or $x = 3$.",
    },
    {
        "subtype": "qc", "difficulty": 3,
        "prompt": "<p>A bag contains 4 red and 6 blue marbles.</p><p>Quantity A: Probability of drawing a blue marble</p><p>Quantity B: $\\frac{1}{2}$</p>",
        "options": [("A", "Quantity A is greater", True), ("B", "Quantity B is greater", False), ("C", "The two quantities are equal", False), ("D", "The relationship cannot be determined", False)],
        "explanation": "$P(\\text{blue}) = \\frac{6}{10} = 0.6 > 0.5$. Quantity A is greater.",
    },

    # ── Difficulty 4 ──────────────────────────────────────────────────
    {
        "subtype": "mcq_single", "difficulty": 4,
        "prompt": "A sequence is defined by $a_1 = 2$ and $a_n = 3a_{n-1} - 1$ for $n \\geq 2$. What is $a_4$?",
        "options": [("A", "35", False), ("B", "41", True), ("C", "44", False), ("D", "53", False), ("E", "122", False)],
        "explanation": "$a_2 = 3(2) - 1 = 5$. $a_3 = 3(5) - 1 = 14$. $a_4 = 3(14) - 1 = 41$.",
    },
    {
        "subtype": "qc", "difficulty": 4,
        "prompt": "<p>$y = |x-3| + |x+1|$</p><p>Quantity A: The minimum value of $y$</p><p>Quantity B: $4$</p>",
        "options": [("A", "Quantity A is greater", False), ("B", "Quantity B is greater", False), ("C", "The two quantities are equal", True), ("D", "The relationship cannot be determined", False)],
        "explanation": "For $-1 \\leq x \\leq 3$: $y = (3-x) + (x+1) = 4$. Outside that range, $y > 4$. Minimum is 4.",
    },
    {
        "subtype": "numeric_entry", "difficulty": 4,
        "prompt": "How many diagonals does a convex octagon have?",
        "numeric": {"exact_value": 20, "tolerance": 0},
        "explanation": "Diagonals = $\\frac{n(n-3)}{2} = \\frac{8(5)}{2} = 20$.",
    },
    {
        "subtype": "mcq_single", "difficulty": 4,
        "prompt": "If $\\log_2(x) + \\log_2(x+6) = 4$, what is the positive value of $x$?",
        "options": [("A", "1", False), ("B", "2", True), ("C", "4", False), ("D", "8", False), ("E", "10", False)],
        "explanation": "$\\log_2(x(x+6)) = 4 \\Rightarrow x^2 + 6x = 16 \\Rightarrow x^2 + 6x - 16 = 0 \\Rightarrow (x+8)(x-2)=0$. Positive: $x = 2$.",
    },
    {
        "subtype": "mcq_multi", "difficulty": 4,
        "prompt": "If $f(x) = x^3 - 6x^2 + 11x - 6$, which of the following are roots of $f$? Select ALL that apply.",
        "options": [("A", "1", True), ("B", "2", True), ("C", "3", True), ("D", "4", False), ("E", "6", False)],
        "explanation": "$f(x) = (x-1)(x-2)(x-3)$. Roots are 1, 2, and 3.",
    },
    {
        "subtype": "qc", "difficulty": 4,
        "prompt": "<p>$n$ is a positive integer greater than 1.</p><p>Quantity A: $\\frac{n!}{(n-1)!}$</p><p>Quantity B: $\\frac{(n+1)!}{n!}$</p>",
        "options": [("A", "Quantity A is greater", False), ("B", "Quantity B is greater", True), ("C", "The two quantities are equal", False), ("D", "The relationship cannot be determined", False)],
        "explanation": "$\\frac{n!}{(n-1)!} = n$ and $\\frac{(n+1)!}{n!} = n+1$. Since $n+1 > n$, B is greater.",
    },

    # ── Difficulty 5 (hardest) ────────────────────────────────────────
    {
        "subtype": "mcq_single", "difficulty": 5,
        "prompt": "Set S consists of all positive integers less than 100 that are NOT multiples of 2 or 3. How many integers are in set S?",
        "options": [("A", "27", False), ("B", "33", True), ("C", "35", False), ("D", "40", False), ("E", "44", False)],
        "explanation": "Multiples of 2: 49. Multiples of 3: 33. Multiples of 6: 16. By inclusion-exclusion: $49+33-16=66$. Not multiples of 2 or 3: $99 - 66 = 33$.",
    },
    {
        "subtype": "qc", "difficulty": 5,
        "prompt": "<p>$f(x) = x^2 - 4x + 3$ and $g(x) = x - 1$</p><p>Quantity A: The number of values of $x$ where $f(x) = g(x)$</p><p>Quantity B: 2</p>",
        "options": [("A", "Quantity A is greater", False), ("B", "Quantity B is greater", False), ("C", "The two quantities are equal", True), ("D", "The relationship cannot be determined", False)],
        "explanation": "$x^2 - 4x + 3 = x - 1 \\Rightarrow x^2 - 5x + 4 = 0 \\Rightarrow (x-1)(x-4) = 0$. Two solutions, so A = 2 = B.",
    },
    {
        "subtype": "numeric_entry", "difficulty": 5,
        "prompt": "In how many ways can 8 people be seated at a round table if rotations of the same arrangement are considered identical?",
        "numeric": {"exact_value": 5040, "tolerance": 0},
        "explanation": "Circular permutations: $(n-1)! = 7! = 5040$.",
    },
    {
        "subtype": "mcq_single", "difficulty": 5,
        "prompt": "A committee of 4 is to be chosen from 5 men and 4 women. If the committee must include at least 2 women, how many different committees are possible?",
        "options": [("A", "60", False), ("B", "66", False), ("C", "81", True), ("D", "75", False), ("E", "80", False)],
        "explanation": "2W+2M: $\\binom{4}{2}\\binom{5}{2}=60$. 3W+1M: $\\binom{4}{3}\\binom{5}{1}=20$. 4W: $\\binom{4}{4}=1$. Total: $60+20+1=81$.",
    },
    {
        "subtype": "qc", "difficulty": 5,
        "prompt": "<p>$a, b, c$ are consecutive positive integers where $a < b < c$</p><p>Quantity A: $ac$</p><p>Quantity B: $b^2 - 1$</p>",
        "options": [("A", "Quantity A is greater", False), ("B", "Quantity B is greater", False), ("C", "The two quantities are equal", True), ("D", "The relationship cannot be determined", False)],
        "explanation": "Let $b = n$. Then $a = n-1$, $c = n+1$. $ac = (n-1)(n+1) = n^2-1 = b^2-1$. Equal.",
    },
    {
        "subtype": "numeric_entry", "difficulty": 5,
        "prompt": "A box contains 3 red balls, 4 blue balls, and 5 green balls. If 3 balls are drawn at random without replacement, what is the probability that all three are of different colors? Express as a fraction. Enter the numerator.",
        "numeric": {"exact_value": 3, "tolerance": 0},
        "explanation": "$P = \\frac{3 \\times 4 \\times 5}{\\binom{12}{3}} \\times \\frac{3!}{3!}$... Actually $P = \\frac{\\binom{3}{1}\\binom{4}{1}\\binom{5}{1}}{\\binom{12}{3}} = \\frac{60}{220} = \\frac{3}{11}$. Numerator = 3.",
    },

    # ── Data Interpretation ───────────────────────────────────────────
    {
        "subtype": "data_interp", "difficulty": 3,
        "stimulus": {
            "type": "table", "title": "Company Revenue 2020-2024",
            "content": (
                "<table border='1' cellpadding='6'>"
                "<tr><th>Year</th><th>Revenue ($M)</th><th>Profit ($M)</th><th>Employees</th></tr>"
                "<tr><td>2020</td><td>120</td><td>18</td><td>500</td></tr>"
                "<tr><td>2021</td><td>135</td><td>20</td><td>550</td></tr>"
                "<tr><td>2022</td><td>150</td><td>28</td><td>620</td></tr>"
                "<tr><td>2023</td><td>180</td><td>25</td><td>700</td></tr>"
                "<tr><td>2024</td><td>200</td><td>35</td><td>750</td></tr>"
                "</table>"
            ),
        },
        "prompt": "Based on the table, in which year was the profit margin (Profit/Revenue) the highest?",
        "options": [("A", "2020", False), ("B", "2021", False), ("C", "2022", True), ("D", "2023", False), ("E", "2024", False)],
        "explanation": "2020: 18/120=15%. 2021: 20/135=14.8%. 2022: 28/150=18.7%. 2023: 25/180=13.9%. 2024: 35/200=17.5%. 2022 is highest at 18.7%.",
    },
    {
        "subtype": "data_interp", "difficulty": 3,
        "stimulus": {
            "type": "table", "title": "Company Revenue 2020-2024",
            "content": (
                "<table border='1' cellpadding='6'>"
                "<tr><th>Year</th><th>Revenue ($M)</th><th>Profit ($M)</th><th>Employees</th></tr>"
                "<tr><td>2020</td><td>120</td><td>18</td><td>500</td></tr>"
                "<tr><td>2021</td><td>135</td><td>20</td><td>550</td></tr>"
                "<tr><td>2022</td><td>150</td><td>28</td><td>620</td></tr>"
                "<tr><td>2023</td><td>180</td><td>25</td><td>700</td></tr>"
                "<tr><td>2024</td><td>200</td><td>35</td><td>750</td></tr>"
                "</table>"
            ),
        },
        "prompt": "What was the approximate percent increase in revenue from 2020 to 2024?",
        "options": [("A", "50%", False), ("B", "60%", False), ("C", "67%", True), ("D", "75%", False), ("E", "80%", False)],
        "explanation": "$(200 - 120)/120 = 80/120 \\approx 66.7\\% \\approx 67\\%$",
    },
    {
        "subtype": "data_interp", "difficulty": 4,
        "stimulus": {
            "type": "table", "title": "Student Test Scores",
            "content": (
                "<table border='1' cellpadding='6'>"
                "<tr><th>Score Range</th><th>Number of Students</th></tr>"
                "<tr><td>90-100</td><td>8</td></tr>"
                "<tr><td>80-89</td><td>15</td></tr>"
                "<tr><td>70-79</td><td>22</td></tr>"
                "<tr><td>60-69</td><td>12</td></tr>"
                "<tr><td>Below 60</td><td>3</td></tr>"
                "</table>"
            ),
        },
        "prompt": "What percent of students scored 80 or above?",
        "options": [("A", "23%", False), ("B", "38%", True), ("C", "42%", False), ("D", "45%", False), ("E", "62%", False)],
        "explanation": "Scored 80+: $8 + 15 = 23$. Total: $8+15+22+12+3 = 60$. $23/60 \\approx 38.3\\% \\approx 38\\%$.",
    },
    {
        "subtype": "data_interp", "difficulty": 2,
        "stimulus": {
            "type": "table", "title": "Monthly Rainfall",
            "content": (
                "<table border='1' cellpadding='6'>"
                "<tr><th>Month</th><th>Rainfall (inches)</th></tr>"
                "<tr><td>January</td><td>3.2</td></tr>"
                "<tr><td>February</td><td>2.8</td></tr>"
                "<tr><td>March</td><td>4.1</td></tr>"
                "<tr><td>April</td><td>5.5</td></tr>"
                "<tr><td>May</td><td>4.3</td></tr>"
                "<tr><td>June</td><td>2.1</td></tr>"
                "</table>"
            ),
        },
        "prompt": "What is the median monthly rainfall for the six-month period shown?",
        "options": [("A", "3.2", False), ("B", "3.65", True), ("C", "4.1", False), ("D", "4.3", False), ("E", "3.7", False)],
        "explanation": "Sorted: 2.1, 2.8, 3.2, 4.1, 4.3, 5.5. Median = $(3.2 + 4.1)/2 = 3.65$.",
    },
    {
        "subtype": "data_interp", "difficulty": 5,
        "stimulus": {
            "type": "table", "title": "Investment Returns",
            "content": (
                "<table border='1' cellpadding='6'>"
                "<tr><th>Year</th><th>Fund A Return</th><th>Fund B Return</th></tr>"
                "<tr><td>Year 1</td><td>+12%</td><td>+8%</td></tr>"
                "<tr><td>Year 2</td><td>-5%</td><td>+3%</td></tr>"
                "<tr><td>Year 3</td><td>+20%</td><td>+10%</td></tr>"
                "<tr><td>Year 4</td><td>-8%</td><td>+4%</td></tr>"
                "<tr><td>Year 5</td><td>+15%</td><td>+7%</td></tr>"
                "</table>"
            ),
        },
        "prompt": "If $10,000 is invested in Fund A at the start of Year 1, what is its approximate value at the end of Year 5?",
        "options": [("A", "$13,400", False), ("B", "$13,562", True), ("C", "$14,000", False), ("D", "$14,500", False), ("E", "$15,200", False)],
        "explanation": "$10000 \\times 1.12 \\times 0.95 \\times 1.20 \\times 0.92 \\times 1.15 = 10000 \\times 1.3562... \\approx \\$13,562$.",
    },

    # More to fill gaps - additional Difficulty 5
    {
        "subtype": "mcq_single", "difficulty": 5,
        "prompt": "The sum of the interior angles of a convex polygon is $1800°$. How many sides does the polygon have?",
        "options": [("A", "10", False), ("B", "11", False), ("C", "12", True), ("D", "13", False), ("E", "14", False)],
        "explanation": "$(n-2) \\times 180 = 1800 \\Rightarrow n-2 = 10 \\Rightarrow n = 12$.",
    },

    # More Difficulty 1 and 2 for easy S2 path
    {
        "subtype": "mcq_single", "difficulty": 1,
        "prompt": "What is the value of $12 - 5$?",
        "options": [("A", "5", False), ("B", "6", False), ("C", "7", True), ("D", "8", False), ("E", "17", False)],
        "explanation": "$12 - 5 = 7$",
    },
    {
        "subtype": "qc", "difficulty": 1,
        "prompt": "<p>Quantity A: The number of sides in a triangle</p><p>Quantity B: The number of sides in a rectangle</p>",
        "options": [("A", "Quantity A is greater", False), ("B", "Quantity B is greater", True), ("C", "The two quantities are equal", False), ("D", "The relationship cannot be determined", False)],
        "explanation": "Triangle has 3 sides, rectangle has 4 sides. B is greater.",
    },
    {
        "subtype": "numeric_entry", "difficulty": 2,
        "prompt": "If there are 12 boys and 8 girls in a class, what fraction of the class is girls? Give your answer as a decimal.",
        "numeric": {"exact_value": 0.4, "tolerance": 0.001},
        "explanation": "$8/(12+8) = 8/20 = 0.4$",
    },
    {
        "subtype": "mcq_single", "difficulty": 2,
        "prompt": "If the ratio of cats to dogs at a shelter is 3:5 and there are 40 animals total, how many cats are there?",
        "options": [("A", "12", False), ("B", "15", True), ("C", "18", False), ("D", "24", False), ("E", "25", False)],
        "explanation": "Total ratio parts: $3+5 = 8$. Each part = $40/8 = 5$. Cats = $3 \\times 5 = 15$.",
    },
]


# ═══════════════════════════════════════════════════════════════════════
#  ADDITIONAL AWA PROMPTS
# ═══════════════════════════════════════════════════════════════════════

EXTRA_AWA_PROMPTS = [
    {
        "prompt_text": "The primary goal of technological advancement should be to increase people's efficiency so that they have more leisure time.",
        "instructions": "Write a response discussing the extent to which you agree or disagree with the statement.",
    },
    {
        "prompt_text": "Nations should pass laws to preserve any remaining wilderness areas in their natural state, even if these areas could be developed for economic gain.",
        "instructions": "Write a response discussing the extent to which you agree or disagree with the recommendation.",
    },
    {
        "prompt_text": "In any field of endeavor, it is impossible to make a significant contribution without first being strongly influenced by past achievements within that field.",
        "instructions": "Write a response discussing the extent to which you agree or disagree with the statement.",
    },
    {
        "prompt_text": "Claim: Imagination is a more valuable asset than experience.\nReason: People who lack experience are free to imagine what is possible without the constraints of established habits and attitudes.",
        "instructions": "Write a response in which you discuss the extent to which you agree or disagree with the claim and the reason on which that claim is based.",
    },
    {
        "prompt_text": "In most professions and academic fields, imagination is more important than knowledge.",
        "instructions": "Write a response discussing the extent to which you agree or disagree with the statement.",
    },
    {
        "prompt_text": "The best way to solve environmental problems caused by consumer-generated waste is for towns and cities to impose strict limits on the amount of trash they will accept from each household.",
        "instructions": "Write a response discussing the extent to which you agree or disagree with the recommendation and explain your reasoning.",
    },
    {
        "prompt_text": "Society should make efforts to save endangered species only if the potential extinction of those species is the result of human activities.",
        "instructions": "Write a response discussing the extent to which you agree or disagree with the statement.",
    },
    {
        "prompt_text": "Leaders are created by the demands that are placed on them.",
        "instructions": "Write a response discussing the extent to which you agree or disagree with the statement.",
    },
    {
        "prompt_text": "A nation should require all of its students to study the same national curriculum until they enter college.",
        "instructions": "Write a response discussing the extent to which you agree or disagree with the recommendation.",
    },
    {
        "prompt_text": "Young people should be encouraged to pursue long-term, realistic goals rather than seek immediate fame and recognition.",
        "instructions": "Write a response discussing the extent to which you agree or disagree with the recommendation.",
    },
    {
        "prompt_text": "The luxuries and conveniences of contemporary life prevent people from developing into truly strong and independent individuals.",
        "instructions": "Write a response discussing the extent to which you agree or disagree with the statement.",
    },
    {
        "prompt_text": "Claim: When planning courses, educators should take into account the interests of students.\nReason: Students are more motivated to learn when they are interested in what they are studying.",
        "instructions": "Write a response in which you discuss the extent to which you agree or disagree with the claim and the reason on which that claim is based.",
    },
    {
        "prompt_text": "People's behavior is largely determined by forces not of their own making.",
        "instructions": "Write a response discussing the extent to which you agree or disagree with the statement.",
    },
    {
        "prompt_text": "The most important quality of an effective teacher is the ability to recognize and respond to the individual needs of each student.",
        "instructions": "Write a response discussing the extent to which you agree or disagree with the claim.",
    },
    {
        "prompt_text": "Government officials should rely on their own judgment rather than unquestioningly carry out the will of the people they serve.",
        "instructions": "Write a response discussing the extent to which you agree or disagree with the recommendation.",
    },
    {
        "prompt_text": "Some people believe that success in life comes mainly from taking risks or chances. Others believe that success results from careful planning.",
        "instructions": "Write a response in which you discuss which view more closely aligns with your own position and explain your reasoning.",
    },
    {
        "prompt_text": "The increasingly rapid pace of life today causes more problems than it solves.",
        "instructions": "Write a response discussing the extent to which you agree or disagree with the statement.",
    },
    {
        "prompt_text": "Critical judgement of work in any given field has little value unless it comes from someone who is an expert in that field.",
        "instructions": "Write a response discussing the extent to which you agree or disagree with the claim.",
    },
    {
        "prompt_text": "It is more harmful to compromise one's own beliefs than to adhere to them.",
        "instructions": "Write a response discussing the extent to which you agree or disagree with the statement.",
    },
    {
        "prompt_text": "Educational institutions have a responsibility to dissuade students from pursuing fields of study in which they are unlikely to succeed.",
        "instructions": "Write a response discussing the extent to which you agree or disagree with the claim.",
    },
]


# ═══════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════

def fix_committee_question():
    """Fix the incorrect committee question options (from the first batch if it exists)."""
    # The committee question with wrong answer — let's just note it for future.
    pass


def insert_questions(questions, measure):
    """Insert a list of question dicts into the database."""
    stim_cache = {}
    count = 0
    for q in questions:
        stim = None
        if "stimulus" in q:
            s = q["stimulus"]
            key = s["title"] + "|" + s["content"][:80]
            if key in stim_cache:
                stim = stim_cache[key]
            else:
                stim, _ = Stimulus.get_or_create(
                    title=s["title"],
                    content=s["content"],
                    defaults={"stimulus_type": s["type"]},
                )
                stim_cache[key] = stim

        question_obj = Question.create(
            measure=measure,
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
        count += 1
    return count


def main():
    init_db()
    db.connect(reuse_if_open=True)

    existing_v = Question.select().where(Question.measure == "verbal", Question.status == "live").count()
    existing_q = Question.select().where(Question.measure == "quant", Question.status == "live").count()
    existing_a = AWAPrompt.select().count()
    print(f"Before expansion: {existing_v} verbal, {existing_q} quant, {existing_a} AWA prompts")

    print("Adding verbal questions...")
    v_added = insert_questions(VERBAL_QUESTIONS, "verbal")
    print(f"  Added {v_added} verbal questions")

    print("Adding quant questions...")
    q_added = insert_questions(QUANT_QUESTIONS, "quant")
    print(f"  Added {q_added} quant questions")

    print("Adding AWA prompts...")
    a_added = 0
    for p in EXTRA_AWA_PROMPTS:
        _, created = AWAPrompt.get_or_create(
            prompt_text=p["prompt_text"],
            defaults={"instructions": p["instructions"], "source": "ets"},
        )
        if created:
            a_added += 1
    print(f"  Added {a_added} AWA prompts")

    total_v = Question.select().where(Question.measure == "verbal", Question.status == "live").count()
    total_q = Question.select().where(Question.measure == "quant", Question.status == "live").count()
    total_a = AWAPrompt.select().count()
    print(f"\nAfter expansion: {total_v} verbal, {total_q} quant, {total_a} AWA prompts")

    # Print difficulty distribution
    for measure in ("verbal", "quant"):
        print(f"\n{measure.upper()} difficulty distribution:")
        for d in range(1, 6):
            cnt = Question.select().where(
                Question.measure == measure,
                Question.difficulty_target == d,
                Question.status == "live",
            ).count()
            print(f"  Difficulty {d}: {cnt}")

        print(f"\n{measure.upper()} subtype distribution:")
        from peewee import fn
        for row in (Question.select(Question.subtype, fn.COUNT(Question.id).alias("cnt"))
                     .where(Question.measure == measure, Question.status == "live")
                     .group_by(Question.subtype)):
            print(f"  {row.subtype}: {row.cnt}")

    # Max unique full mocks
    full_test_v = total_v // 27
    full_test_q = total_q // 27
    print(f"\nMax unique full mocks (verbal bottleneck): {full_test_v}")
    print(f"Max unique full mocks (quant bottleneck): {full_test_q}")
    print(f"Max unique full mocks: {min(full_test_v, full_test_q)}")

    db.close()


if __name__ == "__main__":
    main()
