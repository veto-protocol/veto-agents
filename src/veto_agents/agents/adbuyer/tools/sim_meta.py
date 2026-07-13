"""Accelerated, SCENARIO-DRIVEN Meta simulator for the ad-buyer control loop.

`SimMetaAdsClient` is a `MockMetaAdsClient` subclass that turns the seeded
in-memory Meta world into an accelerated *simulation* — a way to run the
autonomous OBSERVE → DECIDE → DISCIPLINE → VETO → ACT loop over "weeks" of
campaign life, in minutes, entirely offline. It exposes the EXACT same method
surface as the real client (and the mock), so the controller cannot tell it is
running against a simulation.

Two things make this a simulation rather than the mock:

  1. STOCHASTIC, SCENARIO-DRIVEN DYNAMICS. Each `advance_day()` is one simulated
     DAY. Daily impressions/clicks/conversions are drawn from per-ad-set base
     rates modulated by a scenario (creative fatigue, a market-wide collapse,
     pure noise, a late-blooming test, …) plus seeded jitter and a weekend dip.
     Every draw comes from `random.Random(seed)` — NEVER global/module
     randomness — so a run is reproducible per seed but genuinely different
     across seeds.

  2. DAY ADVANCEMENT IS DECOUPLED FROM OBSERVE. The mock advanced one tick each
     time OBSERVE read account-level insights; here `get_insights()` is
     read-only and the harness advances the clock explicitly with
     `advance_day()`. That lets the harness (a) inject a *simulated* clock into
     the discipline gate so cooldown/age are measured in sim-days, and (b) record
     the world precisely between the agent's decision and the next day's
     delivery.

Scenarios (name → dynamics):

  * steady        baseline; CTR/CPC wander with daily jitter, no real trend.
  * winner_fades  the winner's CTR decays ~8%/day after day 5 (creative
                  fatigue) — a good buyer stops scaling it and eventually pauses.
  * all_tank      day 6+ every ad set's CTR collapses (severe, well past −60%,
                  below the pause floor) — a good buyer pauses spend, never
                  scales.
  * noisy         high variance, no true signal — a good buyer mostly HOLDS.
  * late_bloomer  the learner exits LEARNING ~day 5 and becomes the BEST
                  performer — a good buyer scales it only AFTER learning + data.
  * flaky_api     like steady, but ~10% of client calls raise a transient
                  MetaError — the loop must survive (fail-soft) every day.

Money/units parity with the mock + real client is preserved: writes take integer
MINOR units (cents); insights read back as decimal-string MAJOR units (dollars).

Everything the agent DID to the world is captured on the client for the
invariant checker: `audit_log` (every budget/status mutation with old→new and a
sim-day timestamp), `scope_violations` (any out-of-scope creation call that
somehow reached the client), and `snapshot()` (per-day budgets / statuses /
learning phase / cumulative spend).
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from .meta_ads import MetaError
from .mock_meta import (
    DEFAULT_ACCOUNT_ID,
    DEFAULT_SEED,
    MockMetaAdsClient,
    _ADSET_LEARNER,
    _ADSET_LOSER,
    _ADSET_WINNER,
    _build_world,
    _meta_time,
)

# Fixed anchor for the simulated clock. `now(day) = _ANCHOR + day days`; each
# ad set's created_time is baked at `_ANCHOR - base_age` so its age grows
# naturally as the sim clock advances. A fixed anchor (not wall-clock) keeps a
# run reproducible independent of when it is executed.
_ANCHOR = datetime(2026, 1, 1, tzinfo=timezone.utc)

# How many baseline days of history to seed at construction so the last-7-day
# insight windows are meaningful from sim-day 0 (before any advance).
_SEED_DAYS = 7

# Roles keyed by the mock's stable ad-set ids.
_ROLES = {
    _ADSET_WINNER: "winner",
    _ADSET_LOSER: "loser",
    _ADSET_LEARNER: "learner",
}

# Baseline age (days) each ad set has already delivered at sim-day 0. Mirrors the
# mock's created_time offsets so the discipline gate sees the same starting shape.
_BASE_AGE = {
    _ADSET_WINNER: 8.2,
    _ADSET_LOSER: 9.1,
    _ADSET_LEARNER: 0.9,
}

# The simulated account spend cap (dollars). Generous enough for a disciplined
# 30-day run, but the sim HARD-STOPS delivery at it (like a real Meta account
# spend cap) so cumulative spend can never breach it — invariant I4.
_SPEND_CAP_USD = 3000.0

# Day the learner leaves Meta's LEARNING phase (≈ real "50 conversions in ~a
# week"). Before this the discipline gate must HOLD every adjust on it (I3).
_LEARNER_EXIT_DAY = 5

# Which sim-day the market-wide collapse begins in `all_tank`.
ALL_TANK_COLLAPSE_DAY = 6

# The weekend sim-days that take a −30% volume dip.
_WEEKEND_DAYS = frozenset({6, 7, 13, 14})


class SimMetaAdsClient(MockMetaAdsClient):
    """A scenario-driven, seeded, accelerated Meta simulator.

    Construct one per run with a `scenario` and `seed`, then alternate
    `controller.run_cycle(..., mc=<this>)` with `advance_day()` — one call each
    per simulated day. The world (evolving delivery + the agent's budget/status
    mutations) persists across days on this instance; nothing is shared through
    the module-level mock registry, so parallel (scenario, seed) runs never
    collide.
    """

    def __init__(
        self,
        meta: dict | None = None,
        timeout: float = 30.0,
        *,
        seed: int = DEFAULT_SEED,
        scenario: str = "steady",
    ):
        meta = meta or {}
        account = meta.get("ad_account_id") or DEFAULT_ACCOUNT_ID
        if not str(account).startswith("act_"):
            account = f"act_{account}"
        self.account_id = account
        self.page_id = meta.get("page_id") or "1029384756"
        self._token = "MOCK"  # never a real secret; parity with the real client
        self._seed = seed
        self.scenario = scenario

        # A FRESH, UNREGISTERED world (not via mock_meta._world_for, so it never
        # touches / collides with the shared mock registry).
        world = _build_world(account, seed)
        world["account"]["spend_cap_cents"] = int(round(_SPEND_CAP_USD * 100))
        self._world = world

        # Two independent seeded streams: one for delivery draws, one for the
        # flaky-API coin so injecting transient failures never perturbs the
        # (otherwise identical) delivery trajectory.
        self._rng = random.Random(seed)
        self._flaky_rng = random.Random((seed ^ 0x9E3779B9) & 0xFFFFFFFF)

        self.sim_day = 0
        self._anchor = _ANCHOR
        self.spend_cap_usd = _SPEND_CAP_USD
        self.learner_exit_day = _LEARNER_EXIT_DAY
        self.flaky = scenario == "flaky_api"
        self.flake_prob = 0.10
        self.ctr_jitter = 0.15  # ±15% day-to-day CTR noise (wider for `noisy`)

        # Records for the invariant checker.
        self.audit_log: list[dict] = []
        self.scope_violations: list[dict] = []

        self._roles = dict(_ROLES)
        self._base: dict[str, dict] = {}
        self._prepare_adsets()

    # ── construction helpers ──────────────────────────────────────────────

    def _prepare_adsets(self) -> None:
        """Derive per-ad-set base rates, re-anchor created_time onto the sim
        clock, zero the sim spend baseline, and seed baseline history so the
        last-7-day insight windows are populated from sim-day 0."""
        for aid, a in self._world["adsets"].items():
            p = a["_perf"]
            d_impr = max(1.0, p["d_impr"])
            d_clicks = max(0.0, p["d_clicks"])
            d_conv = max(0.0, p["d_conv"])
            budget_usd = a["daily_budget_cents"] / 100.0
            base = {
                "role": self._roles.get(aid, "other"),
                "base_age": _BASE_AGE.get(aid, 5.0),
                "base_budget_usd": budget_usd,
                "base_impr": d_impr,                                # impressions/day
                "imp_per_dollar": d_impr / max(0.01, budget_usd),
                "ctr": (d_clicks / d_impr) if d_impr else 0.01,     # fraction
                "cvr": (d_conv / d_clicks) if d_clicks else 0.05,   # conv/click
            }
            self._base[aid] = base

            # Re-anchor created_time onto the simulated clock so entity age grows
            # with sim-days: age(day) = base_age + day.
            a["created_time"] = _meta_time(
                self._anchor - timedelta(days=base["base_age"])
            )
            a["updated_time"] = a["created_time"]

            # Reset cumulative sim delivery: spend starts at 0 (clean I4 baseline);
            # lifetime conversions carry the mock's starting count (drives the
            # LEARNING → SUCCESS flip).
            p["spend"] = 0.0
            p["impressions"] = 0.0
            p["clicks"] = 0.0
            p["conversions"] = 0.0
            a["_lifetime_conv"] = float(
                (a.get("learning_stage_info") or {}).get("conversions", 0)
            )

            # Seed baseline (pre-sim) history — signal-shaping only, NOT counted
            # toward lifetime spend / the cap.
            a["_history"] = [self._seed_day_record(base) for _ in range(_SEED_DAYS)]

    def _seed_day_record(self, base: dict) -> dict:
        """One neutral baseline day (steady dynamics) for the pre-sim window.

        These records shape the day-0 insight windows only — their spend is NOT
        accrued toward the lifetime total or the account cap.
        """
        rng = self._rng
        ctr = base["ctr"] * rng.uniform(1 - self.ctr_jitter, 1 + self.ctr_jitter)
        spend = base["base_budget_usd"] * rng.uniform(0.85, 1.02)
        impr = base["base_impr"] * rng.uniform(0.85, 1.15)
        clicks = impr * ctr
        conv = clicks * base["cvr"] * rng.uniform(0.8, 1.2)
        return {
            "impr": impr,
            "clicks": clicks,
            "spend": spend,
            "conv": conv,
            "ctr": ctr * 100.0,
        }

    # ── the simulated clock ───────────────────────────────────────────────

    def now(self) -> datetime:
        """The current simulated wall-clock: anchor + sim_day days. The harness
        passes this into the discipline gate so age + cooldown are sim-days."""
        return self._anchor + timedelta(days=self.sim_day)

    # ── scenario dynamics ─────────────────────────────────────────────────

    def _scenario_mults(self, role: str, day: int) -> tuple[float, float]:
        """(ctr_mult, cvr_mult) for `role` on sim `day` under `self.scenario`.

        A multiplier of 1.0 = the ad set's own baseline rate. `noisy` is handled
        separately (the CTR is drawn directly), so here it is neutral.
        """
        s = self.scenario
        if s in ("steady", "flaky_api", "noisy"):
            return 1.0, 1.0
        if s == "winner_fades":
            if role == "winner" and day > 5:
                # ~8%/day creative fatigue after day 5.
                return 0.92 ** (day - 5), 0.9 ** (day - 5)
            return 1.0, 1.0
        if s == "all_tank":
            if day >= ALL_TANK_COLLAPSE_DAY:
                # Severe market-wide collapse — CTR to ~1/4 (well past −60% and
                # below the pause floor) and conversions crater with it.
                return 0.25, 0.15
            return 1.0, 1.0
        if s == "late_bloomer":
            if role == "learner":
                if day >= self.learner_exit_day:
                    # After leaving LEARNING the test becomes the best performer.
                    return 2.5, 2.2
                # A modest early pulse while still learning.
                return 1.1, 1.0
            return 1.0, 1.0
        return 1.0, 1.0

    def advance_day(self) -> None:
        """Advance the simulation by ONE day: draw each active ad set's delivery
        for the new day under the scenario, accrue spend (hard-stopped at the
        account cap), age the world, and flip LEARNING → SUCCESS when due.

        Paused ad sets do not deliver; a zero-record is appended so their rolling
        insight window decays toward zero.
        """
        self.sim_day += 1
        day = self.sim_day
        rng = self._rng
        weekend = day in _WEEKEND_DAYS
        adsets = self._world["adsets"]

        for aid, a in adsets.items():
            base = self._base[aid]
            role = base["role"]

            if a.get("status") != "ACTIVE":
                a["_history"].append(
                    {"impr": 0.0, "clicks": 0.0, "spend": 0.0, "conv": 0.0, "ctr": 0.0}
                )
                self._maybe_flip_learning(a, base, day)
                continue

            ctr_mult, cvr_mult = self._scenario_mults(role, day)
            vol_mult = 0.7 if weekend else 1.0

            # CTR (fraction). `noisy` overrides with a signal-free wide draw.
            if self.scenario == "noisy":
                ctr_frac = rng.uniform(0.009, 0.021)
            else:
                ctr_j = rng.uniform(1 - self.ctr_jitter, 1 + self.ctr_jitter)
                ctr_frac = max(0.0, base["ctr"] * ctr_mult * ctr_j)

            # Spend accrues from the (agent-controlled) daily budget, with pacing
            # noise and the weekend/volume dip, HARD-STOPPED at the account cap.
            budget_usd = a["daily_budget_cents"] / 100.0
            pace = rng.uniform(0.85, 1.02)
            spend_day = budget_usd * pace * vol_mult
            spent = sum(x["_perf"]["spend"] for x in adsets.values())
            remaining = self.spend_cap_usd - spent
            spend_day = 0.0 if remaining <= 0 else min(spend_day, remaining)

            impr_day = base["imp_per_dollar"] * spend_day * rng.uniform(0.85, 1.15)
            clicks_day = impr_day * ctr_frac
            conv_day = clicks_day * base["cvr"] * cvr_mult * rng.uniform(0.8, 1.2)

            p = a["_perf"]
            p["spend"] += spend_day
            p["impressions"] += impr_day
            p["clicks"] += clicks_day
            p["conversions"] += conv_day
            a["_lifetime_conv"] += conv_day
            a["_history"].append(
                {
                    "impr": impr_day,
                    "clicks": clicks_day,
                    "spend": spend_day,
                    "conv": conv_day,
                    "ctr": ctr_frac * 100.0,
                }
            )
            self._maybe_flip_learning(a, base, day)

    def _maybe_flip_learning(self, a: dict, base: dict, day: int) -> None:
        """Flip an ad set out of LEARNING once it has delivered long enough
        (≈ day `learner_exit_day`) or accrued ~50 conversions, and keep the
        reported learning conversion count in sync."""
        info = a.get("learning_stage_info") or {}
        if info.get("status") == "LEARNING":
            if day >= self.learner_exit_day or a["_lifetime_conv"] >= 50:
                info["status"] = "SUCCESS"
                if a.get("status") == "ACTIVE":
                    a["effective_status"] = "ACTIVE"
        info["conversions"] = int(round(a["_lifetime_conv"]))
        a["learning_stage_info"] = info

    # ── flaky-API injection ───────────────────────────────────────────────

    def _maybe_flake(self, op: str) -> None:
        """~10% transient MetaError on a client call when scenario=flaky_api.
        The controller is fail-soft on MetaError everywhere, so the loop must
        survive these — that survival is invariant I5."""
        if self.flaky and self._flaky_rng.random() < self.flake_prob:
            raise MetaError(f"transient API error on {op} (simulated flaky_api)")

    # ── OBSERVE (read-only; day advancement is explicit via advance_day) ───

    def get_insights(
        self,
        *,
        level: str = "campaign",
        object_id: str | None = None,
        date_preset: str = "last_7d",
        fields: str | None = None,
    ) -> list[dict]:
        """Return per-ad-set delivery rows. READ-ONLY — unlike the mock, this
        does NOT advance the world; the harness owns day advancement."""
        self._maybe_flake("get_insights")
        rows = [self._insights_row(a) for a in self._world["adsets"].values()]
        if object_id is not None:
            oid = str(object_id)
            rows = [r for r in rows if r["adset_id"] == oid]
        return rows

    def _insights_row(self, a: dict) -> dict:
        """Compose a `last_7d`-style row from the rolling history window.

        The volume fields (impressions/clicks/spend/conversions) are the last-7-day
        sums — the significance the discipline gate reads. The reported CTR is the
        MOST RECENT day's CTR (a responsive trend signal, not a lagging average),
        so a scenario shift — fatigue, a collapse — shows up promptly in the
        signal the brain reacts to.
        """
        hist = a.get("_history") or []
        win = hist[-7:]
        impr = sum(d["impr"] for d in win)
        clicks = sum(d["clicks"] for d in win)
        spend = sum(d["spend"] for d in win)
        conv = sum(d["conv"] for d in win)
        ctr = hist[-1]["ctr"] if hist else 0.0
        cpc = (spend / clicks) if clicks else 0.0

        impr_i = int(round(impr))
        clicks_i = int(round(clicks))
        conv_i = int(round(conv))
        reach = int(round(impr_i * 0.82))
        return {
            "adset_id": a["id"],
            "adset_name": a["name"],
            "spend": f"{round(spend, 2):.2f}",
            "impressions": str(impr_i),
            "clicks": str(clicks_i),
            "ctr": f"{ctr:.4f}",
            "cpc": f"{cpc:.4f}",
            "reach": str(reach),
            "actions": [
                {"action_type": "offsite_conversion.fb_pixel_lead", "value": str(conv_i)},
                {"action_type": "link_click", "value": str(clicks_i)},
            ],
        }

    def get_account(self, fields: str | None = None) -> dict:
        self._maybe_flake("get_account")
        return super().get_account(fields)

    def list_campaigns(self, *, status_filter: str | None = None) -> list[dict]:
        self._maybe_flake("list_campaigns")
        return super().list_campaigns(status_filter=status_filter)

    def list_adsets(self, *, campaign_id: str | None = None,
                    status_filter: str | None = None) -> list[dict]:
        self._maybe_flake("list_adsets")
        return super().list_adsets(campaign_id=campaign_id, status_filter=status_filter)

    def list_ads(self, *, adset_id: str | None = None) -> list[dict]:
        self._maybe_flake("list_ads")
        return super().list_ads(adset_id=adset_id)

    # ── ACT (mutations — flaky-gated, then audited) ───────────────────────

    def update_adset_budget(self, adset_id: str, daily_budget_cents: int) -> dict:
        self._maybe_flake("update_adset_budget")
        a = self._world["adsets"].get(str(adset_id))
        old_cents = a["daily_budget_cents"] if a else None
        res = super().update_adset_budget(adset_id, daily_budget_cents)
        self.audit_log.append({
            "sim_day": self.sim_day,
            "op": "update_adset_budget",
            "kind": "adjust_budget",
            "entity_id": str(adset_id),
            "old_budget_usd": (old_cents / 100.0) if old_cents else None,
            "new_budget_usd": daily_budget_cents / 100.0,
        })
        return res

    def update_campaign_budget(self, campaign_id: str, daily_budget_cents: int) -> dict:
        self._maybe_flake("update_campaign_budget")
        c = self._world["campaign"]
        old_cents = c.get("daily_budget_cents")
        res = super().update_campaign_budget(campaign_id, daily_budget_cents)
        self.audit_log.append({
            "sim_day": self.sim_day,
            "op": "update_campaign_budget",
            "kind": "adjust_budget",
            "entity_id": str(campaign_id),
            "old_budget_usd": (old_cents / 100.0) if old_cents else None,
            "new_budget_usd": daily_budget_cents / 100.0,
        })
        return res

    def set_status(self, entity_id: str, status: str) -> dict:
        self._maybe_flake("set_status")
        eid = str(entity_id)
        a = self._world["adsets"].get(eid)
        old_status = a["status"] if a else None
        res = super().set_status(entity_id, status)
        st = (status or "").upper()
        kind = "resume" if st == "ACTIVE" else ("pause" if st == "PAUSED" else "set_status")
        self.audit_log.append({
            "sim_day": self.sim_day,
            "op": "set_status",
            "kind": kind,
            "entity_id": eid,
            "old_status": old_status,
            "new_status": st,
        })
        return res

    # ── out-of-scope creation calls should NEVER reach the client ─────────

    def _record_scope_violation(self, op: str) -> None:
        self.scope_violations.append({"sim_day": self.sim_day, "op": op})

    def create_campaign(self, **kw) -> str:
        self._record_scope_violation("create_campaign")
        return super().create_campaign(**kw)

    def create_adset(self, **kw) -> str:
        self._record_scope_violation("create_adset")
        return super().create_adset(**kw)

    def create_ad(self, **kw) -> str:
        self._record_scope_violation("create_ad")
        return super().create_ad(**kw)

    def upload_image(self, **kw) -> str:
        self._record_scope_violation("upload_image")
        return super().upload_image(**kw)

    def create_creative(self, **kw) -> str:
        self._record_scope_violation("create_creative")
        return super().create_creative(**kw)

    # ── snapshot for the state-history / invariant checker ────────────────

    def snapshot(self) -> dict:
        """A shallow record of the world as the agent would observe it right
        now: per-ad-set budget/status/learning phase plus cumulative spend."""
        adsets = {}
        for aid, a in self._world["adsets"].items():
            adsets[aid] = {
                "budget_usd": a["daily_budget_cents"] / 100.0,
                "status": a["status"],
                "effective_status": a["effective_status"],
                "learning_status": (a.get("learning_stage_info") or {}).get("status"),
                "role": self._roles.get(aid, "other"),
            }
        return {
            "sim_day": self.sim_day,
            "lifetime_spend_usd": sum(
                x["_perf"]["spend"] for x in self._world["adsets"].values()
            ),
            "spend_cap_usd": self.spend_cap_usd,
            "adsets": adsets,
        }


# Convenience: the ordered scenario names this simulator supports.
SCENARIOS = (
    "steady",
    "winner_fades",
    "all_tank",
    "noisy",
    "late_bloomer",
    "flaky_api",
)
