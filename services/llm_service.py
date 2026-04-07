"""
LLM service — OpenRouter integration (OpenAI-compatible API) with async support for wxPython.
Supports runtime configuration of provider, model, and API key.
"""
import json
import threading

from openai import OpenAI

from config import load_llm_config


class LLMService:
    """Thread-safe OpenRouter API client (OpenAI-compatible)."""

    def __init__(self):
        self._client = None
        self._config = None

    def _get_client(self):
        config = load_llm_config()
        # Recreate client if config changed
        if self._client is None or config != self._config:
            api_key = config.get("api_key", "")
            if not api_key:
                raise RuntimeError(
                    "OPENROUTER_API_KEY not set. Copy .env.example to .env and add your key, "
                    "or configure via Settings in the app."
                )
            self._client = OpenAI(
                api_key=api_key,
                base_url=config.get("base_url", "https://openrouter.ai/api/v1"),
            )
            self._config = config
        return self._client, config

    def generate(self, system_prompt, user_prompt, max_tokens=None):
        """
        Synchronous LLM call. Returns the response text.
        Use call_async() for non-blocking GUI integration.
        """
        client, config = self._get_client()
        response = client.chat.completions.create(
            model=config.get("model", "anthropic/claude-sonnet-4-20250514"),
            max_tokens=max_tokens or config.get("max_tokens", 4096),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.choices[0].message.content

    def generate_json(self, system_prompt, user_prompt, max_tokens=None):
        """
        Call LLM and parse response as JSON.
        The system prompt should instruct the model to return JSON.
        """
        raw = self.generate(system_prompt, user_prompt, max_tokens)
        # Extract JSON from potential markdown code fences
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)
        return json.loads(text)

    def call_async(self, system_prompt, user_prompt, callback,
                   max_tokens=None, parse_json=False):
        """
        Non-blocking LLM call for wxPython integration.
        Runs in a background thread; callback(result, error) is called
        when complete. Use wx.CallAfter in the callback to update GUI.

        Args:
            system_prompt: system message
            user_prompt: user message
            callback: function(result, error) — called from worker thread,
                      wrap GUI updates in wx.CallAfter
            max_tokens: optional override
            parse_json: if True, parse response as JSON
        """
        def worker():
            try:
                if parse_json:
                    result = self.generate_json(system_prompt, user_prompt, max_tokens)
                else:
                    result = self.generate(system_prompt, user_prompt, max_tokens)
                callback(result, None)
            except Exception as e:
                callback(None, e)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return thread

    def get_current_config(self):
        """Return the currently active LLM configuration (for display in settings UI)."""
        return load_llm_config()


# Singleton
llm_service = LLMService()
