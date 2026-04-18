"""
LLM service — OpenRouter integration (OpenAI-compatible API) with async support
for wxPython. Used for ALL runtime AI features (AWA scoring, AI tutor chat,
mistake coach, study plan).

Build-time data generation scripts in scripts/ use a separate LLM gateway client
and are not invoked from the running app.
"""
import json
import threading

from openai import OpenAI

from config import load_llm_config


class LLMService:
    """Thread-safe OpenRouter client (OpenAI-compatible)."""

    def __init__(self):
        self._client = None
        self._config = None

    def _get_client(self):
        config = load_llm_config()
        if self._client is None or config != self._config:
            api_key = config.get("api_key", "")
            if not api_key:
                raise RuntimeError(
                    "No LLM API key configured. Add an OpenRouter key via "
                    "Settings inside the app, or set OPENROUTER_API_KEY in .env."
                )
            self._client = OpenAI(
                api_key=api_key,
                base_url=config.get("base_url", "https://openrouter.ai/api/v1"),
            )
            self._config = config
        return self._client, config

    # ── Single-turn ─────────────────────────────────────────────────────

    def generate(self, system_prompt, user_prompt, max_tokens=None, model=None):
        """Synchronous single-turn call. Returns response text."""
        client, config = self._get_client()
        response = client.chat.completions.create(
            model=model or config.get("model", "anthropic/claude-opus-4"),
            max_tokens=max_tokens or config.get("max_tokens", 4096),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.choices[0].message.content

    def generate_json(self, system_prompt, user_prompt, max_tokens=None, model=None):
        """Single-turn call, parsed as JSON (strips markdown fences)."""
        raw = self.generate(system_prompt, user_prompt, max_tokens, model)
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)
        return json.loads(text)

    # ── Multi-turn ──────────────────────────────────────────────────────

    def chat(self, system_prompt, messages, max_tokens=None, model=None):
        """Multi-turn chat. messages is a list of {role, content} dicts.

        Used by AnswerChat (the per-question AI tutor) which keeps a running
        history of user follow-ups and assistant replies.
        """
        client, config = self._get_client()
        full_messages = [{"role": "system", "content": system_prompt}] + list(messages)
        response = client.chat.completions.create(
            model=model or config.get("model", "anthropic/claude-opus-4"),
            max_tokens=max_tokens or config.get("max_tokens", 4096),
            messages=full_messages,
        )
        return response.choices[0].message.content

    # ── Async wrappers ──────────────────────────────────────────────────

    def call_async(self, system_prompt, user_prompt, callback,
                   max_tokens=None, parse_json=False, model=None):
        """Non-blocking single-turn call. callback(result, error) fires from
        worker thread; wrap GUI updates in wx.CallAfter."""
        def worker():
            try:
                if parse_json:
                    result = self.generate_json(system_prompt, user_prompt,
                                                max_tokens, model)
                else:
                    result = self.generate(system_prompt, user_prompt,
                                           max_tokens, model)
                callback(result, None)
            except Exception as e:
                callback(None, e)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return thread

    def chat_async(self, system_prompt, messages, callback,
                   max_tokens=None, model=None):
        """Non-blocking multi-turn chat call."""
        def worker():
            try:
                result = self.chat(system_prompt, messages, max_tokens, model)
                callback(result, None)
            except Exception as e:
                callback(None, e)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return thread

    def get_current_config(self):
        return load_llm_config()


# Singleton
llm_service = LLMService()
