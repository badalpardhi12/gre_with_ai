"""
Abstract LLM client interface.

Pipeline stages depend on this interface only; the concrete adapter that
wraps the local-only model gateway is registered at runtime in
`services/synthetic/llm_adapter.py` (gitignored). Tests inject a stub
client so they never need network access.

Three role-named factory methods cover every stage's use case:

- `drafter()`   — high-temperature, large-output structured generation
- `solver()`    — cold-attempt with reasoning trace
- `judge()`     — JSON rubric scoring with conservative temperature

Each returns an `LLMClient` configured for that role; callers don't pick
model IDs directly. This keeps the per-role tuning (temp, max_tokens,
model choice) in one place and means swapping Opus -> Sonnet -> Gemini
for an entire role is a one-line change.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


@dataclass
class LLMResponse:
    """Uniform return value from any backend."""
    text: str
    parsed_json: Optional[Any] = None    # populated when caller requested JSON
    finish_reason: str = ""              # "stop" | "length" | …
    input_tokens: int = 0
    output_tokens: int = 0
    raw: Optional[Dict[str, Any]] = None  # backend-specific debug payload


class LLMClient(abc.ABC):
    """Abstract chat-completion client.

    Backends implement `complete` (text) and `complete_json` (parsed
    JSON, with one auto-retry on parse failure). The pipeline never
    calls a backend constructor directly — see `LLMClientFactory`.

    `model_alias` is the abstract name from the role config (e.g.,
    "opus", "sonnet", "gemini-pro"). The pipeline orchestrator uses it
    to enforce the no-self-grade rule (drafter family must not appear
    in the judge panel — see refinement plan §8).
    """

    role: str = "generic"
    name: str = "abstract"
    model_alias: str = ""

    @abc.abstractmethod
    def complete(
        self,
        messages: List[Dict[str, str]],
        system: Optional[str] = None,
        max_tokens: int = 2048,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        ...

    @abc.abstractmethod
    def complete_json(
        self,
        messages: List[Dict[str, str]],
        system: Optional[str] = None,
        max_tokens: int = 2048,
        temperature: Optional[float] = None,
        retries: int = 1,
    ) -> LLMResponse:
        ...


# ── Factory plumbing ─────────────────────────────────────────────────


# A registry pattern (instead of hard-imports) lets the local-only
# adapter live in a gitignored file. Tests register a stub backend in a
# fixture; production registers the real backend at import-time of the
# adapter module.
_REGISTRY: Dict[str, Callable[..., LLMClient]] = {}


def register_backend(name: str, factory: Callable[..., LLMClient]) -> None:
    """Bind a backend name -> client factory.

    `factory(role: str, **kwargs) -> LLMClient` is called by
    `LLMClientFactory.for_role`. Calling `register_backend` twice with
    the same name overrides the prior binding (useful in tests).
    """
    _REGISTRY[name] = factory


def get_backend(name: str) -> Callable[..., LLMClient]:
    if name not in _REGISTRY:
        raise KeyError(
            f"No LLM backend registered as {name!r}. "
            f"Known backends: {sorted(_REGISTRY)}. "
            "Did you import the local-only adapter?"
        )
    return _REGISTRY[name]


def list_backends() -> List[str]:
    return sorted(_REGISTRY)


class LLMClientFactory:
    """Build per-role clients from a single config dict.

    Per-role tuning lives in `roles`:

        factory = LLMClientFactory(
            backend="local",
            roles={
                "drafter": {"model": "opus", "temperature": 1.0},
                "solver_a": {"model": "sonnet", "temperature": 0.2},
                "solver_b": {"model": "gemini-pro", "temperature": 0.2},
                "judge_a": {"model": "opus", "temperature": 0.1},
                "judge_b": {"model": "sonnet", "temperature": 0.1},
                "judge_c": {"model": "gemini-pro", "temperature": 0.1},
                "ambiguity": {"model": "sonnet", "temperature": 0.2},
            },
        )

    The `backend` string is looked up in the registry; the per-role
    `kwargs` are passed through.
    """

    def __init__(self, backend: str, roles: Dict[str, Dict[str, Any]]):
        self.backend = backend
        self.roles = roles
        self._cache: Dict[str, LLMClient] = {}

    def for_role(self, role: str) -> LLMClient:
        if role not in self.roles:
            raise KeyError(
                f"Role {role!r} not configured. "
                f"Known roles: {sorted(self.roles)}"
            )
        if role in self._cache:
            return self._cache[role]
        factory = get_backend(self.backend)
        client = factory(role=role, **self.roles[role])
        client.role = role
        # Preserve the model alias for downstream no-self-grade checks.
        if "model" in self.roles[role]:
            client.model_alias = self.roles[role]["model"]
        self._cache[role] = client
        return client


# ── Default role plan ───────────────────────────────────────────────


# The plan-recommended distribution: Opus drafts, Sonnet+Gemini cross-
# check, all three judge. Models are abstract names; the local adapter
# maps them onto concrete IDs. Re-tunable per environment.
DEFAULT_ROLES: Dict[str, Dict[str, Any]] = {
    "drafter":   {"model": "opus", "temperature": 1.0, "max_tokens": 3000},
    "solver_a":  {"model": "sonnet", "temperature": 0.2, "max_tokens": 1500},
    "solver_b":  {"model": "gemini-pro", "temperature": 0.2, "max_tokens": 1500},
    "ambiguity": {"model": "sonnet", "temperature": 0.2, "max_tokens": 800},
    "judge_a":   {"model": "opus", "temperature": 0.1, "max_tokens": 1500},
    "judge_b":   {"model": "sonnet", "temperature": 0.1, "max_tokens": 1500},
    "judge_c":   {"model": "gemini-pro", "temperature": 0.1, "max_tokens": 1500},
}
