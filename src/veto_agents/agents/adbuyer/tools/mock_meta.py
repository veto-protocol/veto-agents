"""In-memory 'mimic Meta' client for the ad-buyer agent — NO network, NO spend.

`MockMetaAdsClient` is a drop-in for `meta_ads.MetaAdsClient`: it exposes the
SAME public method signatures and the SAME field / units conventions (money
WRITE fields in integer MINOR units / cents; insights READ fields as decimal
strings in MAJOR units / dollars). It never touches the network and never
spends — it just returns realistic, SEEDED, EVOLVING fake data so the whole
autonomous loop (OBSERVE -> DECIDE -> DISCIPLINE -> Veto -> ACT) can run
end-to-end against mimicked campaigns when the user has no live Meta account.

The mimicked world is ONE campaign with three ad sets that are deliberately
distinct so each layer of the loop is exercised:

  A  WINNER   — out of learning (learning_stage_info.status=SUCCESS), high CTR /
               low CPA, ~8 days old, plenty of delivery, past any cooldown. The
               discipline gate lets it through; the brain wants to SCALE it.
  B  LOSER    — out of learning, poor CTR / high spend, ~9 days old, plenty of
               delivery. The brain wants to PAUSE it (pause is exempt from the
               learning/data bar anyway).
  C  LEARNING — created ~1 day ago, learning_stage_info.status=LEARNING, thin
               data. The CODE discipline gate MUST HOLD it no matter what the
               brain proposes.

`get_insights()` EVOLVES across successive cycles: spend, impressions, clicks
and conversions accumulate every OBSERVE, and the winner pulls ahead. Writes
(`update_adset_budget`, `set_status`, …) mutate the in-memory world so the next
OBSERVE reflects them. Everything is deterministic given the seed.

The world lives in a module-level registry keyed by (account_id, seed) so it
survives even if the controller reconstructs the client each cycle (the real
client is stateless HTTP; this one must remember). Use `reset_world()` for a
clean slate (tests / a fresh demo run).
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

# Reuse the SAME currency conventions + error type as the real client so the
# controller can't tell the difference.
from .meta_ads import MetaError, minor_to_usd, usd_to_minor  # noqa: F401 (parity re-export)

DEFAULT_SEED = 7
DEFAULT_ACCOUNT_ID = "act_601234567890123"

# ── stable synthetic ids (long numeric strings, like real Meta ids) ────────
_CAMPAIGN_ID = "23851000012001"
_ADSET_WINNER = "23851000034001"
_ADSET_LOSER = "23851000034002"
_ADSET_LEARNER = "23851000034003"
_AD_WINNER = "23851000056001"
_AD_LOSER = "23851000056002"
_AD_LEARNER = "23851000056003"

# One shared world per (account_id, seed) so mutations + evolution persist
# across cycles regardless of whether the client is reused or reconstructed.
_WORLDS: dict[tuple[str, int], dict] = {}


def _meta_time(dt: datetime) -> str:
    """Format a datetime the way Meta returns created_time: '...T..+0000'."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")


def reset_world(account_id: str | None = None, seed: int = DEFAULT_SEED) -> None:
    """Drop the cached world so the next client rebuilds a fresh seeded state."""
    key = (account_id or DEFAULT_ACCOUNT_ID, seed)
    _WORLDS.pop(key, None)


def _build_world(account_id: str, seed: int) -> dict:
    """Construct the deterministic starting world (baked-in delivery history)."""
    now = datetime.now(timezone.utc)

    def perf(impr, clicks, spend, conv, d_impr, d_clicks, d_spend, d_conv, d_conv_ramp):
        # cumulative totals + per-cycle base deltas (jittered per tick, seeded)
        return {
            "impressions": float(impr),
            "clicks": float(clicks),
            "spend": float(spend),
            "conversions": float(conv),
            "d_impr": float(d_impr),
            "d_clicks": float(d_clicks),
            "d_spend": float(d_spend),
            "d_conv": float(d_conv),
            "d_conv_ramp": float(d_conv_ramp),  # winner gains momentum each tick
        }

    adsets = {
        _ADSET_WINNER: {
            "id": _ADSET_WINNER,
            "name": "US · Lookalike 1% (winner)",
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
            "daily_budget_cents": 2000,          # $20.00 / day
            "optimization_goal": "OFFSITE_CONVERSIONS",
            "campaign_id": _CAMPAIGN_ID,
            "learning_stage_info": {"status": "SUCCESS", "conversions": 94},
            "created_time": _meta_time(now - timedelta(days=8.2)),
            "updated_time": _meta_time(now - timedelta(days=1.0)),
            # ~3.3% CTR, ~$0.10 CPC, ~$0.50 CPA — a clear winner.
            "_perf": perf(14200, 468, 46.80, 94, 1850, 62, 6.30, 13, 1.0),
        },
        _ADSET_LOSER: {
            "id": _ADSET_LOSER,
            "name": "US · Broad interest (underperformer)",
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
            "daily_budget_cents": 1500,          # $15.00 / day
            "optimization_goal": "OFFSITE_CONVERSIONS",
            "campaign_id": _CAMPAIGN_ID,
            "learning_stage_info": {"status": "SUCCESS", "conversions": 7},
            "created_time": _meta_time(now - timedelta(days=9.1)),
            "updated_time": _meta_time(now - timedelta(days=2.0)),
            # ~0.7% CTR, ~$0.71 CPC, ~$11 CPA — a clear loser.
            "_perf": perf(15900, 112, 79.50, 7, 2100, 15, 7.80, 1, 0.0),
        },
        _ADSET_LEARNER: {
            "id": _ADSET_LEARNER,
            "name": "US · New creative test (learning)",
            "status": "ACTIVE",
            "effective_status": "LEARNING",
            "daily_budget_cents": 1000,          # $10.00 / day
            "optimization_goal": "OFFSITE_CONVERSIONS",
            "campaign_id": _CAMPAIGN_ID,
            "learning_stage_info": {"status": "LEARNING", "conversions": 1},
            "created_time": _meta_time(now - timedelta(days=0.9)),
            "updated_time": _meta_time(now - timedelta(days=0.9)),
            # thin data, ~1 day old — the discipline gate must HOLD this.
            "_perf": perf(540, 9, 2.10, 1, 620, 10, 2.40, 1, 0.0),
        },
    }

    ads = [
        {"id": _AD_WINNER, "name": "Winner — testimonial", "status": "ACTIVE",
         "adset_id": _ADSET_WINNER, "creative": {"id": "23851000078001"}},
        {"id": _AD_LOSER, "name": "Underperformer — stock hero", "status": "ACTIVE",
         "adset_id": _ADSET_LOSER, "creative": {"id": "23851000078002"}},
        {"id": _AD_LEARNER, "name": "New test — carousel", "status": "ACTIVE",
         "adset_id": _ADSET_LEARNER, "creative": {"id": "23851000078003"}},
    ]

    return {
        "tick": 0,
        "seed": seed,
        "rng": random.Random(seed),
        "account": {
            "id": account_id,
            "name": "Veto Demo Ad Account (mimicked)",
            "currency": "USD",
            "account_status": 1,
            "spend_cap_cents": 21000,   # $210.00 hard ceiling
            "balance_cents": 0,
        },
        "campaign": {
            "id": _CAMPAIGN_ID,
            "name": "Signups · US · Prospecting",
            "objective": "OUTCOME_LEADS",
            "status": "ACTIVE",
            "daily_budget_cents": None,   # budget lives on the ad sets (ABO)
        },
        "adsets": adsets,
        "ads": ads,
        "_next_id": 23851000090001,
    }


def _world_for(account_id: str, seed: int) -> dict:
    key = (account_id, seed)
    w = _WORLDS.get(key)
    if w is None:
        w = _build_world(account_id, seed)
        _WORLDS[key] = w
    return w


class MockMetaAdsClient:
    """Drop-in mimic of `meta_ads.MetaAdsClient` — same signatures, no network.

    `meta` is the same dict shape the real client takes (access_token,
    ad_account_id, page_id); only ad_account_id is used (to key the world), and
    a synthetic default is used if absent. `seed` makes the whole world
    deterministic.
    """

    def __init__(self, meta: dict | None = None, timeout: float = 30.0, *, seed: int = DEFAULT_SEED):
        meta = meta or {}
        account = meta.get("ad_account_id") or DEFAULT_ACCOUNT_ID
        if not str(account).startswith("act_"):
            account = f"act_{account}"
        self.account_id = account
        self.page_id = meta.get("page_id") or "1029384756"
        self._token = "MOCK"          # never a real secret; parity with real client
        self._seed = seed
        self._world = _world_for(account, seed)

    # -- lifecycle (no-ops; the world outlives the client) -----------------

    def close(self) -> None:
        # Deliberately does NOT drop the world — mutations/evolution persist so
        # the next cycle's OBSERVE reflects them. Use reset_world() for a reset.
        return None

    def __enter__(self) -> "MockMetaAdsClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- evolution ---------------------------------------------------------

    def _advance(self) -> None:
        """Accumulate one OBSERVE-cycle of delivery onto every ad set.

        Seeded jitter is applied as ONE scale factor per ad set per tick so CTR
        / CPC stay stable (a winner keeps winning); the winner also gains a
        little conversion momentum each tick so it visibly pulls ahead.
        """
        w = self._world
        w["tick"] += 1
        tick = w["tick"]
        rng: random.Random = w["rng"]
        for a in w["adsets"].values():
            if a.get("status") != "ACTIVE":
                continue  # a paused ad set stops delivering
            p = a["_perf"]
            f = rng.uniform(0.92, 1.08)
            p["impressions"] += p["d_impr"] * f
            p["clicks"] += p["d_clicks"] * f
            p["spend"] += p["d_spend"] * f
            # winner momentum: +d_conv_ramp extra conversions per elapsed tick
            conv_delta = (p["d_conv"] + p["d_conv_ramp"] * (tick - 1)) * f
            p["conversions"] += conv_delta

    # -- account / spend cap ----------------------------------------------

    def get_account(self, fields: str | None = None) -> dict:
        acct = self._world["account"]
        spent = sum(a["_perf"]["spend"] for a in self._world["adsets"].values())
        return {
            "id": acct["id"],
            "name": acct["name"],
            "currency": acct["currency"],
            "account_status": acct["account_status"],
            "amount_spent": str(int(round(spent * 100))),   # minor units (cents)
            "spend_cap": str(int(acct["spend_cap_cents"])),
            "balance": str(int(acct["balance_cents"])),
        }

    def set_account_spend_cap(self, cents: int) -> dict:
        if cents < 0:
            raise MetaError("spend_cap must be >= 0 (0 clears the cap).")
        self._world["account"]["spend_cap_cents"] = int(cents)
        return {"success": True}

    # -- hierarchy creation (v1 scope forbids this in the loop; parity only) -

    def create_campaign(self, *, name, objective="OUTCOME_TRAFFIC", status="PAUSED",
                        special_ad_categories=None) -> str:
        return self._new_id()

    def create_adset(self, *, name, campaign_id, daily_budget_cents,
                    objective="OUTCOME_TRAFFIC", billing_event="IMPRESSIONS",
                    optimization_goal=None, bid_strategy="LOWEST_COST_WITHOUT_CAP",
                    countries=None, age_min=18, age_max=65, status="PAUSED") -> str:
        return self._new_id()

    def upload_image(self, *, image_path=None, image_url=None) -> str:
        if not image_path and not image_url:
            raise MetaError("upload_image needs image_path or image_url.")
        return f"mockhash_{self._new_id()}"

    def create_creative(self, *, name, image_hash, link, message="",
                        call_to_action_type="LEARN_MORE", page_id=None) -> str:
        return self._new_id()

    def create_ad(self, *, name, adset_id, creative_id, status="PAUSED") -> str:
        return self._new_id()

    # -- enumeration (OBSERVE) --------------------------------------------

    def list_campaigns(self, *, status_filter: str | None = None) -> list[dict]:
        c = self._world["campaign"]
        row = {
            "id": c["id"],
            "name": c["name"],
            "objective": c["objective"],
            "status": c["status"],
        }
        if c.get("daily_budget_cents"):
            row["daily_budget"] = str(int(c["daily_budget_cents"]))
        if status_filter and row["status"] != status_filter:
            return []
        return [row]

    def list_adsets(self, *, campaign_id: str | None = None,
                    status_filter: str | None = None) -> list[dict]:
        out: list[dict] = []
        for a in self._world["adsets"].values():
            if campaign_id and a.get("campaign_id") != campaign_id:
                continue
            if status_filter and a.get("effective_status") != status_filter:
                continue
            out.append({
                "id": a["id"],
                "name": a["name"],
                "status": a["status"],
                "effective_status": a["effective_status"],
                "daily_budget": str(int(a["daily_budget_cents"])),
                "optimization_goal": a["optimization_goal"],
                "campaign_id": a["campaign_id"],
                "learning_stage_info": dict(a["learning_stage_info"]),
                "created_time": a["created_time"],
                "updated_time": a["updated_time"],
            })
        return out

    def list_ads(self, *, adset_id: str | None = None) -> list[dict]:
        out: list[dict] = []
        for ad in self._world["ads"]:
            if adset_id and ad.get("adset_id") != adset_id:
                continue
            out.append(dict(ad))
        return out

    # -- mutation (ACT — gated by Veto in the controller BEFORE calling) ---

    def update_adset_budget(self, adset_id: str, daily_budget_cents: int) -> dict:
        if daily_budget_cents <= 0:
            raise MetaError(
                f"daily_budget must be > 0 (received {daily_budget_cents} minor units)."
            )
        a = self._world["adsets"].get(str(adset_id))
        if not a:
            raise MetaError(f"unknown ad set {adset_id}")
        a["daily_budget_cents"] = int(daily_budget_cents)
        a["updated_time"] = _meta_time(datetime.now(timezone.utc))
        return {"success": True, "id": str(adset_id)}

    def update_campaign_budget(self, campaign_id: str, daily_budget_cents: int) -> dict:
        if daily_budget_cents <= 0:
            raise MetaError(
                f"daily_budget must be > 0 (received {daily_budget_cents} minor units)."
            )
        c = self._world["campaign"]
        if str(campaign_id) != c["id"]:
            raise MetaError(f"unknown campaign {campaign_id}")
        c["daily_budget_cents"] = int(daily_budget_cents)
        return {"success": True, "id": str(campaign_id)}

    def set_status(self, entity_id: str, status: str) -> dict:
        st = (status or "").upper()
        if st not in ("PAUSED", "ACTIVE", "ARCHIVED", "DELETED"):
            raise MetaError(f"status '{status}' invalid — use PAUSED or ACTIVE.")
        eid = str(entity_id)
        a = self._world["adsets"].get(eid)
        if a:
            a["status"] = st
            a["effective_status"] = st if st != "ACTIVE" else a["effective_status"]
            a["updated_time"] = _meta_time(datetime.now(timezone.utc))
            return {"success": True, "id": eid}
        for ad in self._world["ads"]:
            if ad["id"] == eid:
                ad["status"] = st
                return {"success": True, "id": eid}
        if self._world["campaign"]["id"] == eid:
            self._world["campaign"]["status"] = st
            return {"success": True, "id": eid}
        raise MetaError(f"unknown entity {entity_id}")

    def update_entity_status(self, entity_id: str, status: str) -> dict:
        return self.set_status(entity_id, status)

    # -- insights (evolving) ----------------------------------------------

    def get_insights(self, *, level: str = "campaign", object_id: str | None = None,
                    date_preset: str = "last_7d", fields: str | None = None) -> list[dict]:
        """Return per-ad-set delivery rows (dollars, as decimal strings).

        The account-level read (object_id is None) is what OBSERVE calls once
        per cycle, so that's where we ADVANCE the world one tick — the winner
        pulls ahead over successive cycles. A scoped read (object_id set) does
        NOT advance; it just reports the current row(s).
        """
        rows = [self._insights_row(a) for a in self._world["adsets"].values()]
        if object_id is not None:
            oid = str(object_id)
            rows = [r for r in rows if r["adset_id"] == oid]
        else:
            # one OBSERVE cycle happened — accumulate delivery for the next one.
            self._advance()
        return rows

    def _insights_row(self, a: dict) -> dict:
        p = a["_perf"]
        impr = int(round(p["impressions"]))
        clicks = int(round(p["clicks"]))
        spend = round(p["spend"], 2)
        conv = int(round(p["conversions"]))
        ctr = round((clicks / impr * 100.0), 4) if impr else 0.0
        cpc = round((spend / clicks), 4) if clicks else 0.0
        reach = int(round(impr * 0.82))
        return {
            "adset_id": a["id"],
            "adset_name": a["name"],
            "spend": f"{spend:.2f}",
            "impressions": str(impr),
            "clicks": str(clicks),
            "ctr": f"{ctr:.4f}",
            "cpc": f"{cpc:.4f}",
            "reach": str(reach),
            "actions": [
                {"action_type": "offsite_conversion.fb_pixel_lead", "value": str(conv)},
                {"action_type": "link_click", "value": str(clicks)},
            ],
        }

    # -- low-level parity (the refresh_creative path pokes mc._post) -------

    def _post(self, path: str, data: dict | None = None, files=None) -> dict:
        return {"success": True, "id": str(path)}

    def _get(self, path: str, params: dict | None = None) -> dict:
        return {"data": []}

    def _new_id(self) -> str:
        nid = self._world["_next_id"]
        self._world["_next_id"] = nid + 1
        return str(nid)
