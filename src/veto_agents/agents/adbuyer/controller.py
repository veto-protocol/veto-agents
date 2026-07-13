"""Autonomous control loop for the ad-buyer agent.

The human deploys this ONCE with a standing GOAL and a policy, then walks away.
From then on the agent runs its own decision loop, and Veto is the ongoing
guardrail on the agent's OWN intent — not a per-command human consent gate.

Every ~interval minutes:

    OBSERVE   pull Meta insights + current campaigns / ad sets / budgets +
              account amount_spent / spend_cap.
    DECIDE    an LLM brain, given the GOAL + current performance, proposes a
              list of actions constrained to the v1 autonomy scope.
    DISCIPLINE a CODE-enforced ad-ops readiness gate (_is_actionable) runs
              BEFORE Veto: it HOLDS (does nothing) unless the ad set has left
              the learning phase, delivered enough days + data, and is past its
              per-entity cooldown. It also clamps any budget change to +/- a max
              percent of the current budget. Observing is cheap and frequent;
              acting is rare and only on stable, significant data. HOLD is the
              default, valid outcome. This layer is independent of the LLM.
    GOVERN    for EVERY action with a spend implication, call Veto authorize
              (decision_only) with the AGENT'S OWN rationale as context, BEFORE
              executing. allow -> execute; deny -> log + skip; escalate -> log +
              NOTIFY (console + receipt_url) + skip. An authorize exception
              SKIPS the action (fail-closed) — it never fails open.
    ACT       apply the one mutation via the MetaAdsClient.
    RECORD    print a per-action + per-cycle summary.
    sleep     ... then repeat forever.

AUTONOMY SCOPE v1 — the decide step may ONLY produce these action types, and
only on EXISTING entities:

    adjust_budget      raise / lower an ad set (or CBO campaign) daily_budget
    pause              pause a campaign / ad set / ad
    resume             resume a campaign / ad set / ad
    refresh_creative   generate a NEW image over x402 (fal_image) + swap it
                       into an existing creative's ad

It must NOT create brand-new campaigns / ad sets / ads. Any such proposed
action is rejected before execution.

Governance is fail-closed by construction: the whole point of the loop is that
a misbehaving brain cannot spend. Veto governs; Meta executes.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from rich.console import Console

from ...structured_llm import NoLLMKeyError, has_llm_key, structured_llm
from ...veto_client import VetoClient
from ..media.tools import fal_image  # REUSE the governed x402 creative tool
from . import meta_env
from .tools import meta_ads

# The merchant string passed to Veto for ad-budget authorization. Must match an
# entry in adbuyer's policy allowlist_merchants.
META_MERCHANT = "facebook.com"

# The v1 autonomy scope — the ONLY action types the brain may emit, and each
# only mutates an entity that already exists.
ALLOWED_ACTIONS = {"adjust_budget", "pause", "resume", "refresh_creative"}

# Default cadence between OBSERVE cycles. A real media buyer looks often but
# acts rarely — observing is cheap and safe, so the default is 6h and the
# readiness gate (not the interval) is what prevents premature action. This is
# only the fallback; the live default comes from ad_ops.observe_interval_minutes.
DEFAULT_INTERVAL_MIN = 360


# ─── ad-ops discipline ("patience") config + state ────────────────────────


@dataclass
class AdOpsConfig:
    """The discipline thresholds, loaded from policy.yaml's `ad_ops:` section.

    These do NOT move money — they make the loop behave like a real media buyer:
    observe often, act rarely, and only on stable, significant data. Every field
    is fail-soft (a missing/garbage value falls back to the default below).
    """

    respect_learning_phase: bool = True
    min_days_before_action: float = 4.0
    min_conversions_before_action: int = 50
    min_impressions_before_action: int = 2000
    cooldown_days_per_entity: float = 3.0
    max_budget_change_pct: float = 20.0
    observe_interval_minutes: int = DEFAULT_INTERVAL_MIN


def _as_bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(v, (int, float)):
        return bool(v)
    return default


def _as_float(v: Any, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if f >= 0 else default


def _as_int(v: Any, default: int) -> int:
    try:
        i = int(float(v))
    except (TypeError, ValueError):
        return default
    return i if i >= 0 else default


def _policy_paths() -> list[Path]:
    """Where to look for the adbuyer policy, best (user-edited) first."""
    paths: list[Path] = []
    try:
        from ... import config as cfg_module

        paths.append(cfg_module.policies_dir() / "adbuyer.yaml")
    except Exception:  # noqa: BLE001 — config import is best-effort
        pass
    paths.append(Path(__file__).with_name("policy.yaml"))  # bundled default
    return paths


def _read_policy_ad_ops() -> dict:
    """Return the first `ad_ops:` mapping we can find, or {} (→ defaults)."""
    import yaml

    for p in _policy_paths():
        try:
            if p and p.is_file():
                data = yaml.safe_load(p.read_text()) or {}
                if isinstance(data, dict) and isinstance(data.get("ad_ops"), dict):
                    return data["ad_ops"]
        except Exception:  # noqa: BLE001 — a bad policy file must never crash the loop
            continue
    return {}


def load_ad_ops() -> AdOpsConfig:
    """Load the ad-ops discipline config from policy.yaml, fail-soft to defaults."""
    raw = _read_policy_ad_ops()
    d = AdOpsConfig()
    if not isinstance(raw, dict):
        return d
    return AdOpsConfig(
        respect_learning_phase=_as_bool(
            raw.get("respect_learning_phase"), d.respect_learning_phase
        ),
        min_days_before_action=_as_float(
            raw.get("min_days_before_action"), d.min_days_before_action
        ),
        min_conversions_before_action=_as_int(
            raw.get("min_conversions_before_action"), d.min_conversions_before_action
        ),
        min_impressions_before_action=_as_int(
            raw.get("min_impressions_before_action"), d.min_impressions_before_action
        ),
        cooldown_days_per_entity=_as_float(
            raw.get("cooldown_days_per_entity"), d.cooldown_days_per_entity
        ),
        max_budget_change_pct=_as_float(
            raw.get("max_budget_change_pct"), d.max_budget_change_pct
        ),
        observe_interval_minutes=_as_int(
            raw.get("observe_interval_minutes"), d.observe_interval_minutes
        ),
    )


# -- per-entity cooldown state (persisted across runs) ----------------------
#
# ~/.veto/adbuyer_state.json maps entity_id -> ISO8601 timestamp of the last
# agent action on that entity. Read at cycle start; written ONLY after an action
# is allowed AND executed (never on HOLD/deny/dry-run). Every accessor is
# fail-soft: a missing or corrupt file just means "no cooldown history yet".


def _state_path() -> Path:
    return Path.home() / ".veto" / "adbuyer_state.json"


def _mock_state_path() -> Path:
    """Cooldown state for --mock runs — a SEPARATE file so the mimicked demo
    fully exercises the per-entity cooldown discipline without polluting (or
    being polluted by) the real production cooldown history."""
    return Path.home() / ".veto" / "adbuyer_state.mock.json"


def load_action_state(state_path: Path | None = None) -> dict[str, str]:
    """Read the per-entity last-action map. Fail-soft → {} on missing/corrupt."""
    p = state_path or _state_path()
    try:
        if not p.exists():
            return {}
        data = json.loads(p.read_text())
    except Exception:  # noqa: BLE001 — corrupt/unreadable state is not fatal
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def save_action_state(state: dict[str, str], state_path: Path | None = None) -> None:
    """Persist the per-entity last-action map. Fail-soft (never raises)."""
    p = state_path or _state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2, sort_keys=True))
    except Exception:  # noqa: BLE001 — cooldown is best-effort discipline
        pass


def _record_action(
    state: dict[str, str], entity_id: str, when: datetime,
    state_path: Path | None = None,
) -> None:
    """Stamp `entity_id`'s last-action time and persist immediately."""
    state[str(entity_id)] = when.astimezone(timezone.utc).isoformat()
    save_action_state(state, state_path)


def _parse_iso(s: Any) -> "datetime | None":
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ─── the action the brain proposes (validated into this shape) ────────────


@dataclass
class Action:
    """One validated, in-scope action the brain wants to take.

    `spend_implication_usd` is what we hand Veto as the authorize amount: the
    dollar figure the action commits Meta to. For a budget change it's the NEW
    daily budget (the ceiling the agent is asking to let Meta spend/day); for a
    refresh_creative it's the x402 image cost (gated a second time inside
    fal_image); for pause it's $0 (still logged, never a Meta spend).
    """

    type: str                         # one of ALLOWED_ACTIONS
    entity_id: str                    # existing campaign / ad set / ad id
    entity_level: str = "adset"       # "campaign" | "adset" | "ad"
    rationale: str = ""               # the AGENT'S OWN reasoning (Veto context)
    # adjust_budget
    new_budget_usd: float | None = None
    old_budget_usd: float | None = None
    # pause / resume
    target_status: str | None = None  # "PAUSED" | "ACTIVE"
    # refresh_creative
    creative_prompt: str | None = None
    creative_id: str | None = None    # the creative to rebuild an ad against
    spend_implication_usd: float = 0.0

    def describe(self) -> str:
        if self.type == "adjust_budget":
            old = f"${self.old_budget_usd:,.2f}" if self.old_budget_usd is not None else "?"
            new = f"${self.new_budget_usd:,.2f}" if self.new_budget_usd is not None else "?"
            return f"adjust_budget {self.entity_level} {self.entity_id}: {old}/day -> {new}/day"
        if self.type in ("pause", "resume"):
            return f"{self.type} {self.entity_level} {self.entity_id}"
        if self.type == "refresh_creative":
            return f"refresh_creative on ad {self.entity_id}"
        return f"{self.type} {self.entity_id}"


# ─── OBSERVE ──────────────────────────────────────────────────────────────


def observe(mc: "meta_ads.MetaAdsClient") -> dict[str, Any]:
    """Pull the current account + entity state. Fail-soft: any single read that
    errors is surfaced but doesn't abort the observation."""
    state: dict[str, Any] = {
        "account": {},
        "campaigns": [],
        "adsets": [],
        "ads": [],
        "insights": [],
        "errors": [],
        "currency": "USD",
    }

    try:
        acct = mc.get_account()
        currency = acct.get("currency", "USD") or "USD"
        state["currency"] = currency
        state["account"] = {
            "name": acct.get("name"),
            "currency": currency,
            "account_status": acct.get("account_status"),
            "amount_spent_usd": meta_ads.minor_to_usd(acct.get("amount_spent"), currency),
            "spend_cap_usd": meta_ads.minor_to_usd(acct.get("spend_cap"), currency),
            "balance_usd": meta_ads.minor_to_usd(acct.get("balance"), currency),
        }
    except meta_ads.MetaError as e:
        state["errors"].append(f"account: {e}")

    currency = state["currency"]

    try:
        state["campaigns"] = [
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "objective": c.get("objective"),
                "status": c.get("status"),
                "daily_budget_usd": meta_ads.minor_to_usd(c.get("daily_budget"), currency)
                if c.get("daily_budget")
                else None,
            }
            for c in mc.list_campaigns()
        ]
    except meta_ads.MetaError as e:
        state["errors"].append(f"campaigns: {e}")

    try:
        state["adsets"] = [
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "status": a.get("status"),
                "campaign_id": a.get("campaign_id"),
                "optimization_goal": a.get("optimization_goal"),
                "daily_budget_usd": meta_ads.minor_to_usd(a.get("daily_budget"), currency)
                if a.get("daily_budget")
                else None,
                # signals the ad-ops discipline gate reads (learning phase + age)
                "effective_status": a.get("effective_status"),
                "learning_stage_info": a.get("learning_stage_info"),
                "created_time": a.get("created_time"),
                "updated_time": a.get("updated_time"),
            }
            for a in mc.list_adsets()
        ]
    except meta_ads.MetaError as e:
        state["errors"].append(f"adsets: {e}")

    try:
        state["ads"] = [
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "status": a.get("status"),
                "adset_id": a.get("adset_id"),
                "creative": a.get("creative"),
            }
            for a in mc.list_ads()
        ]
    except meta_ads.MetaError as e:
        state["errors"].append(f"ads: {e}")

    try:
        # Account-level insights over the last 7 days — the performance signal
        # the brain reasons over (spend/cpc/ctr come back in DOLLARS).
        state["insights"] = mc.get_insights(
            level="adset", object_id=None, date_preset="last_7d",
            fields="adset_name,adset_id,spend,impressions,clicks,ctr,cpc,reach,actions",
        )
    except meta_ads.MetaError as e:
        state["errors"].append(f"insights: {e}")

    return state


# ─── DECIDE (LLM brain -> validated Actions) ──────────────────────────────


_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "description": (
                "Zero or more actions to take this cycle. Prefer FEWER, "
                "high-confidence changes. Empty list = hold steady."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": sorted(ALLOWED_ACTIONS),
                        "description": (
                            "adjust_budget (raise/lower an EXISTING ad set's "
                            "daily budget), pause, resume, or refresh_creative. "
                            "You may NOT create new campaigns/adsets/ads."
                        ),
                    },
                    "entity_id": {
                        "type": "string",
                        "description": "The id of an EXISTING entity from the observed state.",
                    },
                    "entity_level": {
                        "type": "string",
                        "enum": ["campaign", "adset", "ad"],
                        "description": "What kind of entity entity_id refers to.",
                    },
                    "new_budget_usd": {
                        "type": "number",
                        "description": "For adjust_budget: the new daily budget in USD.",
                    },
                    "target_status": {
                        "type": "string",
                        "enum": ["PAUSED", "ACTIVE"],
                        "description": "For pause -> PAUSED, resume -> ACTIVE.",
                    },
                    "creative_prompt": {
                        "type": "string",
                        "description": "For refresh_creative: the image prompt for the new creative.",
                    },
                    "rationale": {
                        "type": "string",
                        "description": (
                            "Your OWN reasoning for this action, grounded in the "
                            "observed performance and the goal. This is recorded "
                            "and shown to the governance layer."
                        ),
                    },
                },
                "required": ["type", "entity_id", "rationale"],
            },
        },
        "summary": {
            "type": "string",
            "description": "One line describing your read of performance this cycle.",
        },
    },
    "required": ["actions"],
}


def _adset_budget_index(state: dict) -> dict[str, float | None]:
    """entity_id -> current daily_budget_usd, for filling old_budget on a change."""
    idx: dict[str, float | None] = {}
    for a in state.get("adsets", []):
        if a.get("id"):
            idx[str(a["id"])] = a.get("daily_budget_usd")
    for c in state.get("campaigns", []):
        if c.get("id"):
            idx[str(c["id"])] = c.get("daily_budget_usd")
    return idx


def _validate_action(raw: dict, state: dict) -> Action | None:
    """Coerce one raw brain proposal into a validated in-scope Action, or None
    if it's out of scope / malformed. This is where we REJECT any attempt to
    create new entities or use an unknown action type."""
    a_type = str(raw.get("type", "")).strip().lower()
    if a_type not in ALLOWED_ACTIONS:
        return None
    entity_id = str(raw.get("entity_id", "")).strip()
    if not entity_id:
        return None

    # The entity MUST already exist in the observed state (v1 scope: no creation).
    known_ids = {str(c.get("id")) for c in state.get("campaigns", [])}
    known_ids |= {str(a.get("id")) for a in state.get("adsets", [])}
    known_ids |= {str(a.get("id")) for a in state.get("ads", [])}
    if entity_id not in known_ids:
        return None

    level = str(raw.get("entity_level", "")).strip().lower()
    if level not in ("campaign", "adset", "ad"):
        # infer from where the id was found
        if entity_id in {str(a.get("id")) for a in state.get("adsets", [])}:
            level = "adset"
        elif entity_id in {str(c.get("id")) for c in state.get("campaigns", [])}:
            level = "campaign"
        else:
            level = "ad"

    rationale = str(raw.get("rationale", "")).strip() or "(no rationale given)"
    budget_idx = _adset_budget_index(state)

    if a_type == "adjust_budget":
        try:
            new_budget = float(raw.get("new_budget_usd"))
        except (TypeError, ValueError):
            return None
        if new_budget <= 0:
            return None
        return Action(
            type="adjust_budget",
            entity_id=entity_id,
            entity_level=level if level != "ad" else "adset",
            rationale=rationale,
            new_budget_usd=round(new_budget, 2),
            old_budget_usd=budget_idx.get(entity_id),
            spend_implication_usd=round(new_budget, 2),
        )

    if a_type in ("pause", "resume"):
        status = "PAUSED" if a_type == "pause" else "ACTIVE"
        return Action(
            type=a_type,
            entity_id=entity_id,
            entity_level=level,
            rationale=rationale,
            target_status=status,
            spend_implication_usd=0.0,  # pause/resume is a $0 Meta write
        )

    if a_type == "refresh_creative":
        prompt = str(raw.get("creative_prompt", "")).strip()
        if not prompt:
            return None
        # creative x402 spend is gated a SECOND time inside fal_image; the
        # authorize here uses the model's list price as the implication.
        est = fal_image.estimate_cost("flux-schnell")
        return Action(
            type="refresh_creative",
            entity_id=entity_id,
            entity_level="ad",
            rationale=rationale,
            creative_prompt=prompt,
            spend_implication_usd=est,
        )

    return None


def decide(state: dict, goal: str, cfg) -> tuple[list[Action], str]:
    """LLM brain: given the goal + observed state, return validated Actions.

    Returns (actions, summary). On any brain error the loop stays alive — we
    return ([], "<reason>") so the cycle simply holds steady. The LLM is
    provider-agnostic: `structured_llm` routes to whichever provider/key the
    user has (Anthropic, OpenAI, OpenRouter, Hermes, local …), fixing the old
    "always call anthropic.Anthropic" 404/401.
    """
    adops = load_ad_ops()
    system_prompt = (
        "You are an autonomous Meta (Facebook/Instagram) ad buyer running in a "
        "24/7 control loop. You optimize toward a standing GOAL by adjusting the "
        "EXISTING campaigns/ad sets/ads you are given. You are strictly limited "
        "to four action types: adjust_budget (raise/lower an existing ad set's "
        "daily budget), pause, resume, and refresh_creative. You MUST NOT invent "
        "new campaigns, ad sets, or ads, and you MUST only reference entity ids "
        "present in the observed state. Every action needs a concrete rationale "
        "grounded in the performance data. A separate governance layer (Veto) "
        "will authorize or block each spend before it happens, so you do not need "
        "to self-censor budgets — just be honest about your reasoning.\n\n"
        "ACT LIKE A DISCIPLINED MEDIA BUYER, NOT A TWITCHY BOT:\n"
        f"• Respect Meta's LEARNING phase — do NOT adjust budget, resume, or "
        f"refresh the creative of an ad set still learning (learning_stage_info "
        f"status LEARNING). Let it stabilize.\n"
        f"• Don't act on thin data. Wait until an ad set has delivered at least "
        f"{adops.min_days_before_action:g} days AND has meaningful volume "
        f"(~{adops.min_conversions_before_action} conversions or "
        f"~{adops.min_impressions_before_action} impressions) before judging it.\n"
        f"• HOLDING is a good, valid outcome. Prefer FEWER, higher-confidence "
        f"changes; when in doubt, propose NO actions (empty list).\n"
        f"• Make budget changes GRADUAL — keep any single change within roughly "
        f"+/-{adops.max_budget_change_pct:g}% of the current daily budget "
        f"(larger jumps will be clamped downstream anyway).\n"
        f"• You may PAUSE a clearly-bad or runaway ad set at any time — that is "
        f"the one action that does not need to wait for the learning/data bar.\n"
        "• When proposing refresh_creative, the creative_prompt must fit the "
        "BRAND line (tone, colors, and never the forbidden items) when a BRAND "
        "line is present.\n"
        "A separate CODE layer enforces all of the above regardless of what you "
        "propose, so proposing patient, well-justified actions makes the loop "
        "smoother — reckless proposals will simply be held."
    )

    # Light brand context (advisory) so refresh_creative rationales/prompts fit
    # the brand. Governance + the code discipline layer stay authoritative.
    from .creative import brand as brand_mod
    _bp = brand_mod.load(cfg)
    brand_line = f"BRAND: {brand_mod.brand_summary_line(_bp)}\n\n" if _bp else ""

    user_prompt = (
        f"GOAL: {goal}\n\n"
        f"{brand_line}"
        f"OBSERVED STATE (last 7 days):\n{_state_for_prompt(state)}\n\n"
        "Decide what to do this cycle. Return zero or more in-scope actions."
    )

    try:
        decision = structured_llm(
            cfg, system=system_prompt, user=user_prompt,
            schema=_DECISION_SCHEMA, tools_name="emit_decision", max_tokens=1024,
        )
    except NoLLMKeyError:
        return [], (
            "no LLM key — run `veto-agents creds set ANTHROPIC_API_KEY <key>`; "
            "holding steady"
        )
    except Exception as e:  # noqa: BLE001 — brain failure must NOT kill the loop
        return [], f"brain error ({e}); holding steady"

    summary = str(decision.get("summary", "")).strip()
    raw_actions = decision.get("actions") or []
    if not isinstance(raw_actions, list):
        return [], summary or "brain returned no actions"

    actions: list[Action] = []
    for raw in raw_actions:
        if not isinstance(raw, dict):
            continue
        validated = _validate_action(raw, state)
        if validated is not None:
            actions.append(validated)
    return actions, summary


# ─── DECIDE (heuristic brain — used offline / with --mock and no LLM key) ──


def _f(v: Any, default: float = 0.0) -> float:
    """Parse a Meta insights string ('3.3012', '46.80') into a float, fail-soft."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _heuristic_decide(state: dict, adops: AdOpsConfig) -> tuple[list[Action], str]:
    """Pure-rules brain used when there is NO LLM key (e.g. --mock offline).

    No model call — just the media-buyer's obvious first-pass rules, returning
    the SAME validated Action shape as `decide()`:

      • a clear WINNER (out of learning, strong CTR)  -> adjust_budget +~20%
      • a clear LOSER  (out of learning, weak CTR + real spend) -> pause
      • anything still in LEARNING -> a naive scale proposal too

    The learning-phase ad set is proposed on DELIBERATELY: this brain is naive,
    so the CODE discipline gate (`_is_actionable`) is what actually HOLDS it —
    which is exactly the behavior the mock mode exists to demonstrate. Nothing
    here decides money; every proposal still passes through the discipline gate
    and then the Veto authorize gate before anything happens.
    """
    win_ctr = 2.0   # % — at/above this (out of learning) reads as a winner
    lose_ctr = 1.0  # % — at/below this with real spend reads as a loser
    step = 1.0 + max(0.0, adops.max_budget_change_pct) / 100.0  # e.g. +20%

    budget_idx = _adset_budget_index(state)
    actions: list[Action] = []
    for a in state.get("adsets", []):
        aid = str(a.get("id"))
        old = budget_idx.get(aid)
        row = _insights_row_for(state, aid)
        ctr = _f(row.get("ctr")) if isinstance(row, dict) else 0.0
        spend = _f(row.get("spend")) if isinstance(row, dict) else 0.0
        learning = _learning_status(a)

        if learning == "LEARNING":
            # naive scale — the discipline gate will HOLD this (that's the point)
            if old and old > 0:
                new = round(old * step, 2)
                actions.append(Action(
                    type="adjust_budget", entity_id=aid, entity_level="adset",
                    rationale=(
                        f"New ad set shows an early pulse ({ctr:.2f}% CTR); a naive "
                        f"buyer would scale it — but it is still in LEARNING."
                    ),
                    new_budget_usd=new, old_budget_usd=old, spend_implication_usd=new,
                ))
            continue

        if ctr >= win_ctr and old and old > 0:
            new = round(old * step, 2)
            actions.append(Action(
                type="adjust_budget", entity_id=aid, entity_level="adset",
                rationale=(
                    f"Clear winner: {ctr:.2f}% CTR, out of learning — scale the "
                    f"daily budget ~{adops.max_budget_change_pct:g}% to capture more."
                ),
                new_budget_usd=new, old_budget_usd=old, spend_implication_usd=new,
            ))
        elif ctr <= lose_ctr and spend > 0:
            actions.append(Action(
                type="pause", entity_id=aid, entity_level="adset",
                rationale=(
                    f"Clear loser: only {ctr:.2f}% CTR on ${spend:,.2f} spent, out "
                    f"of learning — pause it to stop the bleed."
                ),
                target_status="PAUSED", spend_implication_usd=0.0,
            ))

    summary = (
        "heuristic brain: scale the winner, pause the loser, and (naively) try to "
        "scale the learning ad set — the discipline gate will hold that one."
    )
    return actions, summary


def _state_for_prompt(state: dict) -> str:
    """Compact, LLM-friendly rendering of the observed state."""
    import json

    acct = state.get("account", {})
    lines = [
        "account: "
        + json.dumps(
            {
                "amount_spent_usd": acct.get("amount_spent_usd"),
                "spend_cap_usd": acct.get("spend_cap_usd"),
                "currency": acct.get("currency"),
                "status": acct.get("account_status"),
            }
        ),
        "campaigns: " + json.dumps(state.get("campaigns", [])),
        "adsets: " + json.dumps(state.get("adsets", [])),
        "ads: " + json.dumps(
            [{"id": a.get("id"), "name": a.get("name"), "status": a.get("status"),
              "adset_id": a.get("adset_id")} for a in state.get("ads", [])]
        ),
        "insights_last_7d: " + json.dumps(state.get("insights", [])),
    ]
    if state.get("errors"):
        lines.append("read_errors: " + json.dumps(state["errors"]))
    return "\n".join(lines)


# ─── DISCIPLINE ("patience") — the CODE-enforced readiness gate ────────────


def _find_adset(state: dict, entity_id: str) -> dict | None:
    for a in (state or {}).get("adsets", []):
        if str(a.get("id")) == str(entity_id):
            return a
    return None


def _insights_row_for(state: dict, entity_id: str) -> dict | None:
    for r in (state or {}).get("insights", []):
        if str(r.get("adset_id")) == str(entity_id):
            return r
    return None


def _parent_adset_id(state: dict, ad_id: str) -> "str | None":
    """Given an AD id, return its parent ad-set id (or None if unknown).

    The learning/age/data discipline is an AD-SET concept, but the brain may
    target an AD (refresh_creative always does; pause/resume may). Without this
    mapping an ad-level action would resolve to no ad-set record and silently
    fall through to the cooldown-only gate — bypassing the learning-phase and
    min-delivery checks. Resolving the parent ad set is what makes the CODE gate
    honor 'do not refresh/resume an ad set still learning'.
    """
    for a in (state or {}).get("ads", []):
        if str(a.get("id")) == str(ad_id):
            pid = a.get("adset_id")
            return str(pid) if pid else None
    return None


def _gate_entity_id(state: dict, action: "Action") -> str:
    """The id whose ad-set readiness we should check for `action`.

    For an ad-level action, that's the ad's PARENT ad set; otherwise the action's
    own entity_id. (The per-entity cooldown stays keyed on action.entity_id.)
    """
    if action.entity_level == "ad":
        parent = _parent_adset_id(state or {}, action.entity_id)
        if parent:
            return parent
    return action.entity_id


def _learning_status(entity: dict | None) -> str | None:
    """The ad set's learning_stage_info.status (upper-cased), or None."""
    if not isinstance(entity, dict):
        return None
    info = entity.get("learning_stage_info")
    if isinstance(info, dict) and info.get("status"):
        return str(info["status"]).strip().upper()
    return None


def _parse_meta_time(s: Any) -> "datetime | None":
    """Parse Meta's created_time ('2026-06-01T12:34:56+0000') → aware datetime."""
    if not s or not isinstance(s, str):
        return None
    txt = s.strip().replace("Z", "+00:00")
    # Meta uses a +0000 offset with no colon; normalize for fromisoformat.
    if len(txt) >= 5 and txt[-5] in "+-" and txt[-3] != ":":
        txt = txt[:-2] + ":" + txt[-2:]
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _entity_age_days(entity: dict | None, now: datetime) -> "float | None":
    if not isinstance(entity, dict):
        return None
    dt = _parse_meta_time(entity.get("created_time"))
    if dt is None:
        return None
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def _insights_impressions(row: dict | None) -> int:
    if not isinstance(row, dict):
        return 0
    try:
        return int(float(row.get("impressions", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _insights_conversions(row: dict | None) -> int:
    """Sum the `actions` list into a single conversions/actions count."""
    if not isinstance(row, dict):
        return 0
    actions = row.get("actions")
    total = 0
    if isinstance(actions, list):
        for a in actions:
            if not isinstance(a, dict):
                continue
            try:
                total += int(float(a.get("value", 0) or 0))
            except (TypeError, ValueError):
                continue
    return total


def _is_actionable(
    action: "Action",
    entity: dict | None,
    insights: dict | None,
    adops: AdOpsConfig,
    last_action_at: "datetime | None",
    now: datetime,
) -> tuple[bool, str]:
    """The ad-ops readiness gate. Returns (ok, reason).

    ok=False means HOLD (do nothing). ok=True means the action has cleared the
    discipline bar and may proceed to the Veto authorize gate. This is enforced
    in CODE, independent of the LLM — the brain's proposals are advisory; this
    gate is the real "patience".

    An action passes only if ALL hold:
      (a) if respect_learning_phase, the ad set is NOT in LEARNING;
      (b) it has delivered >= min_days_before_action days AND meets
          min_conversions_before_action OR min_impressions_before_action;
      (c) it is past cooldown_days_per_entity since the last agent action.

    `pause` is EXEMPT from (a) and (b) — killing a clearly-bad or runaway ad set
    must be possible anytime — but still respects the cooldown (c).
    """
    # (c) cooldown — applies to EVERY action type, pause included.
    if last_action_at is not None:
        cooldown = timedelta(days=max(0.0, adops.cooldown_days_per_entity))
        elapsed = now - last_action_at
        if elapsed < cooldown:
            days_since = elapsed.total_seconds() / 86400.0
            days_left = (cooldown - elapsed).total_seconds() / 86400.0
            return False, (
                f"cooldown active — last agent action {days_since:.1f}d ago; "
                f"{days_left:.1f}d left of the {adops.cooldown_days_per_entity:g}d "
                f"per-entity cooldown"
            )

    # pause is exempt from the learning/data checks (past the cooldown above).
    if action.type == "pause":
        return True, "pause allowed (exempt from learning/data checks; past cooldown)"

    # The learning/data gate is an AD-SET concept. If we have no ad-set record
    # for this entity (e.g. a campaign-level CBO budget change), we can't read
    # learning phase / age / delivery — fall back to the cooldown-only discipline
    # already applied above rather than block it forever.
    if not isinstance(entity, dict):
        return True, "no ad-set readiness data (campaign-level or unknown) — cooldown-only gate"

    # (a) learning phase.
    if adops.respect_learning_phase and _learning_status(entity) == "LEARNING":
        return False, "ad set still in LEARNING phase — waiting for it to stabilize before acting"

    # (b) minimum delivery: age AND (conversions OR impressions).
    age_days = _entity_age_days(entity, now)
    if age_days is None:
        return False, "ad set age unknown (no created_time) — holding until delivery is measurable"
    if age_days < adops.min_days_before_action:
        return False, (
            f"ad set only {age_days:.1f}d old (< {adops.min_days_before_action:g}d min) "
            f"— too early to judge"
        )
    impressions = _insights_impressions(insights)
    conversions = _insights_conversions(insights)
    if not (
        conversions >= adops.min_conversions_before_action
        or impressions >= adops.min_impressions_before_action
    ):
        return False, (
            f"thin data — {conversions} conv (need {adops.min_conversions_before_action}) "
            f"and {impressions} impr (need {adops.min_impressions_before_action}); "
            f"one threshold must be met"
        )
    return True, (
        f"ready — {age_days:.1f}d old, {impressions} impr, {conversions} conv, "
        f"past {adops.cooldown_days_per_entity:g}d cooldown"
    )


def _apply_magnitude_cap(
    action: "Action", adops: AdOpsConfig, console: Console
) -> bool:
    """Clamp an adjust_budget's new daily budget to within +/- max_budget_change_pct
    of the CURRENT budget, in place. Clamp (not reject) — log if we clamped.

    Returns True if the action may proceed to the Veto gate. Returns False (→ the
    caller HOLDs) for an adjust_budget whose CURRENT budget we couldn't read:
    without a base the +/-% band is undefined, and letting an unbounded change
    through would defeat the whole magnitude discipline. HOLD is the safe default.
    Non-budget actions always return True.
    """
    if action.type != "adjust_budget":
        return True
    old = action.old_budget_usd
    proposed = action.new_budget_usd
    if not old or old <= 0 or proposed is None:
        # No known current budget → the +/-% band is undefined. Never authorize
        # or write an unclamped budget change; signal the caller to HOLD.
        return False
    pct = max(0.0, adops.max_budget_change_pct) / 100.0
    lo = old * (1.0 - pct)
    hi = old * (1.0 + pct)
    # Clamp to the band, then quantize to whole cents WITHOUT ever leaving it.
    # Plain round() can push a value a hair past the cap — e.g. $85.98 * 1.20 =
    # $103.176 rounds to $103.18, which is +20.005%, breaching the magnitude
    # discipline. Floor the upper edge (and ceil the lower edge) so the WRITTEN
    # budget is always within +/-pct of the current one — a spend cap must never
    # round a change upward past its own limit.
    lo_cents = math.ceil(lo * 100 - 1e-9)
    hi_cents = math.floor(hi * 100 + 1e-9)
    proposed_cents = round(min(hi, max(lo, proposed)) * 100)
    clamped_cents = min(hi_cents, max(lo_cents, proposed_cents))
    clamped = clamped_cents / 100.0
    if abs(clamped - proposed) > 0.005:
        console.print(
            f"    [yellow]capped to +/-{adops.max_budget_change_pct:g}%[/yellow] "
            f"· proposed ${proposed:,.2f}/day → ${clamped:,.2f}/day "
            f"(from ${old:,.2f}/day)"
        )
        action.new_budget_usd = clamped
        action.spend_implication_usd = clamped
    return True


# ─── GOVERN + ACT (per-action Veto gate, fail-closed) ─────────────────────


@dataclass
class ActionOutcome:
    """The full result of gating ONE action through discipline → Veto → act.

    `govern_and_execute` used to return only the coarse `outcome` string and
    PRINT the verdict/receipt. That's fine for the console daemon, but a
    structured caller (the MCP server) needs the detail the daemon printed:
    the Veto verdict, its reason codes, the receipt URL, the clamped budget,
    and the discipline HOLD reason. This carries all of it WITHOUT moving any
    gate logic — the string is still `outcome`, everything else is decoration.
    """

    outcome: str                        # executed|held|denied|escalated|skipped|dry-run|failed
    type: str
    entity_id: str
    entity_level: str
    rationale: str
    verdict: str | None = None          # allow|deny|escalate | None (held/refused pre-Veto)
    reason_codes: list[str] = field(default_factory=list)
    receipt_url: str | None = None
    applied: bool = False               # True only when the Meta write actually happened
    reason: str | None = None           # discipline HOLD reason, or a refuse/skip reason
    old_budget_usd: float | None = None
    new_budget_usd: float | None = None  # post-clamp


def _show_verdict(res, console: Console, *, label: str) -> None:
    """Print the Veto verdict + receipt URL. Governance is shown on the GOOD path
    too: an allow prints a clear green line, not just deny/escalate — so a demo
    viewer sees Veto approving every autonomous spend, not only blocking."""
    if res.receipt_url:
        console.print(f"    [dim]receipt: {res.receipt_url}[/dim]")
    if res.verdict == "allow":
        console.print("    [green]Veto: allowed ✓[/green]")
    else:
        codes = ", ".join(res.reason_codes) if res.reason_codes else "(no reason codes)"
        console.print(f"    [red]x {label}: {res.verdict}[/red] · {codes}")


def govern_and_execute(
    action: Action,
    client: VetoClient,
    mc: "meta_ads.MetaAdsClient | None",
    cfg,
    console: Console,
    *,
    meta: dict,
    goal: str,
    dry_run: bool = False,
    state: dict | None = None,
    adops: AdOpsConfig | None = None,
    action_state: dict[str, str] | None = None,
    action_state_path: Path | None = None,
    now: datetime | None = None,
) -> ActionOutcome:
    """Gate ONE action through the discipline gate, then Veto, then (unless
    dry_run) apply the Meta write.

    Returns an `ActionOutcome` whose `.outcome` is the short string the daemon
    used to return ("executed" | "held" | "denied" | "escalated" | "skipped" |
    "dry-run" | "failed") plus the full detail a structured caller needs (Veto
    verdict, reason codes, receipt URL, clamped budget, hold reason). The
    daemon reads `.outcome`; the MCP server reads the rest. No gate logic moved.

    Order of gates (both BEFORE any Meta write):
      1. ad-ops DISCIPLINE (_is_actionable) — CODE-enforced patience,
         independent of the LLM. If the ad set is still learning, hasn't
         delivered enough days/data, or is inside its per-entity cooldown, HOLD:
         log "holding: <reason>" and skip. `adjust_budget` amounts are also
         clamped here to +/- max_budget_change_pct of the current budget.
      2. Veto authorize — fail-closed money gate on the (clamped) amount.

    Fail-closed contract:
      * every action with a spend implication is authorized BEFORE any Meta
        write.
      * allow  -> execute (or, in dry_run, log and skip the write).
      * deny   -> log "Veto blocked" + skip.
      * escalate -> log + NOTIFY (console line + receipt_url) + skip. The loop
        is NEVER frozen.
      * an authorize EXCEPTION skips the action (never fails open).

    The per-entity cooldown is stamped in `action_state` (and persisted) ONLY
    after an action is allowed AND executed — never on HOLD/deny/dry-run.
    """
    console.print(f"  [bold cyan]->[/bold cyan] {action.describe()}")
    console.print(f"    [dim]rationale:[/dim] {action.rationale}")

    label = action.type

    def _outcome(outcome: str, **kw) -> ActionOutcome:
        """Build the structured result, stamping the action's identity + the
        (possibly clamped) budget as read from `action` AT THIS point. Every
        `return` below goes through here so the daemon's `.outcome` string and
        the MCP caller's detail stay in lock-step."""
        return ActionOutcome(
            outcome=outcome,
            type=action.type,
            entity_id=action.entity_id,
            entity_level=action.entity_level,
            rationale=action.rationale,
            old_budget_usd=action.old_budget_usd,
            new_budget_usd=action.new_budget_usd,
            **kw,
        )

    # ── HARD scope enforcement (independent of the LLM/decide path) ───────
    # Defense-in-depth: even if an Action is constructed outside _validate_action
    # (a refactor, a direct caller, a future code path), an out-of-scope action
    # type MUST NEVER reach an authorize or a Meta write. Refuse it here, before
    # anything with a side effect.
    if action.type not in ALLOWED_ACTIONS:
        console.print(
            f"    [red]x out-of-scope action '{action.type}' — refused before "
            f"authorize (allowed: {', '.join(sorted(ALLOWED_ACTIONS))}).[/red]"
        )
        return _outcome(
            "skipped",
            reason=f"out-of-scope action '{action.type}' refused before authorize",
        )
    if not str(action.entity_id or "").strip():
        console.print("    [red]x action has no entity_id — refused.[/red]")
        return _outcome("skipped", reason="action has no entity_id")
    # refresh_creative needs a Facebook Page (creative upload requires
    # page_id). A user without META_PAGE_ID can still run the whole loop —
    # budgets, pause/resume — we just refuse the one action needing a page.
    if action.type == "refresh_creative" and not getattr(mc, "page_id", None):
        console.print(
            "    [yellow]holding: refresh_creative needs META_PAGE_ID "
            "(a Facebook Page) — not configured; skipping.[/yellow]"
        )
        return _outcome("held", reason="refresh_creative unavailable: no META_PAGE_ID configured")

    # ── ad-ops DISCIPLINE gate (CODE-enforced patience, BEFORE Veto) ──────
    if adops is None:
        adops = load_ad_ops()
    if action_state is None:
        action_state = load_action_state(action_state_path)
    if now is None:
        now = datetime.now(timezone.utc)

    # The learning/age/data gate reads the ad SET; for an ad-level action that's
    # the ad's PARENT ad set (else refresh_creative/resume-on-an-ad would skip
    # the learning-phase check). The cooldown stays keyed on the action's own id.
    gate_id = _gate_entity_id(state or {}, action)
    entity = _find_adset(state or {}, gate_id)
    insights_row = _insights_row_for(state or {}, gate_id)
    last_action_at = _parse_iso(action_state.get(action.entity_id))
    ok, reason = _is_actionable(action, entity, insights_row, adops, last_action_at, now)
    if not ok:
        console.print(f"    [yellow]holding: {reason}[/yellow]")
        return _outcome("held", reason=reason)
    console.print(f"    [dim]ready:[/dim] {reason}")

    # ── magnitude cap: clamp a budget change to +/- max_budget_change_pct ──
    # (authorize the CLAMPED amount — mutates action in place before Veto sees it).
    # If the current budget is unreadable, the band is undefined → HOLD, so an
    # unclamped budget change can never reach Veto or Meta.
    if not _apply_magnitude_cap(action, adops, console):
        console.print(
            f"    [yellow]holding: can't read the current budget for "
            f"{action.entity_level} {action.entity_id} — the "
            f"+/-{adops.max_budget_change_pct:g}% magnitude discipline can't be "
            f"enforced, so no change is made.[/yellow]"
        )
        return _outcome(
            "held",
            reason=(
                f"can't read current budget for {action.entity_level} "
                f"{action.entity_id} — +/-{adops.max_budget_change_pct:g}% "
                f"magnitude discipline can't be enforced"
            ),
        )

    # ── Veto gate (fail-closed) ──────────────────────────────────────────
    try:
        res = client.authorize(
            agent_id=cfg.agent_id,
            action="payment",
            merchant=META_MERCHANT,
            amount=action.spend_implication_usd,
            currency="USD",
            description=action.describe(),
            context={
                "tool": "meta.ads",
                "decision": action.type,
                "autonomous": True,
                "goal": goal[:200],
                "ad_account_id": meta.get("ad_account_id"),
                "entity_id": action.entity_id,
                "entity_level": action.entity_level,
                "old_budget_usd": action.old_budget_usd,
                "new_budget_usd": action.new_budget_usd,
                "target_status": action.target_status,
                # The AGENT'S OWN rationale is the intent Veto governs.
                "rationale": action.rationale[:500],
            },
        )
    except Exception as e:  # noqa: BLE001 — fail CLOSED: skip, never fail open
        console.print(f"    [red]x authorize error — skipping (fail-closed):[/red] {e}")
        return _outcome("skipped", reason=f"authorize error (fail-closed): {e}")

    _show_verdict(res, console, label=label)

    if res.verdict == "deny":
        console.print("    [yellow]Veto blocked — skipping this action.[/yellow]")
        return _outcome(
            "denied", verdict=res.verdict,
            reason_codes=list(res.reason_codes or []), receipt_url=res.receipt_url,
        )
    if res.verdict == "escalate":
        # NOTIFY, then skip — never freeze the loop.
        console.print(
            "    [magenta]! escalated to a human — skipping this action; "
            "the loop continues.[/magenta]"
        )
        if res.receipt_url:
            console.print(f"    [magenta]  review: {res.receipt_url}[/magenta]")
        return _outcome(
            "escalated", verdict=res.verdict,
            reason_codes=list(res.reason_codes or []), receipt_url=res.receipt_url,
        )
    if res.verdict != "allow":
        console.print(f"    [yellow]unknown verdict '{res.verdict}' — skipping.[/yellow]")
        return _outcome(
            "skipped", verdict=res.verdict,
            reason_codes=list(res.reason_codes or []), receipt_url=res.receipt_url,
            reason=f"unknown verdict '{res.verdict}'",
        )

    # ── allowed → ACT (skip the Meta write in dry_run) ───────────────────
    if dry_run:
        console.print("    [dim](dry-run) allowed — skipping Meta write.[/dim]")
        return _outcome(
            "dry-run", verdict=res.verdict,
            reason_codes=list(res.reason_codes or []), receipt_url=res.receipt_url,
        )

    if mc is None:
        console.print("    [yellow]no Meta client — skipping write.[/yellow]")
        return _outcome(
            "skipped", verdict=res.verdict,
            reason_codes=list(res.reason_codes or []), receipt_url=res.receipt_url,
            reason="no Meta client — write skipped",
        )

    try:
        wrote = _apply(action, mc, cfg, console, meta=meta)
    except meta_ads.MetaError as e:
        console.print(f"    [red]x Meta write failed:[/red] {e}")
        return _outcome(
            "failed", verdict=res.verdict,
            reason_codes=list(res.reason_codes or []), receipt_url=res.receipt_url,
            reason=f"Meta write failed: {e}",
        )
    except Exception as e:  # noqa: BLE001 — a bad write must not kill the loop
        console.print(f"    [red]x action failed:[/red] {e}")
        return _outcome(
            "failed", verdict=res.verdict,
            reason_codes=list(res.reason_codes or []), receipt_url=res.receipt_url,
            reason=f"action failed: {e}",
        )
    if not wrote:
        # e.g. a refresh_creative whose x402 image spend was denied/failed by
        # Veto/fal — nothing was applied. Don't misreport it as executed.
        return _outcome(
            "failed", verdict=res.verdict,
            reason_codes=list(res.reason_codes or []), receipt_url=res.receipt_url,
            reason="downstream governed spend (creative) denied/failed — nothing applied",
        )
    # Stamp the per-entity cooldown ONLY now that the action truly executed.
    _record_action(action_state, action.entity_id, now, action_state_path)
    console.print("    [green]done.[/green]")
    return _outcome(
        "executed", verdict=res.verdict, applied=True,
        reason_codes=list(res.reason_codes or []), receipt_url=res.receipt_url,
    )


def _apply(
    action: Action,
    mc: "meta_ads.MetaAdsClient",
    cfg,
    console: Console,
    *,
    meta: dict,
) -> bool:
    """Execute the (already-authorized) Meta write for one action.

    Returns True if a Meta write was actually applied, False if the action was
    a no-op because a downstream governed spend (the x402 creative) was denied
    or failed — so the caller never reports a non-write as "executed".
    """
    if action.type == "adjust_budget":
        cents = meta_ads.usd_to_minor(action.new_budget_usd or 0.0)
        if action.entity_level == "campaign":
            mc.update_campaign_budget(action.entity_id, cents)
        else:
            mc.update_adset_budget(action.entity_id, cents)
        return True

    if action.type in ("pause", "resume"):
        mc.set_status(action.entity_id, action.target_status or "PAUSED")
        return True

    if action.type == "refresh_creative":
        # Generate a fresh image over x402 (gated a 2nd time inside fal_image),
        # then swap it into a NEW creative attached to the existing ad — a
        # no-cost Meta write.
        console.print("    [cyan]generating new creative over x402 (fal.ai) …[/cyan]")
        cr = fal_image.generate(
            prompt=action.creative_prompt or "ad creative refresh",
            model="flux-schnell", cfg=cfg,
        )
        if cr.denied:
            console.print(f"    [red]x creative blocked by Veto[/red] · {cr.error}")
            if cr.receipt_url:
                console.print(f"    [dim]receipt: {cr.receipt_url}[/dim]")
            return False
        if not cr.ok:
            console.print(f"    [red]x creative generation failed[/red] · {cr.error}")
            return False
        console.print(
            f"    [green]creative ready[/green] · ${cr.actual_cost_usd:.4f} · "
            f"[cyan]{cr.output_path}[/cyan]"
        )
        if cr.receipt_url:
            console.print(f"    [dim]receipt: {cr.receipt_url}[/dim]")

        image_hash = mc.upload_image(
            image_url=cr.output_url,
            image_path=cr.output_path if not cr.output_url else None,
        )
        new_creative_id = mc.create_creative(
            name=f"refresh {action.entity_id}",
            image_hash=image_hash,
            link=(meta.get("landing_url") or "https://example.com/landing"),
            message=(action.creative_prompt or "")[:120],
            call_to_action_type="LEARN_MORE",
        )
        # Swap the new creative onto the existing ad (a no-cost write).
        mc.set_status(action.entity_id, "PAUSED")  # safe: don't serve mid-swap
        mc._post(action.entity_id, data={"creative": _json_obj({"creative_id": new_creative_id})})
        console.print(f"    [green]swapped creative[/green] {new_creative_id} onto ad {action.entity_id}")
        return True

    # Unknown type reached _apply despite the scope gate — never write.
    return False


def _json_obj(obj: dict) -> str:
    import json

    return json.dumps(obj)


# ─── run_loop (the deploy-once daemon) ────────────────────────────────────


def _preflight(cfg, console: Console, *, mock: bool = False) -> tuple[Any, dict] | None:
    """Sign-in + Meta-creds pre-flight. Returns (cfg, meta) or None to abort.

    Sign-in is ALWAYS required (mock mode still calls REAL Veto authorize —
    decision_only, free — so the mimicked demo exercises real governance). Only
    the Meta-credentials requirement is skipped when `mock`: there is no real
    Meta account, so we hand back a synthetic account id for the mimicked world.
    """
    if not (cfg.api_key and cfg.agent_id):
        from ...auth_gate import require_signin

        cfg = require_signin(console, cfg)
        if not (cfg.api_key and cfg.agent_id):
            return None

    if mock:
        from .tools.mock_meta import DEFAULT_ACCOUNT_ID

        return cfg, {
            "access_token": "MOCK",
            "ad_account_id": DEFAULT_ACCOUNT_ID,
            "page_id": "1029384756",
            "landing_url": "https://example.com/landing",
        }

    meta = meta_env.load_meta(cfg)
    absent = meta_env.missing(meta)
    if absent:
        console.print(
            "[yellow]Meta credentials missing:[/yellow] "
            + ", ".join(absent)
            + "\n  Set them in [cyan]~/.veto/meta.env[/cyan] (KEY=VALUE lines):\n"
            "    [dim]META_ACCESS_TOKEN=...   (System-User token, ads_management + ads_read)\n"
            "    META_AD_ACCOUNT_ID=act_1234567890   (a SANDBOX account is recommended)\n"
            "    META_PAGE_ID=1029384756[/dim]\n"
            "  [dim]Sandbox accounts use the same API and never spend — perfect for the demo.[/dim]\n"
        )
        return None
    return cfg, meta


def make_meta_client(meta: dict, *, mock: bool = False):
    """Factory for the OBSERVE/ACT client. `mock=True` returns the offline
    `MockMetaAdsClient` (mimicked campaigns, no network, no spend); otherwise the
    real Graph API `MetaAdsClient`. Injected so --mock swaps the whole data
    surface without any other change to the loop."""
    if mock:
        from .tools.mock_meta import MockMetaAdsClient

        return MockMetaAdsClient(meta)
    return meta_ads.MetaAdsClient(meta)


def _run_cycle(
    n: int,
    cfg,
    console: Console,
    client: VetoClient,
    meta: dict,
    goal: str,
    dry_run: bool,
    *,
    mc=None,
    use_heuristic: bool = False,
    action_state_path: Path | None = None,
) -> None:
    """One OBSERVE -> DECIDE -> GOVERN+ACT -> RECORD cycle.

    `mc` (optional): a pre-built Meta client to REUSE across cycles — the mock
    client carries the mimicked world (evolving insights + budget/status
    mutations), so it must persist. When None, a real client is built (and
    closed) for this cycle. `use_heuristic` selects the pure-rules brain (no LLM
    key). `action_state_path` overrides the per-entity cooldown file (--mock
    keeps its own).
    """
    console.print(f"\n[bold]— cycle {n} —[/bold]  [dim]goal:[/dim] {goal}")

    # OBSERVE
    owns_mc = mc is None
    if owns_mc:
        try:
            mc = meta_ads.MetaAdsClient(meta)
        except meta_ads.MetaError as e:
            console.print(f"  [red]x Meta client error — skipping cycle:[/red] {e}")
            return
    try:
        state = observe(mc)
        acct = state.get("account", {})
        console.print(
            "  [cyan]observed[/cyan] · "
            f"campaigns={len(state.get('campaigns', []))} "
            f"adsets={len(state.get('adsets', []))} "
            f"ads={len(state.get('ads', []))} · "
            f"spent=${(acct.get('amount_spent_usd') or 0):,.2f} "
            f"cap=${(acct.get('spend_cap_usd') or 0):,.2f}"
        )
        _render_observed_adsets(state, console)
        for err in state.get("errors", []):
            console.print(f"    [yellow]! read:[/yellow] {err}")

        # DECIDE — heuristic (no LLM key / --no-llm) or the real LLM brain.
        if use_heuristic:
            actions, summary = _heuristic_decide(state, load_ad_ops())
        else:
            actions, summary = decide(state, goal, cfg)
        if summary:
            console.print(f"  [dim]brain:[/dim] {summary}")
        if not actions:
            console.print("  [dim]no in-scope actions this cycle — holding steady.[/dim]")
            return

        # GOVERN + ACT (per-action: DISCIPLINE gate, then Veto, fail-closed)
        # Read the ad-ops config + per-entity cooldown state ONCE at cycle start;
        # `now` is a single reference clock for the whole cycle's gate checks.
        adops = load_ad_ops()
        action_state = load_action_state(action_state_path)
        now = datetime.now(timezone.utc)
        tally: dict[str, int] = {}
        for action in actions:
            outcome = govern_and_execute(
                action, client, mc, cfg, console,
                meta=meta, goal=goal, dry_run=dry_run,
                state=state, adops=adops, action_state=action_state,
                action_state_path=action_state_path, now=now,
            )
            tally[outcome.outcome] = tally.get(outcome.outcome, 0) + 1

        # RECORD
        summary_bits = ", ".join(f"{k}={v}" for k, v in sorted(tally.items()))
        console.print(f"  [bold]cycle {n} summary:[/bold] {summary_bits}")
        if tally.get("held") and set(tally) == {"held"}:
            console.print(
                "  [dim]all proposed actions held for discipline — patience is "
                "the default, valid outcome.[/dim]"
            )
    finally:
        if owns_mc:
            try:
                mc.close()
            except Exception:  # noqa: BLE001 — cleanup must never mask/kill a cycle
                pass


def _render_observed_adsets(state: dict, console: Console) -> None:
    """One compact line per observed ad set — the OBSERVE snapshot a human (or a
    reader of the mock demo) can scan: budget, learning phase, CTR, spend."""
    for a in state.get("adsets", []):
        row = _insights_row_for(state, str(a.get("id")))
        learn = _learning_status(a) or "—"
        budget = a.get("daily_budget_usd")
        budget_s = f"${budget:,.2f}/day" if budget is not None else "—"
        if isinstance(row, dict):
            perf = (
                f"CTR {row.get('ctr', '0')}% · ${row.get('spend', '0')} spent · "
                f"{row.get('impressions', '0')} impr"
            )
        else:
            perf = "no delivery"
        console.print(
            f"    [dim]· {a.get('name')}[/dim] "
            f"[{a.get('status')}/{learn}] {budget_s} — {perf}"
        )


def run_loop(
    cfg,
    console: Console,
    goal: str,
    interval_min: int | None = None,
    once: bool = False,
    dry_run: bool = False,
    mock: bool = False,
    no_llm: bool = False,
) -> None:
    """Deploy-once autonomous loop. Runs forever (foreground) unless `once`.

    A per-cycle exception is logged and the loop continues; only Ctrl-C or
    `once` stops it. `dry_run` runs OBSERVE + DECIDE + the discipline gate + the
    Veto authorize gates and logs verdicts, but SKIPS every Meta write.

    `interval_min` is the OBSERVE cadence. When unset (None / <= 0) it defaults
    to the policy's `ad_ops.observe_interval_minutes` (360). Observing is cheap
    and safe; the readiness gate — not the interval — is what prevents premature
    action, so the default cadence is deliberately slow.

    `mock` mimics Meta entirely offline (MockMetaAdsClient — no real account, no
    real spend) so the full loop runs against seeded, evolving fake campaigns.
    The REAL Veto authorize gate and the CODE discipline gate still run on every
    action — mock only swaps the Meta data surface, never the governance. When
    `mock` is set and there is no LLM key (or `no_llm`), the pure-rules heuristic
    brain is used so the demo needs no model key.
    """
    pre = _preflight(cfg, console, mock=mock)
    if pre is None:
        return
    cfg, meta = pre

    # Pick the brain: real LLM decide() by default; the heuristic when the user
    # forced it (--no-llm) or when we're mimicking Meta with no LLM key present.
    # `has_llm_key` resolves ANY configured/present provider (not just Anthropic).
    has_llm = has_llm_key(cfg)
    use_heuristic = no_llm or (mock and not has_llm)
    action_state_path = _mock_state_path() if mock else None

    if mock:
        # Each mock invocation rebuilds a fresh mimicked world (new process =
        # fresh campaigns); reset the matching cooldown file so the demo is
        # reproducible run-to-run. Cooldown still applies fully ACROSS cycles
        # within this run (stamped after cycle 1, enforced from cycle 2 on).
        from .tools import mock_meta as _mock

        _mock.reset_world()
        try:
            p = action_state_path
            if p and p.exists():
                p.unlink()
        except Exception:  # noqa: BLE001 — best-effort demo reset
            pass

    adops = load_ad_ops()
    if interval_min and interval_min > 0:
        effective_interval = interval_min
        interval_src = "--interval"
    else:
        effective_interval = adops.observe_interval_minutes
        interval_src = "policy ad_ops.observe_interval_minutes"

    console.print("\n[bold cyan]Veto Agents — adbuyer autonomous loop[/bold cyan]")
    if mock:
        console.print(
            "  [magenta]MOCK:[/magenta]     mimicking Meta offline — NO real account, "
            "NO real spend. Real Veto + discipline gates still run on every action."
        )
    console.print(f"  [dim]goal:[/dim]      {goal}")
    console.print(f"  [dim]agent_id:[/dim]  {cfg.agent_id}")
    console.print(f"  [dim]account:[/dim]   {meta.get('ad_account_id')}")
    console.print(
        f"  [dim]brain:[/dim]     {'heuristic (pure rules, no LLM)' if use_heuristic else 'LLM decide()'}"
    )
    console.print(
        f"  [dim]interval:[/dim]  {effective_interval} min "
        f"[dim]({interval_src})[/dim]" + ("  [dim](--once)[/dim]" if once else "")
    )
    console.print(
        "  [dim]discipline:[/dim] "
        f"respect_learning={adops.respect_learning_phase} · "
        f">={adops.min_days_before_action:g}d & "
        f"(>={adops.min_conversions_before_action} conv or "
        f">={adops.min_impressions_before_action} impr) · "
        f"cooldown {adops.cooldown_days_per_entity:g}d · "
        f"budget +/-{adops.max_budget_change_pct:g}%"
    )
    if dry_run:
        console.print("  [yellow]dry-run:[/yellow]   authorize gates run; Meta writes skipped")
    console.print(
        "  [dim]Veto governs every autonomous decision before Meta is touched. "
        "Ctrl-C to stop.[/dim]"
    )

    client = VetoClient(api_base=cfg.veto_api_base, api_key=cfg.api_key)
    # In mock mode build the mimic client ONCE and reuse it — it carries the
    # evolving world (accumulating insights + budget/status mutations) that the
    # real, stateless HTTP client wouldn't need to.
    persistent_mc = make_meta_client(meta, mock=True) if mock else None
    n = 0
    try:
        while True:
            n += 1
            try:
                _run_cycle(
                    n, cfg, console, client, meta, goal, dry_run,
                    mc=persistent_mc, use_heuristic=use_heuristic,
                    action_state_path=action_state_path,
                )
            except KeyboardInterrupt:
                raise
            except Exception as e:  # noqa: BLE001 — a bad cycle must NOT kill the loop
                console.print(f"  [red]x cycle {n} error (loop continues):[/red] {e}")

            if once:
                break

            try:
                time.sleep(max(1, effective_interval) * 60)
            except KeyboardInterrupt:
                raise
    except KeyboardInterrupt:
        console.print("\n[dim]· stopped. No cycle in progress; nothing left mid-flight.[/dim]\n")
    finally:
        client.close()
        if persistent_mc is not None:
            try:
                persistent_mc.close()
            except Exception:  # noqa: BLE001 — cleanup must never raise
                pass


# ─── STRUCTURED entry points (for the MCP server / any non-TTY caller) ─────
#
# These wrap the EXACT same governed core the daemon uses (observe / decide /
# _is_actionable / _apply_magnitude_cap / govern_and_execute) with a quiet
# console and a structured dict return — instead of printing to a TTY and
# looping forever. They deliberately do NOT call `_preflight` (which would
# prompt for a magic-link sign-in — impossible over stdio); a missing sign-in
# becomes a structured `{"error": "not_signed_in"}` the caller can surface.
#
# CRITICAL: no gate is bypassed. `run_cycle` runs the SAME per-action sequence
# as `_run_cycle`/`run_loop` — CODE discipline (`_is_actionable` +
# `_apply_magnitude_cap`) BEFORE the fail-closed Veto authorize BEFORE any Meta
# write — because it calls `govern_and_execute`, the one function that owns
# those gates.


def observe_structured(cfg, *, mock: bool = False) -> dict[str, Any]:
    """Read-only OBSERVE of the ad account, returned as a plain dict.

    No spend happens here, so there is no Veto gate — it's pure observation.
    Returns `{ad_account_id, account, campaigns, adsets, ads, insights, errors,
    currency}` on success, or `{"error": ...}` when not signed in / Meta creds
    are missing. Fail-soft: a single failed read is surfaced in `errors`, never
    raised.
    """
    if not (getattr(cfg, "api_key", None) and getattr(cfg, "agent_id", None)):
        return {"error": "not_signed_in", "hint": "run veto-agents setup"}

    if mock:
        from .tools.mock_meta import DEFAULT_ACCOUNT_ID

        meta_id = DEFAULT_ACCOUNT_ID
        mc = make_meta_client({"ad_account_id": DEFAULT_ACCOUNT_ID}, mock=True)
    else:
        meta = meta_env.load_meta(cfg)
        absent = meta_env.missing(meta)
        if absent:
            return {"error": "meta_credentials_missing", "missing": absent}
        meta_id = meta.get("ad_account_id")
        try:
            mc = make_meta_client(meta, mock=False)
        except Exception as e:  # noqa: BLE001 — surface as data, never raise
            return {"error": "meta_client_error", "detail": str(e)}

    try:
        state = observe(mc)  # already structured + fail-soft
    finally:
        try:
            mc.close()
        except Exception:  # noqa: BLE001 — cleanup must never mask the result
            pass
    return {"ad_account_id": meta_id, **state}


def run_cycle(
    cfg,
    goal: str,
    *,
    mock: bool = False,
    no_llm: bool = False,
    dry_run: bool = False,
    mc: "meta_ads.MetaAdsClient | None" = None,
    veto_client: "VetoClient | None" = None,
    action_state_path: Path | None = None,
    reset_mock_world: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run ONE OBSERVE → DECIDE → DISCIPLINE → VETO → ACT cycle, structured.

    This is `_run_cycle` minus the printing and minus the forever-loop. It
    reuses every gate: the CODE discipline gate and the fail-closed Veto
    authorize both run INSIDE `govern_and_execute`, so a caller (e.g. an MCP
    host LLM) cannot spend or mutate Meta around them.

    `mock=True` mimics Meta entirely offline (seeded fake campaigns, no real
    account, no real spend) — but the REAL Veto authorize (decision_only, free)
    and the discipline gate still run on every action. `no_llm` forces the
    pure-rules heuristic brain (also implied when mocking with no LLM key).
    `dry_run` runs both gates but skips the Meta write.

    Test-harness injection seam (all optional; production callers pass none of
    these and behaviour is unchanged):
      * `mc` — a pre-built Meta client to REUSE across cycles instead of building
        one. When supplied the caller owns its lifecycle (it is NOT closed here)
        and the mock-world/cooldown reset below is skipped, so an accelerated
        simulation can drive many cycles against ONE persistent world.
      * `veto_client` — a pre-built (or stub) Veto client. When supplied the
        caller owns its lifecycle (it is NOT closed here). The offline sim uses a
        local `allow` stub so the discipline gate is exercised under volume
        without hammering prod; a live pass injects a real `VetoClient`.
      * `action_state_path` — override the per-entity cooldown state file (a
        per-run temp file keeps sim runs off the real `~/.veto` state).
      * `reset_mock_world` — when False, do NOT reset the seeded mock world /
        cooldown file (the sim resets once and then persists across sim-days).
      * `now` — the reference clock for the discipline gate (entity age + the
        per-entity cooldown). The sim passes a SIMULATED clock (one day per
        cycle) so cooldown/age are measured in sim-days, not wall-clock.

    Returns:
      {goal, mock, dry_run, brain, summary,
       observed:{account,campaigns,adsets,ads,errors},
       proposals:[<action.describe()>],
       actions:[{type,entity,entity_level,verdict,applied,rationale,reason,
                 receipt_url,reason_codes,outcome,old_budget_usd,new_budget_usd}],
       summary_counts:{executed,held,denied,escalated,skipped,failed,dry_run}}
    or {"error": ...} when not signed in / Meta creds missing.
    """
    console = Console(quiet=True)  # gates still execute; all output is discarded

    # NON-interactive sign-in check (never prompt over stdio).
    if not (getattr(cfg, "api_key", None) and getattr(cfg, "agent_id", None)):
        return {"error": "not_signed_in", "hint": "run veto-agents setup"}

    if mock:
        from .tools.mock_meta import DEFAULT_ACCOUNT_ID

        meta = {
            "access_token": "MOCK",
            "ad_account_id": DEFAULT_ACCOUNT_ID,
            "page_id": "1029384756",
            "landing_url": "https://example.com/landing",
        }
    else:
        meta = meta_env.load_meta(cfg)
        absent = meta_env.missing(meta)
        if absent:
            return {"error": "meta_credentials_missing", "missing": absent}

    # Brain selection — identical rule to run_loop.
    use_heuristic = no_llm or (mock and not has_llm_key(cfg))
    # Cooldown state file: an injected path (sim → per-run temp) wins; else the
    # mock file for mock runs, or the real file otherwise.
    if action_state_path is None:
        action_state_path = _mock_state_path() if mock else None

    # Reset the mimicked world + its cooldown file ONCE per invocation so a
    # standalone mock cycle is reproducible — but ONLY when we own the Meta
    # client. An injected `mc` (the accelerated sim) carries a persistent world
    # across many sim-days and manages its own reset, so we must not wipe it.
    if mock and mc is None and reset_mock_world:
        from .tools import mock_meta as _mock

        _mock.reset_world()
        try:
            if action_state_path and action_state_path.exists():
                action_state_path.unlink()
        except Exception:  # noqa: BLE001 — best-effort demo reset
            pass

    # Veto client + Meta client: reuse an injected instance (caller owns its
    # lifecycle) or build+own one here.
    owns_client = veto_client is None
    client = veto_client or VetoClient(api_base=cfg.veto_api_base, api_key=cfg.api_key)
    owns_mc = mc is None
    if mc is None:
        try:
            mc = make_meta_client(meta, mock=mock)
        except Exception as e:  # noqa: BLE001 — surface as data, never raise
            if owns_client:
                client.close()
            return {"error": "meta_client_error", "detail": str(e)}

    rows: list[dict] = []
    tally: dict[str, int] = {}
    summary = ""
    state: dict[str, Any] = {}
    actions: list[Action] = []
    try:
        state = observe(mc)
        if use_heuristic:
            actions, summary = _heuristic_decide(state, load_ad_ops())
        else:
            actions, summary = decide(state, goal, cfg)

        adops = load_ad_ops()
        action_state = load_action_state(action_state_path)
        # `now` is the discipline-gate reference clock; the sim injects a
        # simulated one (one day per cycle) so age/cooldown are in sim-days.
        if now is None:
            now = datetime.now(timezone.utc)
        for action in actions:
            oc = govern_and_execute(
                action, client, mc, cfg, console,
                meta=meta, goal=goal, dry_run=dry_run,
                state=state, adops=adops, action_state=action_state,
                action_state_path=action_state_path, now=now,
            )
            tally[oc.outcome] = tally.get(oc.outcome, 0) + 1
            rows.append({
                "type": oc.type,
                "entity": oc.entity_id,
                "entity_level": oc.entity_level,
                "verdict": oc.verdict,
                "applied": oc.applied,
                "rationale": oc.rationale,
                "reason": oc.reason,
                "receipt_url": oc.receipt_url,
                "reason_codes": oc.reason_codes,
                "outcome": oc.outcome,
                "old_budget_usd": oc.old_budget_usd,
                "new_budget_usd": oc.new_budget_usd,
            })
    finally:
        # Only close what we own; an injected sim client / Veto client is
        # reused across many cycles and closed by the caller.
        if owns_client:
            client.close()
        if owns_mc:
            try:
                mc.close()
            except Exception:  # noqa: BLE001 — cleanup must never mask the result
                pass

    return {
        "goal": goal,
        "mock": mock,
        "dry_run": dry_run,
        "brain": "heuristic" if use_heuristic else "llm",
        "summary": summary,
        "observed": {
            "account": state.get("account", {}),
            "campaigns": state.get("campaigns", []),
            "adsets": state.get("adsets", []),
            "ads": state.get("ads", []),
            "errors": state.get("errors", []),
        },
        "proposals": [a.describe() for a in actions],
        "actions": rows,
        "summary_counts": {
            "executed": tally.get("executed", 0),
            "held": tally.get("held", 0),
            "denied": tally.get("denied", 0),
            "escalated": tally.get("escalated", 0),
            "skipped": tally.get("skipped", 0),
            "failed": tally.get("failed", 0),
            "dry_run": tally.get("dry-run", 0),
        },
    }
