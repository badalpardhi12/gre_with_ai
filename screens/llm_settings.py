"""
LLM Settings dialog — allows runtime configuration of OpenRouter API key,
base URL, model, and max tokens.
"""
import wx

from config import load_llm_config, save_llm_config, LLM_MODEL


# Popular models available on OpenRouter (for the dropdown). Listed
# Opus-first so the recommended high-quality default is at the top of the
# combobox.
POPULAR_MODELS = [
    "anthropic/claude-opus-4",
    "anthropic/claude-sonnet-4",
    "anthropic/claude-3.5-sonnet",
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "google/gemini-2.0-flash-001",
    "google/gemini-2.5-pro-preview-03-25",
    "meta-llama/llama-4-maverick",
    "deepseek/deepseek-chat-v3-0324",
    "mistralai/mistral-large",
]


class LLMSettingsDialog(wx.Dialog):
    """Dialog for configuring OpenRouter LLM settings."""

    def __init__(self, parent):
        super().__init__(parent, title="LLM Settings (OpenRouter)",
                         size=(520, 380),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)

        self._config = load_llm_config()
        self._build_ui()
        self._load_values()
        self.CenterOnParent()

    def _build_ui(self):
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # Info text
        info = wx.StaticText(self, label=(
            "Configure your OpenRouter connection. Get an API key at "
            "https://openrouter.ai/keys"
        ))
        info.Wrap(480)
        main_sizer.Add(info, 0, wx.ALL, 12)

        grid = wx.FlexGridSizer(rows=4, cols=2, hgap=8, vgap=10)
        grid.AddGrowableCol(1, 1)

        # API Key
        grid.Add(wx.StaticText(self, label="API Key:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.api_key_ctrl = wx.TextCtrl(self, style=wx.TE_PASSWORD)
        grid.Add(self.api_key_ctrl, 1, wx.EXPAND)

        # Base URL
        grid.Add(wx.StaticText(self, label="Base URL:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.base_url_ctrl = wx.TextCtrl(self)
        grid.Add(self.base_url_ctrl, 1, wx.EXPAND)

        # Model (combobox: editable dropdown)
        grid.Add(wx.StaticText(self, label="Model:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.model_ctrl = wx.ComboBox(self, choices=POPULAR_MODELS,
                                       style=wx.CB_DROPDOWN)
        grid.Add(self.model_ctrl, 1, wx.EXPAND)

        # Max tokens
        grid.Add(wx.StaticText(self, label="Max Tokens:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.max_tokens_ctrl = wx.SpinCtrl(self, min=256, max=32768,
                                            initial=4096)
        grid.Add(self.max_tokens_ctrl, 0)

        main_sizer.Add(grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)

        # Test connection button
        test_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.test_btn = wx.Button(self, label="Test Connection")
        self.test_btn.Bind(wx.EVT_BUTTON, self._on_test)
        self.test_status = wx.StaticText(self, label="")
        test_sizer.Add(self.test_btn, 0, wx.RIGHT, 8)
        test_sizer.Add(self.test_status, 1, wx.ALIGN_CENTER_VERTICAL)
        main_sizer.Add(test_sizer, 0, wx.ALL, 12)

        # Validate question bank — runs the data-corruption audit and
        # surfaces a counts-only summary in a modal. Power-user tool;
        # the same script runs at install + launch already.
        validate_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.validate_btn = wx.Button(self, label="Validate Question Bank")
        self.validate_btn.Bind(wx.EVT_BUTTON, self._on_validate_bank)
        validate_sizer.Add(self.validate_btn, 0, wx.RIGHT, 8)
        validate_sizer.Add(
            wx.StaticText(self, label="Runs the corruption audit on the live bank."),
            1, wx.ALIGN_CENTER_VERTICAL,
        )
        main_sizer.Add(validate_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        # OK / Cancel
        btn_sizer = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 12)

        self.SetSizer(main_sizer)

        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

    def _load_values(self):
        self.api_key_ctrl.SetValue(self._config.get("api_key", ""))
        self.base_url_ctrl.SetValue(
            self._config.get("base_url", "https://openrouter.ai/api/v1"))
        self.model_ctrl.SetValue(
            self._config.get("model", LLM_MODEL))
        self.max_tokens_ctrl.SetValue(
            int(self._config.get("max_tokens", 4096)))

    def _on_ok(self, event):
        """Save settings and close."""
        save_llm_config(
            api_key=self.api_key_ctrl.GetValue().strip(),
            base_url=self.base_url_ctrl.GetValue().strip(),
            model=self.model_ctrl.GetValue().strip(),
            max_tokens=self.max_tokens_ctrl.GetValue(),
        )
        self.EndModal(wx.ID_OK)

    def _on_test(self, event):
        """Test the OpenRouter connection with current settings."""
        self.test_status.SetLabel("Testing...")
        self.test_btn.Disable()
        self.Update()

        import threading

        def worker():
            try:
                from openai import OpenAI
                client = OpenAI(
                    api_key=self.api_key_ctrl.GetValue().strip(),
                    base_url=self.base_url_ctrl.GetValue().strip(),
                )
                response = client.chat.completions.create(
                    model=self.model_ctrl.GetValue().strip(),
                    max_tokens=50,
                    messages=[
                        {"role": "user", "content": "Say 'hello' in one word."},
                    ],
                )
                reply = response.choices[0].message.content
                wx.CallAfter(self._show_test_result, True,
                             f"OK — model responded: {reply[:60]}")
            except Exception as e:
                wx.CallAfter(self._show_test_result, False, str(e)[:100])

        threading.Thread(target=worker, daemon=True).start()

    def _show_test_result(self, success, message):
        self.test_btn.Enable()
        if success:
            self.test_status.SetForegroundColour(wx.Colour(0, 128, 0))
            self.test_status.SetLabel(f"✓ {message}")
        else:
            self.test_status.SetForegroundColour(wx.Colour(200, 0, 0))
            self.test_status.SetLabel(f"✗ {message}")

    def _on_validate_bank(self, _):
        """Run the data-corruption audit and show the result in a dialog."""
        self.validate_btn.Disable()
        try:
            from scripts.audit_data_corruption import audit_database
            corruption, report = audit_database(include_retired=False)
            lines = [
                f"Total live questions: {report.get('total_questions', 0)}",
                f"  Verbal: {report.get('verbal_count', 0)}",
                f"  Quant:  {report.get('quant_count', 0)}",
                "",
                "Verbal classifications:",
            ]
            for cat, n in sorted(report.get("verbal_classifications", {}).items()):
                lines.append(f"  {cat}: {n}")
            quant_issues = report.get("quant_issues", {})
            if quant_issues:
                lines.append("")
                lines.append("Quant issues:")
                for k, n in sorted(quant_issues.items()):
                    lines.append(f"  {k}: {n}")
            if report.get("llm_artifacts"):
                lines.append("")
                lines.append(
                    f"LLM self-correction artifacts: {len(report['llm_artifacts'])}")
            verdict = "⚠️ Critical corruption detected." if corruption else "✅ Bank is clean."
            wx.MessageBox(
                verdict + "\n\n" + "\n".join(lines),
                "Question-bank validation",
                wx.OK | (wx.ICON_WARNING if corruption else wx.ICON_INFORMATION),
                parent=self,
            )
        except Exception as exc:
            wx.MessageBox(
                f"Audit failed: {exc}",
                "Validation error",
                wx.OK | wx.ICON_ERROR,
                parent=self,
            )
        finally:
            self.validate_btn.Enable()
