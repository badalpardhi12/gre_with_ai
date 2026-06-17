#!/usr/bin/env python3
"""
Reusable wxPython screenshot harness for the GRE exam-mode question screen.

Renders ``screens.question_screen.QuestionScreen`` full-window — navy header,
white serif content, grey directions band, centered Mark/Back/Next, navy footer
(navigator + Calc + Help + timer) — for a representative question of every GRE
subtype, and saves one PNG per subtype to ``/tmp/ets_ui/<subtype>.png`` so a
human can visually verify ETS fidelity. Also captures the floating calculator
alone.

The WebView (KaTeX) renders ASYNCHRONOUSLY, so each capture shows the frame and
pumps the wx event loop for a short delay before grabbing the bitmap. Each PNG
is verified non-trivial (decoded back, checked for a minimum size and for not
being a single solid color); a blank grab is retried with a longer delay and
then falls back from ``wx.WindowDC`` to ``wx.ScreenDC``.

Usage::

    venv/bin/python scripts/ui_screenshot.py            # all subtypes + calculator
    venv/bin/python scripts/ui_screenshot.py all        # same
    venv/bin/python scripts/ui_screenshot.py qc         # one subtype
    venv/bin/python scripts/ui_screenshot.py calculator # just the calculator

Output directory: /tmp/ets_ui/
"""
import os
import subprocess
import sys
import time

# Make the project root importable when run as `venv/bin/python scripts/...`.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import wx  # noqa: E402

from models.exam_session import SectionType, SECTION_META  # noqa: E402
from screens.question_screen import QuestionScreen  # noqa: E402
from widgets.calculator import CalculatorWidget  # noqa: E402


OUT_DIR = "/tmp/ets_ui"
FRAME_SIZE = (1366, 900)
# Top-left on-screen origin for captured frames (kept fully on the primary
# display so the screen-rect grab isn't clipped).
FRAME_ORIGIN = (40, 40)
# KaTeX is served from the CDN here (no local resources/katex bundle), so give
# the WebView a generous window to fetch + typeset before the grab.
RENDER_DELAY_S = 1.8
RETRY_DELAY_S = 3.2
MIN_PNG_BYTES = 15_000


# ── Fakes satisfying exactly the QuestionScreen.configure() contract ──────────

class FakeSectionState:
    """Minimal stand-in for ``models.exam_session.SectionState``.

    Implements only what ``QuestionScreen.configure`` / ``_load_question`` read:
    section_type, time_limit, total_questions, navigate_to, current_question_id,
    get_response, marked, question_ids, count_answered, tick, set_response,
    current_index, toggle_mark, plus the optional display_label.
    """

    def __init__(self, section_type, question_ids, time_limit, total_questions):
        self.section_type = section_type
        self.question_ids = list(question_ids)
        self.time_limit = time_limit
        self._total_questions = total_questions
        self.current_index = 0
        self.marked = set()
        self.responses = {}
        self.display_label = None
        self.total_sections = 5

    @property
    def total_questions(self):
        return self._total_questions

    @property
    def current_question_id(self):
        if 0 <= self.current_index < len(self.question_ids):
            return self.question_ids[self.current_index]
        return None

    def navigate_to(self, index):
        if 0 <= index < len(self.question_ids):
            self.current_index = index
            return True
        return False

    def get_response(self, question_id):
        return self.responses.get(question_id)

    def set_response(self, question_id, payload):
        self.responses[question_id] = payload

    def toggle_mark(self, question_id=None):
        qid = question_id or self.current_question_id
        self.marked.symmetric_difference_update({qid})

    def count_answered(self):
        return sum(1 for qid in self.question_ids
                   if self.responses.get(qid) not in (None, {}))

    def tick(self, elapsed_seconds=1):
        return False


class FakeBank:
    """Returns a single canned question by id. Mirrors the
    ``question_bank.get_question(qid) -> dict`` contract."""

    def __init__(self, question):
        self._q = question

    def get_question(self, qid):
        if qid == self._q["id"]:
            return self._q
        return None


# ── Representative question builders (subtype -> dict) ────────────────────────

def _q(subtype, prompt, *, measure, options=None, stimulus=None,
       numeric_answer=None, qid=1):
    return {
        "id": qid,
        "subtype": subtype,
        "prompt": prompt,
        "options": options or [],
        "stimulus": stimulus,
        "numeric_answer": numeric_answer,
        "measure": measure,
    }


_QC_OPTIONS = [
    {"label": "A", "text": "Quantity A is greater.", "is_correct": False},
    {"label": "B", "text": "Quantity B is greater.", "is_correct": False},
    {"label": "C", "text": "The two quantities are equal.", "is_correct": True},
    {"label": "D", "text": "The relationship cannot be determined "
                           "from the information given.", "is_correct": False},
]


def build_qc():
    return _q(
        "qc",
        prompt=("<p>Quantity A: \\(\\frac{0.6}{0.04}\\)</p>"
                "<p>Quantity B: \\(\\frac{0.15}{0.01}\\)</p>"),
        measure="quant",
        options=_QC_OPTIONS,
    )


def build_mcq_single():
    return _q(
        "mcq_single",
        prompt=("<div class=\"prompt\">A store sells notebooks for "
                "\\(\\$3.50\\) each. During a sale, the price is reduced by "
                "\\(20\\%\\). How much does a customer pay for "
                "\\(4\\) notebooks at the sale price?</div>"),
        measure="quant",
        options=[
            {"label": "A", "text": "\\(\\$8.40\\)", "is_correct": False},
            {"label": "B", "text": "\\(\\$11.20\\)", "is_correct": True},
            {"label": "C", "text": "\\(\\$12.60\\)", "is_correct": False},
            {"label": "D", "text": "\\(\\$14.00\\)", "is_correct": False},
            {"label": "E", "text": "\\(\\$16.80\\)", "is_correct": False},
        ],
    )


def build_mcq_multi():
    return _q(
        "mcq_multi",
        prompt=("<div class=\"prompt\">If \\(n\\) is an integer and "
                "\\(2 < n < 12\\), which of the following could be the value "
                "of \\(n^2\\)? Indicate <strong>all</strong> such values."
                "</div>"),
        measure="quant",
        options=[
            {"label": "A", "text": "\\(9\\)", "is_correct": True},
            {"label": "B", "text": "\\(16\\)", "is_correct": True},
            {"label": "C", "text": "\\(20\\)", "is_correct": False},
            {"label": "D", "text": "\\(36\\)", "is_correct": True},
            {"label": "E", "text": "\\(49\\)", "is_correct": True},
            {"label": "F", "text": "\\(81\\)", "is_correct": True},
            {"label": "G", "text": "\\(144\\)", "is_correct": False},
        ],
    )


def build_numeric_entry():
    return _q(
        "numeric_entry",
        prompt=("<div class=\"prompt\">A rectangle has a length of "
                "\\(14\\) cm and a width of \\(9\\) cm. What is the area "
                "of the rectangle, in square centimeters?</div>"),
        measure="quant",
        numeric_answer={"mode": "decimal"},
    )


def build_numeric_entry_fraction():
    return _q(
        "numeric_entry",
        prompt=("<div class=\"prompt\">If \\(\\frac{x}{y} = \\frac{3}{6}\\), "
                "give the value of \\(\\frac{x}{y}\\) in lowest terms as a "
                "fraction.</div>"),
        measure="quant",
        numeric_answer={"mode": "fraction", "numerator": 1, "denominator": 2},
    )


def build_tc():
    # Two-blank Text Completion. Labels use the explicit blank1_/blank2_ prefix
    # so services.scoring.normalize_tc_options groups them into two columns.
    return _q(
        "tc",
        prompt=("<div class=\"prompt\">Although the committee had expected the "
                "negotiations to be (i)____________, the unexpected goodwill on "
                "both sides made the process remarkably (ii)____________.</div>"),
        measure="verbal",
        options=[
            {"label": "blank1_A", "text": "contentious", "is_correct": True},
            {"label": "blank1_B", "text": "perfunctory", "is_correct": False},
            {"label": "blank1_C", "text": "straightforward", "is_correct": False},
            {"label": "blank2_A", "text": "acrimonious", "is_correct": False},
            {"label": "blank2_B", "text": "amicable", "is_correct": True},
            {"label": "blank2_C", "text": "protracted", "is_correct": False},
        ],
    )


_RC_PASSAGE = (
    "<p>For much of the twentieth century, historians of science portrayed the "
    "so-called Scientific Revolution as an abrupt rupture with the past, a "
    "sudden triumph of reason over superstition. More recent scholarship, "
    "however, has complicated that picture. Far from rejecting the inherited "
    "learning of the medieval universities, many early modern natural "
    "philosophers drew heavily upon it, adapting Aristotelian categories even "
    "as they began to question Aristotelian conclusions. The continuity, these "
    "scholars argue, was as significant as the rupture.</p>"
)


def build_rc_single():
    return _q(
        "rc_single",
        prompt=("<div class=\"prompt\">The primary purpose of the passage is "
                "to</div>"),
        measure="verbal",
        stimulus={"content": _RC_PASSAGE},
        options=[
            {"label": "A", "text": "refute a widely held historical claim by "
                                   "presenting new archival evidence",
             "is_correct": False},
            {"label": "B", "text": "describe a shift in how scholars interpret "
                                   "a historical period", "is_correct": True},
            {"label": "C", "text": "defend the achievements of medieval "
                                   "universities", "is_correct": False},
            {"label": "D", "text": "trace the biography of a single natural "
                                   "philosopher", "is_correct": False},
            {"label": "E", "text": "argue that the Scientific Revolution never "
                                   "occurred", "is_correct": False},
        ],
    )


def build_rc_select_passage():
    passage = (
        "<p>"
        "<sent id='1'>The migration of monarch butterflies has long puzzled "
        "biologists.</sent> "
        "<sent id='2'>No single butterfly completes the round trip; the journey "
        "spans several generations.</sent> "
        "<sent id='3'>Recent studies suggest that the insects rely on a "
        "time-compensated sun compass to navigate.</sent> "
        "<sent id='4'>This mechanism adjusts for the sun's movement across the "
        "sky over the course of a day.</sent>"
        "</p>"
    )
    return _q(
        "rc_select_passage",
        prompt=("<div class=\"prompt\">Select the sentence that explains why a "
                "single butterfly cannot complete the migration.</div>"),
        measure="verbal",
        stimulus={"content": passage},
        options=[
            {"label": "1", "text": "", "is_correct": False},
            {"label": "2", "text": "", "is_correct": True},
            {"label": "3", "text": "", "is_correct": False},
        ],
    )


def build_data_interp():
    table = (
        "<p>Annual revenue (in millions of dollars) for Company X:</p>"
        "<table>"
        "<tr><th>Year</th><th>Domestic</th><th>International</th></tr>"
        "<tr><td>2019</td><td>120</td><td>45</td></tr>"
        "<tr><td>2020</td><td>135</td><td>60</td></tr>"
        "<tr><td>2021</td><td>150</td><td>90</td></tr>"
        "<tr><td>2022</td><td>168</td><td>132</td></tr>"
        "</table>"
    )
    return _q(
        "data_interp",
        prompt=("<div class=\"prompt\">Approximately what was the percent "
                "increase in international revenue from 2020 to 2022?</div>"),
        measure="quant",
        stimulus={"content": table},
        options=[
            {"label": "A", "text": "\\(47\\%\\)", "is_correct": False},
            {"label": "B", "text": "\\(72\\%\\)", "is_correct": False},
            {"label": "C", "text": "\\(90\\%\\)", "is_correct": False},
            {"label": "D", "text": "\\(120\\%\\)", "is_correct": True},
            {"label": "E", "text": "\\(220\\%\\)", "is_correct": False},
        ],
    )


def build_mcq_table():
    """A quant MC whose stimulus is a DATA TABLE (reported clipped/single-col)."""
    table = (
        "<p><b>Top Six Finishers, 2024 City Marathon</b></p>"
        "<table>"
        "<tr><th>Place</th><th>Runner</th><th>Country</th>"
        "<th>Time (hours)</th><th>Age</th></tr>"
        "<tr><td>1</td><td>A. Lopez</td><td>Mexico</td><td>2.14</td><td>26</td></tr>"
        "<tr><td>2</td><td>B. Kim</td><td>South Korea</td><td>2.16</td><td>24</td></tr>"
        "<tr><td>3</td><td>C. Smith</td><td>USA</td><td>2.18</td><td>31</td></tr>"
        "<tr><td>4</td><td>D. Patel</td><td>India</td><td>2.22</td><td>28</td></tr>"
        "<tr><td>5</td><td>E. Cho</td><td>South Korea</td><td>2.25</td><td>29</td></tr>"
        "<tr><td>6</td><td>F. Diaz</td><td>Mexico</td><td>2.30</td><td>34</td></tr>"
        "</table>"
    )
    return _q(
        "mcq_single",
        prompt=('<div class="prompt">What is the average (arithmetic mean) '
                'age of the top six finishers?</div>'),
        measure="quant",
        stimulus={"content": table, "type": "table"},
        options=[
            {"label": "A", "text": "28.0", "is_correct": False},
            {"label": "B", "text": "29.0", "is_correct": False},
            {"label": "C", "text": "30.7", "is_correct": True},
            {"label": "D", "text": "30.0", "is_correct": False},
            {"label": "E", "text": "31.0", "is_correct": False},
        ],
    )


def build_mcq_chart():
    """A quant question whose stimulus is a DATA CHART image (reported tiny).
    Uses a real chart stimulus exported from the seed when available."""
    import os
    chart = "<p>(chart unavailable)</p>"
    p = "/tmp/_chart_stim.html"
    if os.path.exists(p):
        chart = open(p).read()
    return _q(
        "numeric_entry",
        prompt=('<div class="prompt">The data for City X in 2024 shows '
                'temperature readings across all 12 months. By how many degrees '
                'Celsius does the average temperature of the four coldest months '
                'fall short of the four warmest months?</div>'),
        measure="quant",
        stimulus={"content": chart, "type": "graph"},
        numeric_answer={"mode": "decimal"},
    )


# Ordered so the deterministic gallery reads header→footer through the types.
BUILDERS = {
    "qc": build_qc,
    "mcq_single": build_mcq_single,
    "mcq_multi": build_mcq_multi,
    "numeric_entry": build_numeric_entry,
    "numeric_entry_fraction": build_numeric_entry_fraction,
    "tc": build_tc,
    "rc_single": build_rc_single,
    "rc_select_passage": build_rc_select_passage,
    "data_interp": build_data_interp,
    "mcq_table": build_mcq_table,
    "mcq_chart": build_mcq_chart,
}


# ── Event-loop pumping + window capture ───────────────────────────────────────

def _pump(seconds):
    """Run the wx event loop for ``seconds`` so async WebView content paints."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        wx.Yield()
        time.sleep(0.03)


def _activate_self():
    """Bring this Python GUI process to the foreground so the captured window
    is frontmost (the screen-rect grab below reads composited pixels, so an
    occluded window would capture whatever sits on top of it)."""
    try:
        subprocess.run(
            ["osascript", "-e",
             "tell application \"System Events\" to set frontmost of "
             "(first process whose unix id is %d) to true" % os.getpid()],
            capture_output=True, timeout=5)
    except Exception:
        pass


def _grab_window_dc(window, path):
    """Fallback: capture ``window`` via wx.WindowDC and save to ``path``.

    Captures wx-drawn native controls but NOT the WebView (KaTeX) layer on
    macOS — only used if the screencapture path is unavailable.
    """
    w, h = window.GetClientSize()
    bmp = wx.Bitmap(w, h)
    mdc = wx.MemoryDC(bmp)
    wdc = wx.WindowDC(window)
    mdc.Blit(0, 0, w, h, wdc, 0, 0)
    mdc.SelectObject(wx.NullBitmap)
    bmp.SaveFile(path, wx.BITMAP_TYPE_PNG)
    return os.path.exists(path)


def _grab_screencapture(window, path):
    """Capture ``window``'s on-screen rect with the macOS ``screencapture``
    CLI (``-R x,y,w,h``) and save to ``path``.

    This is the only method that captures the WKWebView (KaTeX) compositing
    layer on macOS: wx's own WindowDC/ScreenDC blits return the chrome but a
    blank content area (WindowDC can't see the WebView layer; ScreenDC needs a
    Screen Recording grant that the Python process lacks). The ``screencapture``
    binary carries its own Screen Recording entitlement, so it reads the real
    composited pixels — provided the window is frontmost (see ``_activate_self``)
    and fully on-screen.
    """
    r = window.GetScreenRect()
    res = subprocess.run(
        ["screencapture", "-x",
         "-R%d,%d,%d,%d" % (r.x, r.y, r.width, r.height), path],
        capture_output=True, text=True)
    return res.returncode == 0 and os.path.exists(path)


def _image_is_blank(path):
    """True if the PNG at ``path`` looks like a single solid color (or empty).

    Samples a grid of pixels; a real screenshot has many distinct colors, a
    blank grab is one flat color end-to-end.
    """
    img = wx.Image(path)
    if not img.IsOk():
        return True
    w, h = img.GetWidth(), img.GetHeight()
    if w < 4 or h < 4:
        return True
    seen = set()
    for fx in (0.1, 0.3, 0.5, 0.7, 0.9):
        for fy in (0.1, 0.3, 0.5, 0.7, 0.9):
            x = min(w - 1, int(w * fx))
            y = min(h - 1, int(h * fy))
            seen.add((img.GetRed(x, y), img.GetGreen(x, y), img.GetBlue(x, y)))
    return len(seen) <= 1


def _save_verified(window, path, label):
    """Grab + save ``window`` to ``path``, verifying the result is non-trivial.

    Tries ``screencapture`` first (the only method that captures the WebView on
    macOS), retrying with a longer delay if the grab is blank, then falls back
    to wx.WindowDC as a last resort. Verifies the written PNG decodes, exceeds a
    minimum size, and isn't a single solid color.

    Returns (ok, method, byte_size).
    """
    attempts = [
        ("screencapture", _grab_screencapture, RENDER_DELAY_S),
        ("screencapture", _grab_screencapture, RETRY_DELAY_S),
        ("WindowDC", _grab_window_dc, RETRY_DELAY_S),
    ]
    for method, grab, delay in attempts:
        window.Raise()
        _activate_self()
        _pump(delay)
        if not grab(window, path):
            print(f"  [{label}] {method} grab failed; retrying")
            continue
        size = os.path.getsize(path) if os.path.exists(path) else 0
        if _image_is_blank(path):
            print(f"  [{label}] blank grab via {method} (delay {delay}s); retrying")
            continue
        if size < MIN_PNG_BYTES:
            print(f"  [{label}] file too small via {method} ({size} bytes); retrying")
            continue
        return True, method, size
    return False, None, 0


# ── Per-subtype capture ──────────────────────────────────────────────────────

def _make_frame():
    frame = wx.Frame(None, title="GRE exam-mode screenshot", size=FRAME_SIZE,
                     style=wx.DEFAULT_FRAME_STYLE | wx.STAY_ON_TOP)
    frame.SetPosition(FRAME_ORIGIN)
    return frame


def _settle_layout(frame, screen, passes=6):
    """Pump events and force re-layout until the content splitter has real
    height.

    The footer's owner-drawn nav strip (``widgets.question_nav._CircleStrip``)
    reports its best size from its current width; before the first real layout
    that width is 0, so it assumes 1 circle per row and reserves ~12 rows of
    height. That oversized footer (proportion 0) starves the proportional
    content splitter to height 0, leaving the question area blank. Invalidating
    the strip's best size after it has a real width lets it collapse to one row,
    handing the splitter its space — exactly what the live app's MainLoop does
    over several resize cycles. We do it explicitly here so a static, MainLoop-
    free capture matches the running app.
    """
    for _ in range(passes):
        try:
            screen.question_nav.strip.InvalidateBestSize()
            screen.question_nav.Layout()
        except Exception:
            pass
        frame.SendSizeEvent()
        screen.Layout()
        _pump(0.15)
        if screen.content_splitter.GetSize().height > 200:
            break


def _remeasure_prompt(screen, subtype, ceiling=380):
    """Give the prompt WebView enough min-height to show its whole stem.

    The screen's auto-height (``MathView.set_content_auto_height``) measures the
    WebView's scrollHeight in its LOADED event, which on macOS frequently returns
    0 (the WKWebView hasn't flushed layout yet) or a value measured before KaTeX
    typesets — either way the prompt panel can clamp too short and its last row
    (e.g. the QC fraction values, or a multi-line stem) scrolls out of view. We
    can't trust a post-hoc scrollHeight read here (it also returns 0), so we set
    a comfortable fixed floor per subtype: QC needs room for the two-row
    Quantity-A/B table; the rest get a normal multi-line stem height.
    """
    mv = getattr(screen, "prompt_view", None)
    if mv is None:
        return
    # Disable the screen's one-shot auto-height so a late WebView LOADED event
    # (KaTeX/CDN can finish loading after we've laid out) doesn't re-clamp the
    # prompt back down to its floor and re-clip the content. Then set our own
    # comfortable floor.
    mv._auto_height_active = False
    floor = 200 if subtype == "qc" else 150
    mv.SetMinSize((-1, min(floor, ceiling)))
    parent = mv.GetParent()
    if parent is not None:
        parent.Layout()
    screen.Layout()


def capture_subtype(subtype):
    """Render the QuestionScreen for one subtype and save its PNG.

    Returns (ok, method, byte_size, path).
    """
    builder = BUILDERS[subtype]
    question = builder()
    # numeric_entry_fraction is the numeric_entry subtype with a fraction
    # numeric_answer; the screen derives the fraction control + directions from
    # that, so render under its real subtype but key the file on the request.
    measure = question["measure"]
    sec_type = SectionType.QUANT_S1 if measure == "quant" else SectionType.VERBAL_S1
    _, _, time_limit, _ = SECTION_META[sec_type]

    state = FakeSectionState(
        section_type=sec_type,
        question_ids=[question["id"]],
        time_limit=time_limit,
        total_questions=12,
    )
    bank = FakeBank(question)

    frame = _make_frame()
    screen = QuestionScreen(frame)
    screen.configure(state, bank, measure, mode="simulation", exam=None)
    # Static timer reading (skip the live countdown for a deterministic shot).
    screen.timer.set_time(time_limit)

    sizer = wx.BoxSizer(wx.VERTICAL)
    sizer.Add(screen, 1, wx.EXPAND)
    frame.SetSizer(sizer)
    frame.SetSize(FRAME_SIZE)
    frame.Show()
    frame.Layout()
    screen.Layout()
    _settle_layout(frame, screen)
    # Re-measure the prompt WebView's true rendered height. The screen's
    # auto-height (``MathView.set_content_auto_height``) measures scrollHeight in
    # the WebView's LOADED event, which can fire BEFORE KaTeX finishes typesetting
    # — so a QC table or a tall stem gets clamped too short and its last row
    # scrolls out of view. Now that KaTeX has had time to render, read the real
    # scrollHeight and apply it so the whole prompt is visible.
    _remeasure_prompt(screen, subtype)
    # Re-center the passage splitter now that the splitter has its real width
    # (RC/DI/select-in-passage subtypes show a left passage pane).
    if screen.content_splitter.IsSplit():
        w = screen.content_splitter.GetClientSize().width
        if w > 0:
            screen.content_splitter.SetSashPosition(w // 2)
    screen.Layout()
    _pump(0.3)

    path = os.path.join(OUT_DIR, f"{subtype}.png")
    ok, method, size = _save_verified(frame, path, subtype)
    frame.Destroy()
    return ok, method, size, path


def capture_calculator():
    """Render the floating ETS calculator alone and save its PNG.

    Returns (ok, method, byte_size, path).
    """
    # CalculatorWidget needs a top-level parent to anchor its floating frame.
    host = _make_frame()
    host.SetSize((200, 120))
    host.Show()
    calc = CalculatorWidget(host)
    calc.Show()
    frame = calc._frame  # the floating MiniFrame holding the keypad
    frame.Raise()

    path = os.path.join(OUT_DIR, "calculator.png")
    ok, method, size = _save_verified(frame, path, "calculator")
    calc.Destroy()
    host.Destroy()
    return ok, method, size, path


def main(argv):
    os.makedirs(OUT_DIR, exist_ok=True)
    arg = (argv[1] if len(argv) > 1 else "all").lower()

    app = wx.App()  # noqa: F841 — must stay alive for the duration

    if arg == "all":
        targets = list(BUILDERS.keys()) + ["calculator"]
    elif arg in BUILDERS or arg == "calculator":
        targets = [arg]
    else:
        print(f"Unknown subtype '{arg}'. Choices: "
              f"{', '.join(list(BUILDERS.keys()) + ['calculator', 'all'])}")
        return 2

    results = []
    for name in targets:
        print(f"Capturing {name} ...")
        if name == "calculator":
            ok, method, size, path = capture_calculator()
        else:
            ok, method, size, path = capture_subtype(name)
        status = "OK" if ok else "FAILED"
        print(f"  -> {status} {path} ({size} bytes) via {method}")
        results.append((name, ok, method, size, path))

    print("\n=== Summary ===")
    for name, ok, method, size, path in results:
        flag = "clean" if ok else "BLANK/FAILED"
        print(f"  {name:24s} {flag:12s} {size:>8d} B  {path}  [{method}]")
    n_ok = sum(1 for _, ok, *_ in results if ok)
    print(f"\n{n_ok}/{len(results)} captured cleanly into {OUT_DIR}")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
