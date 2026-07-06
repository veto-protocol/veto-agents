"""Provider-agnostic structured-output LLM client.

One shared entry point — `structured_llm(...)` — that both the ad-buyer DECIDE
brain (agents/adbuyer/controller.py) and the creative DIRECTOR
(agents/adbuyer/creative/director.py) call to get a *validated dict* back from
whatever LLM the user actually has a key for.

Why this exists
---------------
Before this module both call sites hard-coded `anthropic.Anthropic(...)`. Once a
user picked a non-Claude provider in setup, `cfg.llm_model` held that provider's
model id (e.g. "gpt-4o-mini") and `cfg.llm_provider` was ignored — so the code
sent an OpenAI model id (404) or a non-Anthropic API key (401) straight to the
Anthropic SDK. This routes by the configured provider instead, auto-detecting
from whichever key is actually present when the provider is unset/unusable, and
never hands a non-Claude model or a non-Anthropic key to the Anthropic SDK.

Wire branches
-------------
Only two:
  • "anthropic"     — Claude only. messages.create + forced tool-use.
  • "openai_compat" — OpenAI, OpenRouter, Hermes, and any local/custom
                      OpenAI-compatible endpoint. chat.completions + forced
                      tool-call, differing only by base_url / api_key.

Both use forced tool-calling (not strict JSON schema) so the existing, un-touched
`_DECISION_SCHEMA` / `_CONCEPT_SCHEMA` — which have optional properties and no
`additionalProperties:false` — work as-is on both providers.

`anthropic` and `openai` are optional, lazily imported; a missing SDK raises a
`StructuredLLMError` with a pip hint.
"""

from __future__ import annotations

import json
from typing import Any

# The single source of truth for provider → env_var / endpoint / default_model.
from . import llm_providers


class StructuredLLMError(RuntimeError):
    """The LLM call failed, returned no structured result, or a dep is missing."""


class NoLLMKeyError(StructuredLLMError):
    """No provider key is configured or present — the caller should fall back to
    a friendly "set a key" path rather than treat this as a hard error."""


# Auto-detect order: the first env var that RESOLVES picks the provider. Mirrors
# the (now-removed) get_credential OR-chain, extended to the OpenAI-compatible
# providers in the registry. Anthropic first (best structured-output quality).
_AUTO_ORDER: list[tuple[str, str]] = [
    ("ANTHROPIC_API_KEY", "claude"),
    ("OPENAI_API_KEY", "openai"),
    ("OPENROUTER_API_KEY", "openrouter"),
    ("NOUS_API_KEY", "hermes"),
    ("XAI_API_KEY", "grok"),
    ("MOONSHOT_API_KEY", "kimi"),
    ("DEEPSEEK_API_KEY", "deepseek"),
    ("GEMINI_API_KEY", "gemini"),
]

# Placeholder api_key for keyless local endpoints (ollama / self-hosted) — the
# OpenAI SDK requires a non-empty string even when the server ignores it.
_LOCAL_PLACEHOLDER = "not-needed"

_ANTHROPIC_FALLBACK_MODEL = "claude-sonnet-4-6"  # repo convention; bump if desired


def _resolve_key(env_var: str, cfg) -> str | None:
    """Resolve one provider key via the studio's precedence resolver:
    shell env → ~/.veto/creative.env → veto-agents keychain.

    Imported at call time (not module top) so it stays patchable in tests and
    avoids an import-time dependency cycle with the adbuyer package.
    """
    from .agents.adbuyer.creative import creds as _creds

    return _creds.resolve(env_var, cfg)


class _Route:
    """The resolved routing decision for one structured_llm call."""

    __slots__ = ("provider", "kind", "base_url", "api_key", "model")

    def __init__(self, provider, kind: str, base_url: str | None,
                 api_key: str, model: str) -> None:
        self.provider = provider
        self.kind = kind            # "anthropic" | "openai_compat"
        self.base_url = base_url
        self.api_key = api_key
        self.model = model


def _select(cfg) -> _Route:
    """Pick provider + key + model, fixing both bug modes. Raises NoLLMKeyError
    when nothing is configured/present.

    1. Prefer the CONFIGURED provider (cfg.llm_provider, default "hermes") when
       its key resolves — OR it is keyless-local (env_var is None and a base_url
       is available).
    2. Else auto-detect by whichever key is actually present (_AUTO_ORDER).
    3. Else keyless LOCAL if cfg.llm_endpoint is set.
    4. Else NoLLMKeyError.

    Model rule (the fix): honor cfg.llm_model ONLY when it belongs to the
    provider we actually resolved; otherwise use that provider's default. And
    never hand a non-"claude" id to the Anthropic SDK.
    """
    configured_name = getattr(cfg, "llm_provider", None) or "hermes"
    configured = llm_providers.get(configured_name)
    endpoint_override = getattr(cfg, "llm_endpoint", None)

    resolved = None
    api_key: str | None = None

    # 1. Configured provider first.
    if configured is not None:
        if configured.env_var is None:
            # keyless-local (ollama / custom): usable if a base_url exists.
            if endpoint_override or configured.endpoint:
                resolved, api_key = configured, None
        else:
            k = _resolve_key(configured.env_var, cfg)
            if k:
                resolved, api_key = configured, k

    # 2. Auto-detect by whichever key is present.
    if resolved is None:
        for env_var, pname in _AUTO_ORDER:
            k = _resolve_key(env_var, cfg)
            if k:
                p = llm_providers.get(pname)
                if p is not None:
                    resolved, api_key = p, k
                    break

    # 3. Keyless LOCAL if an endpoint is configured.
    if resolved is None and endpoint_override:
        resolved, api_key = llm_providers.get("custom"), None

    if resolved is None:
        raise NoLLMKeyError(
            "no LLM key configured or present — set one with "
            "`veto-agents creds set OPENAI_API_KEY <key>` (or ANTHROPIC_API_KEY, "
            "NOUS_API_KEY, …), or point cfg.llm_endpoint at a local endpoint."
        )

    kind = "anthropic" if resolved.name == "claude" else "openai_compat"
    base_url = endpoint_override or resolved.endpoint  # openai_compat only
    if api_key is None:
        api_key = _LOCAL_PLACEHOLDER

    # 4. Model — honor the pin ONLY when it belongs to the resolved provider.
    if resolved.name == configured_name and getattr(cfg, "llm_model", None):
        model = cfg.llm_model
    else:
        model = resolved.default_model
    if not model:  # custom/local with no default → last-ditch fallback
        model = getattr(cfg, "llm_model", None) or "gpt-4o-mini"
    if kind == "anthropic" and not str(model).startswith("claude"):
        # belt-and-suspenders: a stale non-Claude id must never reach the
        # Anthropic SDK (that was the original 404).
        model = _ANTHROPIC_FALLBACK_MODEL

    return _Route(resolved, kind, base_url, api_key, str(model))


def _validate(data: Any, schema: dict, tools_name: str) -> dict:
    """Minimal, defensive shape check — both call sites re-parse fields anyway."""
    if not isinstance(data, dict):
        raise StructuredLLMError(f"{tools_name} result was not a JSON object")
    required = schema.get("required", []) or []
    missing = [k for k in required if k not in data]
    if missing:
        raise StructuredLLMError(
            f"{tools_name} result missing required keys: {', '.join(missing)}"
        )
    return data


def _anthropic_call(route: _Route, system: str, user: str, schema: dict,
                    tools_name: str, max_tokens: int) -> dict:
    try:
        import anthropic  # lazy — optional dep
    except ImportError as e:  # pragma: no cover - install hint
        raise StructuredLLMError(
            "the `anthropic` SDK is not installed — run "
            "`pip install 'veto-agents[media]'` (or `pip install anthropic`)."
        ) from e

    client = anthropic.Anthropic(api_key=route.api_key)
    resp = client.messages.create(
        model=route.model,
        max_tokens=max_tokens,
        system=system,
        tools=[{
            "name": tools_name,
            "description": "Return the structured result.",
            "input_schema": schema,
        }],
        tool_choice={"type": "tool", "name": tools_name},
        messages=[{"role": "user", "content": user}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            return _validate(dict(block.input), schema, tools_name)
    raise StructuredLLMError(f"{route.model} returned no tool_use block")


def _openai_call(route: _Route, system: str, user: str, schema: dict,
                 tools_name: str, max_tokens: int) -> dict:
    try:
        from openai import OpenAI  # lazy — optional dep
    except ImportError as e:  # pragma: no cover - install hint
        raise StructuredLLMError(
            "the `openai` SDK is not installed — run "
            "`pip install 'veto-agents[media]'` (or `pip install openai`)."
        ) from e

    client = OpenAI(api_key=route.api_key, base_url=route.base_url)
    resp = client.chat.completions.create(
        model=route.model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        tools=[{
            "type": "function",
            "function": {
                "name": tools_name,
                "description": "Return the structured result.",
                "parameters": schema,
            },
        }],
        tool_choice={"type": "function", "function": {"name": tools_name}},
    )
    choice = resp.choices[0]
    calls = getattr(choice.message, "tool_calls", None) or []
    if not calls:
        raise StructuredLLMError(f"{route.model} returned no tool call")
    try:
        args = json.loads(calls[0].function.arguments)
    except (TypeError, ValueError) as e:
        raise StructuredLLMError(f"{route.model} returned non-JSON arguments: {e}") from e
    return _validate(args, schema, tools_name)


def structured_llm(
    cfg,
    system: str,
    user: str,
    schema: dict,
    *,
    tools_name: str,
    max_tokens: int = 1024,
) -> dict:
    """Return a validated dict matching `schema` from the user's configured (or
    auto-detected) LLM provider, via forced tool-calling.

    Raises NoLLMKeyError when nothing is configured/present, StructuredLLMError
    on any other failure (missing SDK, no tool block, bad shape). Caveat: native
    non-OpenAI-compatible endpoints (e.g. Gemini's /v1beta) aren't routed here —
    only Anthropic + OpenAI-compatible base_urls. Add a per-provider adapter for
    those once their endpoint is the OpenAI-compat path.
    """
    route = _select(cfg)
    if route.kind == "anthropic":
        return _anthropic_call(route, system, user, schema, tools_name, max_tokens)
    return _openai_call(route, system, user, schema, tools_name, max_tokens)


def has_llm_key(cfg) -> bool:
    """True if some provider key resolves (or a local endpoint is set). Replaces
    the get_credential OR-chain the run loop used to gate the LLM brain."""
    try:
        _select(cfg)
        return True
    except NoLLMKeyError:
        return False
    except Exception:  # noqa: BLE001 — never let brain-selection crash the loop
        return False
