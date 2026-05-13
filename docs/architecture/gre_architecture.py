"""GRE Prep — System Architecture diagram.

Generates:
  diagrams/gre_architecture/gre_architecture.pdf
  diagrams/gre_architecture/gre_architecture.png

Run:  python3 /tmp/gre_arch/diagrams/gre_architecture/gre_architecture.py
"""
from pathlib import Path
from diagrams import Diagram, Cluster, Edge
from diagrams.custom import Custom

ICONS = Path("/tmp/gre_arch/icons")

# ── Global layout ────────────────────────────────────────────────────────────
graph_attr = {
    "bgcolor": "white",
    "pad": "0.5",
    "splines": "polyline",
    "rankdir": "TB",
    "nodesep": "0.4",
    "ranksep": "1.0",
    "dpi": "300",
    "label": (
        "<<B><FONT POINT-SIZE='22'>GRE Prep — System Architecture</FONT></B>"
        "<BR/><FONT POINT-SIZE='13' COLOR='#666666'>"
        "Local-first desktop app · Python 3.9 + wxPython 4.2 + Peewee/SQLite"
        "</FONT>>"
    ),
    "labelloc": "t",
    "labeljust": "c",
    "fontname": "Helvetica",
    "fontsize": "14",
}

node_attr = {
    "fontname": "Helvetica",
    "fontsize": "10",
    "fixedsize": "true",
    "width": "1.05",
    "height": "1.05",
    "imagescale": "true",
    "shape": "none",
    "labelloc": "b",
}

edge_attr = {
    "fontname": "Helvetica",
    "fontsize": "10",
    "color": "#444444",
    "arrowsize": "0.7",
    "penwidth": "1.2",
}

# 3-tier cluster colour palette (low saturation)
CLR_UI = "#eef3fb"          # pale blue — UI / user-facing
CLR_CORE = "#eaf4ec"        # pale green — deterministic core
CLR_LLM = "#fbf3e3"         # pale cream — LLM layer (optional)
CLR_DATA = "#f4eefb"        # pale lilac — data

CLUSTER_DEFAULTS = {
    "style": "rounded",
    "fontsize": "12",
    "fontname": "Helvetica-Bold",
    "fontcolor": "#222222",
    "margin": "14",
    "color": "#d8dde3",
    "penwidth": "1.0",
}

# Edge palette
CLR_PRIMARY = "#1f5fba"     # primary flow
CLR_WRITE = "#1a7a3a"       # persistence write-back
CLR_SECONDARY = "#9aa3ad"   # optional/dashed

with Diagram(
    "",
    filename="/tmp/gre_arch/diagrams/gre_architecture/gre_architecture",
    outformat=["pdf", "png"],
    show=False,
    direction="TB",
    graph_attr=graph_attr,
    node_attr=node_attr,
    edge_attr=edge_attr,
):

    # ─── UI LAYER ────────────────────────────────────────────────────────
    with Cluster(
        "UI Layer · wxPython 4.2",
        graph_attr={**CLUSTER_DEFAULTS, "bgcolor": CLR_UI},
    ):
        with Cluster(
            "Sidebar tabs",
            graph_attr={
                "style": "rounded,dashed",
                "color": "#b9c8de",
                "fontsize": "11",
                "fontname": "Helvetica",
                "fontcolor": "#555",
                "bgcolor": "#f6f9fd",
                "margin": "10",
                "penwidth": "0.9",
            },
        ):
            today = Custom("Today", str(ICONS / "screen.png"))
            learn = Custom("Learn", str(ICONS / "screen.png"))
            practice = Custom("Practice", str(ICONS / "screen.png"))
            vocab = Custom("Vocab", str(ICONS / "screen.png"))
            insights = Custom("Insights", str(ICONS / "screen.png"))
            error_log = Custom("Error Log", str(ICONS / "screen.png"))

        with Cluster(
            "In-flow screens",
            graph_attr={
                "style": "rounded,dashed",
                "color": "#b9c8de",
                "fontsize": "11",
                "fontname": "Helvetica",
                "fontcolor": "#555",
                "bgcolor": "#f6f9fd",
                "margin": "10",
                "penwidth": "0.9",
            },
        ):
            question = Custom("Question", str(ICONS / "screen.png"))
            awa_screen = Custom("AWA editor", str(ICONS / "screen.png"))
            results = Custom("Results", str(ICONS / "screen.png"))
            tutor_ui = Custom("AnswerChat", str(ICONS / "chat.png"))
            onboarding = Custom("Onboarding", str(ICONS / "screen.png"))

    # ─── DETERMINISTIC CORE ──────────────────────────────────────────────
    with Cluster(
        "Deterministic Core · never LLM-dependent",
        graph_attr={**CLUSTER_DEFAULTS, "bgcolor": CLR_CORE},
    ):
        exam = Custom("ExamSession\n+ adaptive routing", str(ICONS / "rule.png"))
        bank = Custom("QuestionBank\ncomposition-aware", str(ICONS / "struct.png"))
        scoring = Custom("ScoringEngine\n11 subtypes", str(ICONS / "chart.png"))
        timer = Custom("Timer\nwallclock-anchored", str(ICONS / "timer.png"))
        mastery = Custom("Mastery\nEWMA + decay", str(ICONS / "mastery.png"))
        rating = Custom("RatingService\nElo item rating", str(ICONS / "rating.png"))

    # ─── LLM LAYER ───────────────────────────────────────────────────────
    with Cluster(
        "LLM Layer · OpenRouter · optional",
        graph_attr={**CLUSTER_DEFAULTS, "bgcolor": CLR_LLM},
    ):
        awa_llm = Custom("AWAScorer\nETS rubric", str(ICONS / "essay.png"))
        coach = Custom("MistakeCoach\n+ AnswerChat", str(ICONS / "chat.png"))
        planner = Custom("StudyPlan\ngenerator", str(ICONS / "plan.png"))
        explainer = Custom("Explanation\ngenerator", str(ICONS / "ml.png"))

    # External OpenRouter (cloud, dashed)
    with Cluster(
        "External · cloud",
        graph_attr={
            "style": "rounded,dashed",
            "color": "#c9b88f",
            "fontsize": "11",
            "fontname": "Helvetica",
            "fontcolor": "#555",
            "bgcolor": "#fdf7e6",
            "margin": "12",
            "penwidth": "0.9",
        },
    ):
        openrouter = Custom("OpenRouter API\n(cloud, optional)",
                            str(ICONS / "openrouter.png"))

    # ─── DATA LAYER ──────────────────────────────────────────────────────
    with Cluster(
        "Data Layer · Peewee + SQLite",
        graph_attr={**CLUSTER_DEFAULTS, "bgcolor": CLR_DATA},
    ):
        mock_db = Custom("gre_mock.db\nshipped seed ~22 MB",
                         str(ICONS / "sqlite.png"))
        user_db = Custom("gre_user.db\nper-user state",
                         str(ICONS / "sqlite.png"))
        migrations = Custom("Migrations\nidempotent",
                            str(ICONS / "python.png"))

    # ── Primary user-flow edges: UI → Core ──────────────────────────────
    today >> Edge(color=CLR_PRIMARY, penwidth="1.6") >> exam
    practice >> Edge(color=CLR_PRIMARY, penwidth="1.6") >> exam
    question >> Edge(label="answer",
                     color=CLR_PRIMARY, penwidth="1.8") >> scoring
    exam >> Edge(color=CLR_PRIMARY, penwidth="1.4") >> bank
    exam >> Edge(color=CLR_SECONDARY, style="dotted") >> timer
    scoring >> Edge(label="update",
                    color=CLR_PRIMARY, penwidth="1.4") >> mastery
    scoring >> Edge(color=CLR_SECONDARY) >> rating
    bank >> Edge(label="pick",
                 color=CLR_SECONDARY) >> question

    # ── UI → LLM (optional flows) ────────────────────────────────────────
    awa_screen >> Edge(label="essay",
                       color=CLR_SECONDARY, style="dashed") >> awa_llm
    tutor_ui >> Edge(style="dashed", color=CLR_SECONDARY) >> coach
    insights >> Edge(style="dashed", color=CLR_SECONDARY) >> planner
    question >> Edge(label="fallback",
                     style="dashed", color=CLR_SECONDARY) >> explainer

    # ── LLM → External API (dashed, optional) ───────────────────────────
    awa_llm >> Edge(style="dashed", color=CLR_SECONDARY) >> openrouter
    coach >> Edge(style="dashed", color=CLR_SECONDARY) >> openrouter
    planner >> Edge(style="dashed", color=CLR_SECONDARY) >> openrouter
    explainer >> Edge(style="dashed", color=CLR_SECONDARY) >> openrouter

    # ── Core → Data (synchronous writes) ────────────────────────────────
    scoring >> Edge(label="Response",
                    color=CLR_WRITE, penwidth="1.6") >> user_db
    bank >> Edge(label="ServedLog",
                 color=CLR_WRITE, penwidth="1.4") >> user_db
    mastery >> Edge(label="MasteryRecord",
                    color=CLR_WRITE, penwidth="1.4") >> user_db
    exam >> Edge(label="Session",
                 color=CLR_WRITE, penwidth="1.4") >> user_db

    # Data reads
    bank >> Edge(label="read items",
                 color=CLR_SECONDARY, style="dotted") >> mock_db
    migrations >> Edge(style="dotted", color=CLR_SECONDARY) >> user_db
    migrations >> Edge(style="dotted", color=CLR_SECONDARY) >> mock_db
