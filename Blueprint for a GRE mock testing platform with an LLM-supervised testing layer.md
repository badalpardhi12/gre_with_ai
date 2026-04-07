# Build Blueprint for a GRE Mock Testing Platform with an LLM-Supervised Testing Layer

## Exam reality and fidelity targets

### What the current GRE actually is

The current GRE General Test (effective September 22, 2023) is a computer-delivered exam lasting about **1 hour 58 minutes** with **five sections** and **one Analytical Writing essay (“Analyze an Issue”)**. citeturn3view0turn4view0

| Measure | Sections | Questions | Time |
|---|---:|---:|---:|
| Analytical Writing | 1 | 1 Issue task | 30 minutes citeturn3view0turn4view0 |
| Verbal Reasoning | 2 | Section 1: 12; Section 2: 15 | 18 min; 23 min citeturn3view0turn4view0 |
| Quantitative Reasoning | 2 | Section 1: 12; Section 2: 15 | 21 min; 26 min citeturn3view0turn4view0 |

Order constraints: **Analytical Writing is always first**; Verbal and Quant can appear in **either order** after that. citeturn3view0

Break behavior: ETS states the shorter test **eliminated the scheduled break**. At test centers, users *may* step away, but the clock does not stop unless they have approved accommodations; **optional breaks are not allowed in the at-home test**. citeturn4view0turn4view1

Adaptive behavior: Verbal and Quant are **section-level adaptive**: the **first section** of each measure is “average difficulty,” and the **difficulty of the second section** depends on performance in the first. citeturn3view0turn4view0turn11search27

Navigation behavior: A defining UX feature is that the test allows moving forward/backward **within a section** (not across sections), with **preview/review**, **Mark/Review**, and the ability to change answers within the current section. citeturn3view0turn2search8

Quant tooling: There is a **basic on-screen calculator** for Quant. citeturn3view0turn1search1turn11search11

### Question archetypes a faithful platform must represent

**Verbal** has three core question families: Reading Comprehension (RC), Text Completion (TC), Sentence Equivalence (SE). ETS notes roughly **half the Verbal measure** involves reading passages and answering questions, and the remaining half involves completing sentences/paragraphs. citeturn14search2turn6view1

For RC, ETS materials describe three on-screen formats:  
- Multiple-choice: select **one** answer  
- Multiple-choice: select **one or more** answers  
- **Select-in-passage** (select a sentence from the passage) citeturn6view1turn14search9  

Select-in-passage is explicitly a computer-dependent type that does **not** appear in paper-delivered forms (replaced by equivalent multiple-choice). citeturn9search2

ETS’s Diagnostic Service describes RC passages as typically **~10 passages**, mostly one paragraph, with one or two longer passages, drawn from multiple disciplines and topic types. citeturn14search4  
This matters for realistic passage pacing, UI scrolling behavior, and test “feel.”

**Quant** has four canonical question types:  
- Quantitative Comparison (QC)  
- Multiple-choice (select one)  
- Multiple-choice (select one or more)  
- Numeric Entry citeturn11search11turn6view0  

Quant questions can appear as discrete items or in **Data Interpretation sets** based on a shared table/graph. citeturn6view0turn6view1

Numeric Entry has exam-specific input rules: answer may be entered as an integer/decimal in one box, or as a fraction with numerator/denominator boxes; equivalent forms (e.g., 2.5 vs 2.50) are accepted; fractions need not be reduced unless required to fit; exact answers are expected unless rounding is requested. citeturn17view0turn6view0

**Quant content scope**: ETS frames Quant as high-school math/statistics (generally no higher than a second algebra course), **excluding trigonometry, calculus, and inferential statistics**, and organized around arithmetic, algebra, geometry, and data analysis. citeturn14search1turn14search0turn14search7

**Analytical Writing (AWA)**: The current GRE has a single **30-minute “Analyze an Issue” task**. citeturn3view0turn15search15  
ETS’s published guidance emphasizes: respond to the specific instructions, consider complexity, organize/develop ideas, support with reasons/examples, and control standard written English. citeturn15search2turn15search5  
A response to a different issue gets a score of **0** (off-topic). citeturn15search2turn0search5

### What “true representation of the GRE” means operationally

To be “exam-faithful” you need explicit fidelity targets, testable in QA and measurable in telemetry. Below is a practical definition aligned to ETS-published test design features and measured behaviors.

**Timing fidelity**
- Section timers must match current allocations exactly (18/23, 21/26, 30). citeturn3view0  
- No scheduled break; model “optional break” behavior separately for (a) at-home (blocked) and (b) test-center simulation (allowed but timer continues). citeturn4view0turn4view1  
- Logging must support ETS-like diagnostics: question time spent and difficulty level tracking (ETS Diagnostic Service reports time spent per question and difficulty levels 1–5). citeturn11search9turn11search24

**UI fidelity**
- Must implement “within-section navigation + Mark/Review + review list” behaviors. citeturn3view0turn2search8  
- Must implement computer-specific interactions: Select-in-passage selection behavior; Numeric Entry input structure (one box vs fraction boxes); multi-select with “no partial credit unless all correct choices and no others.” citeturn9search2turn17view0turn6view0  
- Full-screen, single-monitor, minimum resolution guidance matters for perceived realism; ETS’s official practice tests recommend full screen at 100% zoom and specify minimum 1280×1024 and single monitor for best simulation. citeturn16view0

**Question-distribution fidelity**
- You cannot exactly replicate ETS’s proprietary assembly blueprint, but you can approximate observable constraints: ~10 passages, mostly short, mix of formats (single, multi, select-in-passage), plus non-passage verbal items. citeturn14search4turn14search2turn14search9  
- For Quant: include both discrete and Data Interpretation sets; QC must use the fixed A/B/C/D comparison answers. citeturn6view0turn17view0  
- Your distribution assumptions should be treated as **calibrated parameters**, not hardcoded “truth,” unless ETS has published the statistic.

**Difficulty fidelity**
- “GRE-like” difficulty is not only content difficulty; it’s the *joint distribution* of: trap design, plausible distractors, time-to-solve, language density, and multi-step reasoning. ETS emphasizes scores depend on the difficulty level of the section plus total correct. citeturn3view0turn11search27  
- Practically: you must run psychometric calibration (see later) rather than trusting author intuition.

**Scoring fidelity**
- Official scaled score mechanics are not fully public, but ETS does confirm: raw score is number correct; within each section, all questions contribute equally; scoring considers difficulty level of sections; Verbal/Quant are section-level adaptive. citeturn11search27turn11search0turn3view0  
- Your platform must therefore:  
  - compute raw scores deterministically  
  - estimate scaled scores via a transparent approximation model  
  - present score uncertainty explicitly (range / confidence) rather than claiming “official” equivalence

**Progression/adaptivity fidelity**
- You must implement a 2-stage-per-measure design (routing after stage 1), which aligns to **multi-stage testing (MST)** patterns (adapt after a module/testlet rather than every item). MST is a recognized adaptive approach with “modules/stages/panels/pathways.” citeturn8search20turn8search28  
- Even though ETS doesn’t label GRE as “MST” in the cited statements, the described behavior (choose the second section as a unit) is consistent with MST-like adaptation. citeturn3view0turn11search27

### Known realism gaps between “official-like” and typical third-party experiences

Evidence strength varies by claim; the platform blueprint should treat these as risks to manage.

- **Format drift risk**: The GRE changed materially in 2023 (shorter test; removed unscored section and scheduled break; removed Analyze an Argument task). A platform that is not aggressively versioned by exam date will become unrealistic. citeturn4view0turn4view1  
- **Interface drift risk**: ETS positions POWERPREP as simulating the actual test and highlights the exact test design features (mark/review, within-section navigation, on-screen calculator). Practice test system requirements are also specified for best simulation (full-screen, resolution, single monitor). citeturn3view0turn9search10turn16view0  
  Inference: products that don’t reproduce these behaviors create different pacing and review strategies than test day. (This is an inference from the fact that these behaviors are part of the official design.) citeturn3view0turn2search8  
- **Scoring mismatch risk**: ETS confirms scoring depends on difficulty of sections and total correct, but does not publish the exact conversion; third-party “score calculators” are necessarily approximations. citeturn11search27turn3view0  

image_group{"layout":"carousel","aspect_ratio":"16:9","query":["ETS POWERPREP practice test interface review screen","GRE on-screen calculator ETS screenshot","GRE select-in-passage question screenshot","GRE quantitative comparison question screen"]}

## Question bank, psychometrics, and scoring

### First-principles requirement: item bank quality determines product quality

A GRE platform is fundamentally an **assessment system**, not a content player. A high-stakes-feeling mock requires:
- controlled content sampling (coverage and balance)
- validity evidence (items represent intended constructs)
- reliability (scores stable enough for decision-making)
- fairness (bias review, accessibility, accommodations)

The open-access *Standards for Educational and Psychological Testing* (AERA/APA/NCME) provide professional guidance across validity, reliability, fairness, test administration, scoring, and documentation. citeturn8search2turn8search34  
entity["organization","American Educational Research Association","testing standards coauthor"] entity["organization","American Psychological Association","testing standards coauthor"] entity["organization","National Council on Measurement in Education","testing standards coauthor"]

For constructed-response scoring (relevant to AWA), ETS itself publishes guidelines connected to quality/fairness for performance assessments. citeturn11search2

### Question sourcing options and a legally safe strategy

You asked to evaluate four approaches. A commercially viable solution is a **hybrid pipeline** with strict provenance tracking.

**Licensed/official-style sources**
- Pros: highest realism; potential calibrated difficulty.
- Cons: expensive; complex licensing; limits differentiation.

Evidence you should verify:
- ETS’s policies emphasize you must obtain permission to reproduce ETS copyrighted material, including test questions. citeturn9search12turn9search13  
- ETS trademark guidance also restricts use of ETS trademarks in product names/domains and requires proper informational use with disclaimers. citeturn9search0turn9search1

**Manually authored bank by experts**
- Pros: maximal control; defensible quality; can align to psychometric targets.
- Cons: high ongoing cost; requires workflow discipline.

**LLM-assisted question generation (with human review)**
- Pros: faster drafting; lower marginal cost per item.
- Cons: risk of low-quality items, subtle ambiguity, wrong difficulty, and style drift; research in other domains repeatedly finds **human review remains essential** for quality and relevance. citeturn10search0turn10search24  

**Hybrid (recommended)**
- Expert-authored “gold” items + LLM-assisted drafting + multi-layer QA + psychometric calibration post-launch.

This recommendation is supported by (a) item-writing guidance emphasizing plausibility/one-correct-answer/distractor rationales and (b) AI item-generation studies showing promise but requiring human review and psychometric checks. citeturn10search7turn10search15turn10search0turn10search20

### Item-writing and review workflow blueprint

A reliable workflow borrows from established item-writing and testing standards, then adds AI-era controls.

**Blueprinting**
- Start from ETS-published construct statements: Quant covers arithmetic/algebra/geometry/data analysis; Verbal focuses on reading and sentence completion; AWA measures critical thinking and analytical writing. citeturn14search7turn14search1turn15search5
- Convert into an internal “test blueprint matrix”:
  - measure → question family → subtype → concept tags → target difficulty band(s) → time target

**Drafting**
- Items written by humans or drafted by LLM but always in a structured authoring format that forces:
  - single unambiguous correct answer condition
  - distractor rationales (what misconception each distractor represents)
  - numeric entry acceptable answer forms (where applicable)

Item-writing research-based guidelines emphasize: direct stems, one correct answer, plausible distractors, avoid “all/none of the above,” and provide rationales for distractors. citeturn10search7turn10search23turn10search15

**Technical review (deterministic)**
- Answer key validation:
  - Quant: symbolic/constraint solver checks where feasible; numeric entry equivalence checking rules aligned to ETS guidance (equivalent decimals accepted; fractions need not be reduced). citeturn17view0
  - Verbal: enforce format rules (SE must have exactly two correct choices; multi-select must define complete correct set; no partial credit) per ETS sample guidance. citeturn6view0turn14search9turn9search2
- Ambiguity detection:
  - run multi-solver agreement checks (human + LLM + rule-based) and flag disagreements for human adjudication

**Content review (human)**
- GRE-style checks (non-negotiable):
  - vocabulary level appropriateness (TC/SE)
  - plausibility of distractors
  - RC passage tone and academic-ish “periodical/book” feel (without copying)
  - trap pattern realism

**Bias & sensitivity review**
- Apply a documented checklist aligned to testing standards on fairness and avoiding construct-irrelevant variance. citeturn8search2turn11search2

**Editorial review**
- consistent punctuation, layout, answer choice parallelism, symbol formatting
- for multi-select: clarity that “select all that apply” is intended, matching ETS wording. citeturn6view0turn14search9

**Pretest calibration**
- Items initially deployed as “beta” (in drills) or included as *non-scored research* in platform-only modes (not in “official simulation mode”), then calibrated via response data.

### Required question metadata schema

Minimum metadata for a GRE-scale platform (including what you listed) should be mandatory fields, not optional tags:

- **Identity & provenance**
  - question_id (stable)
  - version_id (semantic version)
  - author_id, reviewer_id(s)
  - provenance_type (expert, LLM-assisted, licensed, imported)
  - licensing constraints (if any)
  - status (draft → internal QA → pilot → live → retired)

- **Assessment blueprint**
  - measure (Verbal/Quant/AWA)
  - family/subtype (e.g., RC-select-in-passage; Quant-QC; Numeric Entry)
  - concept tags (hierarchical)
  - stimulus linkage (passage_id / dataset_id)
  - answer format + validation spec (see below)

- **Difficulty & timing**
  - target difficulty band (initial)
  - target time-to-solve distribution (e.g., P50/P75)
  - cognitive-step count (optional internal)

- **Answering model**
  - correct answer(s) canonical representation
  - accepted equivalents (numeric entry)
  - distractor rationales
  - misconception tags (for analytics)

- **Calibration statistics (post-launch)**
  - item difficulty parameter(s)
  - discrimination parameter(s)
  - exposure count, p-value (proportion correct), time stats
  - differential item functioning (DIF) flags (if you’re doing fairness checks)

Psychometric basis: Item Response Theory (IRT) is the standard framework for linking item difficulty and ability estimation, and MST is a standard way to adapt by blocks/modules. citeturn8search20turn8search13turn8search28

### Psychometric calibration and why you likely need it

If you want “difficulty fidelity” and a meaningful “scaled score estimate,” you need calibration. Without it, “difficulty tagging” collapses into subjective labels that won’t converge.

A practical, staged approach:

- **Stage zero: Classical Test Theory (CTT) telemetry**
  - difficulty = % correct
  - discrimination proxy = point-biserial / correlation with total score
  - time-to-answer distributions
  (CTT isn’t cited directly above, but this stage is standard practice before IRT; treat as a recommendation consistent with testing standards emphasis on empirical evaluation. citeturn8search2)

- **Stage one: IRT calibration**
  - Start with a 2PL model (difficulty + discrimination) for dichotomous items; optionally 3PL if you model guessing for MCQ, but be conservative and validate. (Recommendation; IRT calibration/ability estimation is discussed broadly in psychometric literature and in MST instructional materials.) citeturn8search20turn8search13turn8search28  

- **Stage two: MST module calibration**
  - Create three second-stage modules per measure (easy/medium/hard) and calibrate module characteristic curves based on item parameters. MST is widely described as adaptation via modules/testlets. citeturn8search20turn8search28

### Scoring: what you can confirm vs what you must approximate

**Confirmed by ETS**
- Verbal & Quant: section-level adaptive, second section chosen based on first; within a section, questions contribute equally; raw score = number correct; scoring accounts for difficulty level of sections. citeturn11search27turn3view0turn11search0  
- Score ranges: Verbal and Quant 130–170 (1-point increments); AWA 0–6 (half-point increments). citeturn11search1turn5search2

**Not fully public / must be approximated**
- Exact mapping from (raw correct, section difficulty) → scaled score. ETS does not publish the formula in the cited materials. citeturn11search27turn3view0

**Recommended scoring approach for a mock platform**
- Use two parallel score outputs:
  1) **Exam-faithful score estimate** (range):  
     - ability estimate from IRT/MST (θ) → map to 130–170 using a learned monotonic mapping fit to your population and anchored to public percentile distributions as a sanity check. ETS publishes percentile tables/interpretive data and describes how percentiles are based on a 3-year window. citeturn11search1turn11search10  
  2) **Learning score** (diagnostic):  
     - skill subscores by concept type, trap type, pacing, etc. ETS’s Diagnostic Service itself frames performance by skill area, difficulty level, and time spent. citeturn11search9turn11search24

**Score confidence**
- Report a confidence band (e.g., “Estimated 161–165Q”) rather than a point score when item bank calibration is immature.
- As calibration improves and sample size grows, shrink bands.

This aligns to good measurement practice: avoid overclaiming precision when error bars are large (recommendation grounded in the Standards’ emphasis on appropriate interpretation and communication of scores). citeturn8search2

## Product scope and UX

### Core user journeys and what each requires technically

A GRE platform’s “surface features” are only valuable if the underlying test engine is consistent and auditable. Below are the journeys you listed, mapped to required subsystems.

**Diagnostic (first-time baseline)**
- One-click full test build (locked, timed)
- Score + confidence + next-step study plan
- Requires: test assembly + scoring + analytics + recommendation layer

**Topic drill**
- Adaptive selection by concept + difficulty + time goal
- Requires: item bank metadata + drill selection algorithm + explanation + error logging

**Section drill**
- Build a Verbal-only or Quant-only timed section with GRE navigation rules
- Requires: section engine + timer + module selection (optional)

**Full mock**
- Strict timing, transitions, no inter-section review
- Requires: session orchestration, persistence, and failure recovery

**AWA practice**
- Essay editor + submission + rubric-based feedback + score estimate
- Requires: AWA evaluation service + safety controls (prompt injection, consistency)

**Post-test review**
- Review with explanations, marked questions, and “error taxonomy”
- Requires: response storage + explanation store + analytics

**Long-term study plan**
- Weekly schedule, spaced repetition, and retest checkpoints
- Requires: recommendation engine + user goal model + content availability planning

### Personas and how they influence product decisions

Personas change not just UI copy, but the default test configurations you should offer.

- **First-time test taker**: needs onboarding, format familiarization, and a diagnostic quickly. (Support: ETS emphasizes understanding test design features; POWERPREP Test Preview Tool exists specifically for interface familiarity.) citeturn3view0turn16view0  
- **Score improver (mid-range)**: needs drill → review → plan loops with tight analytics.
- **Retaker targeting 325+**: needs difficult item exposure, fine-grained pacing analytics, and robust AWA scoring consistency.
- **Quant-focused**: higher density of difficult quant modules; prevent overfitting by rotating content.
- **Verbal-focused / non-native English speaker**: needs vocabulary tracking and RC pacing support; beware that AWA grammar feedback can be demotivating if not framed carefully.
- **Time-constrained professional**: needs short drills and “smart sessions.”

### MVP vs later versions

A serious MVP is still substantial; the key is to ship a **high-integrity test engine** with a smaller content footprint.

**MVP must include**
- Full-length timed mocks with correct 2023+ timing and within-section navigation rules. citeturn3view0turn2search8  
- Section-based tests (Verbal-only, Quant-only)
- Topic drills (Quant + Verbal) with deterministic answer checking and explanations (human-authored or human-reviewed)
- Custom tests filtered by (topic, difficulty band, timing)
- AWA Issue essay submission with rubric-aligned critique and a conservative score estimate
- Review mode (including marked questions) and analytics comparable in *type* to ETS Diagnostic Service (difficulty level bins, time per question, skill area breakdown). citeturn11search9turn11search24  

**Defer to V1/V2**
- True MST-like adaptivity across drills with calibrated item exposure control
- Large-scale personalized study plans (calendar integrations, push reminders)
- Human tutoring add-ons / appeals process for essay scoring disputes
- Institutional dashboards (B2B)

### UI details that strongly affect perceived realism

Because the GRE experience is shaped by interaction constraints as much as content, UX fidelity should be QA’d against ETS behaviors.

**High-impact UI behaviors to replicate**
- Mark/Review and “review list screen” behavior. citeturn2search8  
- Select-in-passage interaction (select a sentence). citeturn9search2  
- Numeric Entry layout (one box vs fraction boxes) and equivalence rules. citeturn17view0  
- Quantitative Comparison fixed answer options A–D. citeturn6view0  
- On-screen calculator availability only in Quant. citeturn11search11turn1search5  
- Full-screen “exam mode” with predictable font/layout; ETS practice materials recommend full screen at 100% zoom and minimum resolution. citeturn16view0  

### Accessibility and accommodations

Even if you’re not delivering a legally “high-stakes” exam, accessibility is essential for commercial viability and user trust.

Standards to implement:
- **WCAG 2.2** for contrast, keyboard navigation, non-text contrast, etc. citeturn12search0turn12search36  
- Timing accommodations: WCAG’s “Timing Adjustable” explains why users need ability to adjust/extend time limits in many contexts. citeturn12search26  
- ETS explicitly supports accommodations like extended time, extra breaks, screen magnification, selectable colors, screen reader/refreshable braille compatibility in official prep contexts. citeturn12search3turn12search31

**Implication for your platform**
- Architect time controls as **policy objects** (e.g., standard, 1.5×, 2×, extra breaks) rather than hardcoding.
- Make accommodations visible in session metadata and analytics for fairness of interpretation.

## LLM-supervised layer and AWA evaluation

### Deterministic vs LLM-driven vs never fully delegated to an LLM

This separation is a core product integrity requirement.

| Subsystem / decision | Should be deterministic / rule-based | Can be LLM-driven (with guardrails) | Should never be left entirely to an LLM |
|---|---|---|---|
| Timers, section transitions, locking, navigation constraints | Yes (must match test rules) citeturn3view0turn2search8 | No | Yes (never LLM) |
| Answer checking (MCQ / multi-select / QC / numeric entry equivalence) | Yes (format and equivalence rules) citeturn6view0turn17view0 | LLM can assist with “why wrong” explanations | LLM must not be source of truth for correctness |
| Test assembly (blueprint constraints, module selection, exposure control) | Yes (rules + psychometric models) citeturn3view0turn8search20 | LLM can propose assembly, but must pass deterministic validator | LLM must not freely pick items without constraints |
| Verbal/Quant scaled score estimate | Yes (documented algorithm, versioned) citeturn11search27turn11search1 | LLM can narrate uncertainty and guidance | LLM must not “guess” scores |
| AWA scoring | Partially (rubric structure, length limits, off-topic checks) citeturn0search5turn15search2turn16view0 | Yes (rubric-based critique, multi-model checks) | Never a single un-audited model for final “authoritative” score |
| Explanation generation | Deterministic structure for what an explanation must include | Yes (draft explanations), then QA | Never publish LLM explanations without review/verification gates |
| Study-plan recommendations | Deterministic constraints (time budget, target date, content availability) | Yes (narrative plan + motivation) | Never let LLM schedule content it can’t justify with the user’s data |
| Content QA / reviewer copilots | Deterministic lint checks | Yes (flag style drift, ambiguity suspicion) | Never let LLM approve items for production alone |

The “never” column is especially important because hallucination and misalignment risks are well documented in LLM feedback/grading contexts. citeturn10search30turn7search6

### Where LLMs add value in this product

A useful way to decide LLM integration is to classify each use case by accuracy tolerance, latency tolerance, and risk.

**High-value, appropriate LLM uses (with constraints)**
- AWA critique (structure, reasoning, clarity) aligned to ETS rubric language. citeturn15search5turn0search2  
- Explanation drafting *when paired with deterministic answer validation and internal reference solution*. (Recommendation supported by documented hallucination risks in generated feedback.) citeturn10search30turn10search2  
- Error pattern summarization (cluster repeated misconceptions)
- Reviewer copilots: suggest alternate distractors, flag potential ambiguity, suggest passage edits

**Medium-risk uses**
- Study plans: helpful but can overfit and overpromise; must be constrained by deterministic scheduler and user time budget.

**High-risk uses**
- Any “authoritative scoring” without calibration or audit.
- Any correctness decisions without deterministic validation.

### AWA scoring and critique design

#### What ETS confirms about AWA scoring approach

ETS describes AWA as holistic scoring on the 0–6 scale. citeturn15search5turn11search1  
ETS also documents that e-rater is used in conjunction with human scoring in high-stakes settings, including GRE Issue tasks, and has research on check-scoring models. citeturn7search8turn7search23  
ETS’s own public material on e-rater explains it extracts features of writing quality and cites research showing strong agreement with human raters on GRE Issue tasks. citeturn7search0turn7search1

Even ETS research also documents failure modes if automated scoring is used as the sole method in high-stakes contexts. citeturn7search32  
Inference: your product should treat automated scoring as “estimate + feedback,” not as a final uncontestable credential.

#### What current research says about LLM essay scoring reliability

Recent studies show mixed results: some find good reliability/validity under certain conditions, while other work finds that LLM grading can diverge systematically from human judgments depending on essay characteristics and rubric framing. citeturn7search33turn7search2turn7search6  
Therefore, design for:
- calibration against benchmark essays
- multi-rater agreement checks (ensemble or redundancy)
- contestability (user can understand why they got a score)

#### Recommended AWA architecture

A robust AWA evaluation service should be **multi-signal**:

1) **Deterministic prechecks (gatekeeping)**
- word count bounds (ETS’s ScoreItNow guidance notes e-rater may not score essays that are too brief or too long; PPP FAQ cites <50 or >1000 word bounds for scoring). citeturn16view0turn15search26  
- off-topic / copied prompt detection (score 0 if not addressing the issue). citeturn15search2turn0search5  
- basic language metrics (sentence boundary sanity, excessive repetition)

2) **Rubric engine (structure, not scoring)**
- formalize ETS score descriptors into rubric dimensions: position clarity, development, organization, support, language control. citeturn15search24turn15search5  
- output must be structured JSON so you can render consistent feedback

3) **LLM scoring + explanation (primary model)**
- Prompt strictly as a grader with rubric + anchor examples (see evaluation section)
- Require structured outputs and schema validation

4) **LLM check-rater (secondary model)**
- Independent scoring pass (different model or different provider route)
- Compare: if discrepant beyond threshold, lower confidence and/or send to human audit queue (mirrors ETS check-score logic conceptually; ETS describes check-score workflow in policy research). citeturn7search23

5) **Security layer: prompt-injection resistance**
- Treat essay text as untrusted input.
- Strip or neutralize hidden instructions (e.g., HTML/CSS tricks in essay editor are limited, but prompt injection can be plain text).
- Use “grader sandboxing” patterns: do not allow essay content to override system instructions.

Prompt injection in grading is an active risk area; recent research shows results depend on model and injection style, and systematic studies exist. citeturn7search3turn7search11turn7search7

6) **Audit & monitoring**
- Store all rubric outputs, scores, and model metadata for later drift analysis (with privacy controls)
- Regularly run benchmark sets to detect score drift when models change.

#### AWA failure cases to explicitly test

You listed several; all should become regression test suites:

- polished but shallow essays (style vs substance mismatch)
- highly templated essays
- off-topic / partially relevant essays (score 0 risk) citeturn15search2turn0search5
- non-native grammar issues (avoid unfair penalization beyond rubric intent)
- adversarial prompt injection within essay content citeturn7search11turn7search7

### OpenRouter integration constraints

Treat OpenRouter as a routing/orchestration layer, but do not tie product quality to one specific model.

Key OpenRouter controls to design around:
- **Structured outputs**: OpenRouter documents schema-enforced JSON outputs for consistent type-safe responses. citeturn13search0  
- **Tool/function calling**: supported, but your app calls tools, not the model directly; design your orchestrator accordingly. citeturn13search12  
- **Routing and fallback**: OpenRouter supports multi-provider routing and model fallback configurations. citeturn13search32turn13search11  
- **Rate limits / usage introspection**: OpenRouter documents how to check limits/credits and plan-based limits. citeturn13search1turn13search30turn13search22  
- **Privacy/logging controls**: OpenRouter describes provider-level logging/retention and settings like Zero Data Retention routing. citeturn13search2turn13search14turn13search20  
- **Terms**: If you enable logging, understand what license you grant; OpenRouter’s Terms discuss private input/output logging and other data use. citeturn13search6  

entity["company","OpenRouter","llm api router"]

## System architecture and data model

### Architecture goals (non-negotiable)

- **Exam-faithful behavior**: timers, locks, transitions, and input formats must be deterministic. citeturn3view0turn17view0  
- **Resilience**: no lost answers on crash/network loss; recover session state instantly.
- **Auditability**: every scoring-affecting event is logged.
- **Scalability**: content authoring + analytics + LLM cost controls.
- **Model-agnostic LLM layer**: easy to switch/check models; monitor quality drift.

### Recommended deployment shape given “Python everywhere + wxPython UI”

A practical approach is a **desktop client + cloud backend**, both in Python:

**Client (wxPython desktop app)**
- Exam UI (timers, navigation, question rendering)
- Local persistence for autosave and offline tolerance
- Secure API client to sync responses and fetch questions
- Optional local “exam mode” lock (not true proctoring; just UX)

wxPython is explicitly a cross-platform Python GUI toolkit built on wxWidgets. citeturn18search1turn18search4turn18search0  
entity["organization","wxPython","python gui toolkit"] (Note: entity type constraints prevent marking as software; treat as org if needed; if not desired, ignore.)

**Backend (Python services)**
- API gateway (auth, sessions, content delivery)
- Test assembly & session engine
- Scoring service (MCQ deterministic, AWA estimator)
- Analytics pipeline
- Admin/reviewer tooling
- LLM orchestrator (OpenRouter)

This split is not required, but it helps achieve reliability: the timer and answer capture can continue even if the network blips, while the back end remains the source of truth for scoring and analytics.

### Service decomposition

Start as a modular monolith (fewer moving parts), but design boundaries so you can split later. The minimal separations that pay off early:

- **Auth & user profile**
- **Question bank service**
- **Test session engine**
- **Scoring engine**
- **AWA evaluation service**
- **Analytics service**
- **Admin/reviewer portal**
- **LLM orchestration layer**
- **Observability + audit logs**

### Stack options in Python and tradeoffs

**API framework**
- FastAPI: strong for typed APIs and async workloads; official docs describe it as “modern, fast.” citeturn18search5turn18search2  
- Django: strong for built-in auth, admin, permissions; Django docs confirm built-in authentication system. citeturn18search3turn18search9  

Practical recommendation:
- Use **Django** for admin/editor workflows and user auth (fastest to ship correct permissions and audit trails), and **FastAPI** for high-throughput session APIs if needed. If you want simplicity, pick one and enforce modular boundaries internally.

**Data**
- Relational DB (PostgreSQL) for: users, sessions, items, responses, calibration stats, payments.
- Object storage for: images/figures, passages, AWA essays (if stored), exports.
- Cache (Redis) for: hot session fetches, question payloads, rate-limiting.
- Queue (Celery/RQ) for: scoring jobs, analytics rollups, LLM calls, nightly calibration.

**Vector search**
- Optional. Use only if you need:
  - semantic deduplication for generated questions
  - explanation retrieval
  - similarity checks to avoid accidental imitation of copyrighted material (see legal section)
  This is a recommendation; vector search is a tool, not a default.

### The testing-session model in detail

Design the test engine as a state machine with explicit invariants.

**Core objects**
- *TestForm* (a full mock configuration): sections, timings, module path rules
- *Session* (one user attempt): immutable form + mutable state
- *SectionState*: timer start/stop times, current question index, marked set
- *ResponseState*: per question answer, timestamps, changes

**State invariants you must enforce**
- Cannot go back to prior section once submitted (GRE-like). (ETS explicitly describes navigation within section; it does not describe cross-section review; mock should match typical GRE behavior.) citeturn3view0turn2search8  
- Mark/review only within current section. citeturn2search8  
- Time expiration auto-submits section; unanswered count as incorrect (platform policy; ETS confirms raw is number correct; leaving blank yields no credit). citeturn11search27turn11search24  

**Autosave + recovery**
- Every answer change emits an append-only event: (timestamp, question_id, new_response, client_timer_state, client_build_version)
- Client writes to local journal immediately (no network required), then syncs to server in background.
- On restart: replay journal to reconstruct. Server becomes authoritative once synced.
- ETS’s official practice tests explicitly support quit/save and resume later. citeturn16view0  
  This is strong evidence that “resume” is an expected user behavior even in official-like practice contexts.

**Question navigation constraints**
- Allow free movement within section (next/prev/jump-to/unanswered list), mirroring “preview and review capabilities.” citeturn3view0turn2search8  
- Make “end section” action scary/confirm (irreversible).

**Should you mimic GRE navigation exactly or differ for pedagogy?**
- Recommendation: provide two modes:
  - **Official Simulation Mode**: strict GRE-like constraints.
  - **Learning Mode**: allow pausing timers, showing solution after each question, etc.
This avoids the trap of compromising realism to add pedagogy; you isolate them.

### Data model recommendation

Below is a schema sketch sufficient for PM + architect + founding engineer handoff. Store everything versioned; content changes must never silently alter past attempts.

**Relational tables (core)**
- users(id, email, locale, created_at, …)
- user_profiles(user_id, target_score_v, target_score_q, test_date, accommodations_policy_id, …)
- accommodations_policies(id, time_multiplier, extra_breaks, …) citeturn12search3turn12search26
- questions(id, version, measure, subtype, stimulus_id, prompt, …)
- question_options(question_id, option_id, text, is_correct [internal], …)
- question_metadata(question_id, tags, difficulty_target, time_target, provenance, status, …)
- stimuli(id, type [passage/graph/table], payload_ref, render_spec, …)
- explanations(question_id, version, explanation_text, proof_steps [quant], …)
- tests(id, version, type [full/section/custom/drill], blueprint_spec_json, …)
- test_modules(id, test_id, stage, difficulty_band, module_spec_json, …)
- sessions(id, user_id, test_id, started_at, ended_at, mode [simulation/learning], …)
- session_sections(id, session_id, measure, section_index, time_limit_s, started_at, ended_at, module_id, …)
- responses(id, session_id, question_id, response_payload_json, is_marked, timestamps, …)
- scoring_results(session_id, v_raw, q_raw, v_est, q_est, v_ci, q_ci, …)
- awa_submissions(id, session_id, prompt_id, essay_text_ref, word_count, …)
- awa_results(submission_id, score_est, score_ci, rubric_json, model_metadata_json, …)
- telemetry_events(id, session_id, event_type, event_payload_json, created_at, …)
- item_stats(question_id, calibration_version, p_value, discrim, time_p50, exposure, …)

**Document / blob storage**
- essay_text (encrypted at rest, access-controlled)
- stimuli assets (images)
- exports (PDF score reports)

## Execution blueprint and operating model

### Executive summary

A high-quality GRE mock platform is primarily an **assessment delivery + measurement system**, not a content site. The key success factor is an exam-faithful **test engine** (timing, navigation, formats, adaptive module selection), supported by a rigorously governed **item bank** and a transparent **scoring approximation** that communicates uncertainty. ETS confirms the current exam’s structure, section timings, within-section navigation features, and section-level adaptive behavior; these become your platform’s immutable constraints for “Official Simulation Mode.” citeturn3view0turn4view0turn11search27

The LLM-supervised layer should not replace assessment design expertise. Instead, use LLMs where they add leverage—AWA critique, explanation drafting, analytics narratives, reviewer copilots—while keeping correctness, timing, and scoring algorithms deterministic and auditable. Recent research shows both promise and risk in LLM grading: some studies find good alignment, others find systematic differences; prompt injection is a real threat vector in educational evaluation. citeturn7search33turn7search6turn7search11turn7search3

Given the constraint of “Python everywhere” and desktop UI via wxPython, the recommended architecture is a **wxPython client** (local persistence + strict timer engine) backed by a Python API and services for content, scoring, analytics, and LLM orchestration through OpenRouter with structured outputs and privacy controls. citeturn18search1turn13search0turn13search2

### MVP product requirements document

**Problem statement**  
Students need GRE practice that is close enough to the real test that improvements in pacing, stamina, and strategy transfer to test day—while still providing richer analytics than official tools typically expose.

**MVP goals**
- Deliver exam-faithful full mocks and section drills matching the post–Sept 22, 2023 format and timing. citeturn3view0turn4view0  
- Provide deterministic scoring of objective questions + conservative scaled-score estimates (with ranges).
- Provide AWA Issue practice with rubric-aligned feedback and a score estimate (with uncertainty). citeturn15search5turn15search15  
- Provide review mode + diagnostics reminiscent of ETS’s Diagnostic Service categories (difficulty bins, time spent, skill breakdown). citeturn11search9turn11search24

**MVP scope**
- Test modes: full mock, section test, topic drill, custom test builder (topic/difficulty/timing)
- AWA: Issue prompt + essay editor + score estimate + critique
- Review: per-question explanation + error tagging + pacing analytics
- Admin: question authoring, review workflow, versioning, and item retirement

**Out of scope for MVP**
- Real proctoring / identity verification (different product category)
- Guaranteed “official-equivalent” score predictions
- Massive question bank coverage without calibration (better to have fewer high-quality items)

**Success metrics**
- Reliability: <0.1% sessions with lost answers
- Realism: user survey on “felt like GRE” ≥ target threshold; plus behavioral proxies (mark/review usage rates, time distributions)
- Learning: improvement between diagnostic and subsequent mock within user cohort (tracked cautiously)

### Recommended system architecture

**Client (wxPython)**
- Render engine for all GRE formats
- Deterministic timer + local journal
- Secure sync to backend
- “Simulation Mode” and “Learning Mode” toggles

**Backend (Python)**
- Auth + billing
- Question bank API
- Test assembly service (blueprint + MST-like module selection)
- Session ingestion + telemetry
- Scoring service
- AWA evaluation service
- Analytics + recommendation service
- Admin/reviewer portal
- LLM orchestration service via OpenRouter (structured outputs, routing/fallback, caching, privacy settings) citeturn13search0turn13search11turn13search2turn13search32

### Question-bank creation and QA pipeline

**Pipeline stages**
1) Blueprint definition (coverage matrix)
2) Draft creation (expert or LLM-assisted)
3) Deterministic validation (format + answer validity)
4) Human content review (GRE style, ambiguity, traps)
5) Bias/sensitivity review (documented checklist) citeturn8search2turn11search2
6) Editorial review
7) Pilot release (drills)
8) Statistical monitoring (difficulty/time/discrimination)
9) Promotion to “Simulation Mode eligible”
10) Retirement/versioning rules (never edit live items without version bump)

**Acceptance criteria for going live in Simulation Mode**
- Passed deterministic validators
- Two independent human reviewers approve
- No ambiguity flags unresolved
- Pilot stats stable enough (minimum exposures)
- Explanation quality meets standards and is verified

Item-writing guidance supports these checks: plausible distractors, consistent option formatting, and explicit rationales; avoid constructs known to weaken reliability. citeturn10search23turn10search15turn10search7

### AWA scoring architecture

**Outputs**
- Score estimate (0–6 in half-point increments, but clearly “estimate”) citeturn11search1turn15search5
- Confidence band
- Rubric breakdown aligned to ETS score descriptors citeturn15search24turn0search2
- Actionable revision plan (2–3 prioritized changes)

**System design**
- Deterministic gates (length, off-topic) citeturn16view0turn15search2  
- Primary LLM grader (structured output)
- Secondary check grader (different model route)
- Disagreement policy → lower confidence or human audit queue (mirrors ETS check-score concept that prioritizes human adjudication on discrepancies) citeturn7search23  
- Prompt injection mitigations with red-team test suite citeturn7search3turn7search11

### GRE-fidelity checklist

Use this as a release gate for Simulation Mode:

- Timing matches ETS post-2023 timings exactly. citeturn3view0turn4view0  
- No scheduled break; at-home “optional breaks” disabled; test-center optional breaks do not pause timer. citeturn4view0turn4view1  
- AWA always first; Verbal/Quant order randomized across full mocks. citeturn3view0  
- Navigation: within-section back/forward + Mark/Review + review list; cannot revisit previous sections. citeturn3view0turn2search8  
- Quant: on-screen calculator available only in Quant sections. citeturn3view0turn1search1  
- Formats: QC fixed A/B/C/D; numeric entry equivalence rules; multi-select “no partial credit” enforced. citeturn6view0turn17view0turn9search2  
- RC includes select-in-passage support in Simulation Mode. citeturn14search9turn9search2  
- Passage mix approximates ETS description (~10 passages, mostly short). citeturn14search4  
- Score reporting uses correct ranges and increments. citeturn11search1turn5search2  
- Accessibility: meets WCAG 2.2 baseline; accommodations policy supported. citeturn12search0turn12search3turn12search26

### Risk register with mitigations

**Legal/IP risk**
- Risk: reproducing ETS copyrighted questions/prompts or too-close imitation. ETS states test materials are copyrighted and permission is required to reproduce; trademarks have strict informational-use rules. citeturn9search12turn9search13turn9search0turn9search1  
  Mitigation: strict provenance tracking; internal similarity detection; license where needed; legal counsel review; avoid ETS trademarks in product/domain names. citeturn9search0turn9search6

**Scoring credibility risk**
- Risk: users interpret mock score as official. ETS scoring formula is not fully public; only key properties are known. citeturn11search27turn3view0  
  Mitigation: publish “estimate + confidence”; validate against benchmark tests; transparent methodology.

**LLM hallucination risk**
- Risk: incorrect explanations or misleading feedback harms trust. Hallucination and hallucinated feedback are documented problems. citeturn10search2turn10search30  
  Mitigation: deterministic validators; human review gates; structured output; retrieval of internal solution steps.

**LLM grading manipulation risk**
- Risk: prompt injection in essays alters scores. Prompt injection in educational grading is a known threat and an active research area. citeturn7search11turn7search7turn7search3  
  Mitigation: treat essay as untrusted; model sandboxing; secondary check-rater; audit sampling.

**Operational cost risk**
- Risk: LLM costs spike with explanation and AWA usage.  
  Mitigation: cache prompts where appropriate; route to cheaper models for low-stakes tasks; enforce structured outputs to reduce retries. OpenRouter documents prompt caching and routing/fallback features. citeturn13search11turn13search32turn13search0

**Accessibility risk**
- Risk: unusable for keyboard/screen readers; reputational and potential legal exposure depending on markets. WCAG defines requirements; ETS offers accessibility accommodations in official prep contexts. citeturn12search0turn12search3turn12search26  
  Mitigation: accessibility test plan; accommodations policy system; keyboard-first navigation.

### Phased roadmap

**MVP**
- wxPython client with Simulation Mode test engine
- Small but high-quality calibrated starter bank (enough for multiple full mocks + drills)
- Deterministic scoring + conservative scaled score estimate (range)
- AWA Issue practice with rubric + two-model check
- Admin authoring/review workflow + audit logs

**V1**
- MST-like module routing refined (easy/medium/hard second sections)
- Larger bank with pilot → promotion workflow
- Enhanced analytics: pacing curves, fatigue drop-off, trap taxonomy
- Better study planner with deterministic scheduler and LLM narration

**V2**
- Psychometric maturity: robust IRT calibration, DIF monitoring, exposure control
- Advanced reviewer copilots
- Institution/B2B features (if desired)
- Optional human essay review add-on and contestability workflow

### Open questions requiring expert validation

- What “realistic enough” score accuracy target is acceptable before marketing score predictions (e.g., correlation with official POWERPREP or self-reported official scores)?
- What exact RC passage count and mix should be used in your Simulation Mode assemblies beyond ETS’s approximate passage count description? citeturn14search4  
- Do you include any platform-only unscored research modules for calibration (would reduce realism, but improve item calibration), or keep calibration entirely in drills?
- How will you define and maintain “GRE-like” linguistic style for TC/SE without copying (requires expert verbal item writers)?
- What is the initial psychometric model choice (CTT first vs immediate IRT) and what minimum sample sizes are required before items are promoted?
- What audit rate for AWA scoring is acceptable (e.g., % of essays sent to human review) to manage risk and improve calibration?
- What privacy posture is required for storing essays and LLM outputs (e.g., require OpenRouter ZDR-only routing)? citeturn13search14turn13search2  
- What jurisdictions and accessibility obligations matter for launch (WCAG vs specific national requirements)? citeturn12search0