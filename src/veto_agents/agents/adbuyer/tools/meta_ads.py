"""Meta Marketing API client for the ad-buyer agent.

A thin, fail-soft wrapper around Graph API v25.0 (current stable, released
2026-02-18). It builds the standard hierarchy — campaign -> ad set -> image ->
creative -> ad — plus the account-level `spend_cap` (the Veto ceiling) and
insights reads.

IMPORTANT CONVENTIONS (verified against the Meta spec):
  * ALL money WRITE fields (daily_budget, lifetime_budget, bid_amount,
    spend_cap) are integers in the account's MINOR units (cents for USD).
    $20.00 -> "2000". We take USD floats and convert with `usd_to_minor`.
  * Insights READ fields (spend/cpc/ctr) come back as decimal strings in MAJOR
    currency units (dollars) — the opposite footgun. We surface them as-is.
  * Everything is created PAUSED so nothing serves before human review.
  * The token is NEVER printed, logged, or returned. Only presence is exposed.

This tool does NOT call Veto. Budget authorization happens in agent.run()
BEFORE any of these writes — Veto governs; Meta executes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

GRAPH_VERSION = "v25.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

# The 6 valid ODAX objectives. Legacy values (LINK_CLICKS, CONVERSIONS, …) are
# rejected with a 400 for new campaigns.
VALID_OBJECTIVES = {
    "OUTCOME_TRAFFIC",
    "OUTCOME_AWARENESS",
    "OUTCOME_ENGAGEMENT",
    "OUTCOME_LEADS",
    "OUTCOME_SALES",
    "OUTCOME_APP_PROMOTION",
}

# optimization_goal that pairs with billing_event=IMPRESSIONS per objective.
#
# NOTE: the commented-out goals below require a `promoted_object` (a pixel,
# lead form, app, etc.) on the ad set or Meta returns a 400. This MVP builds
# only the objectives that work WITHOUT a promoted_object; for the others we
# fall back to a safe, self-contained goal so the demo never 400s. See
# `_OPT_GOAL_NEEDS_PROMOTED_OBJECT`.
_OPT_GOAL_FOR_OBJECTIVE = {
    "OUTCOME_TRAFFIC": "LINK_CLICKS",
    "OUTCOME_AWARENESS": "REACH",
    "OUTCOME_ENGAGEMENT": "POST_ENGAGEMENT",
    "OUTCOME_LEADS": "LINK_CLICKS",        # LEAD_GENERATION needs a lead form
    "OUTCOME_SALES": "LINK_CLICKS",        # OFFSITE_CONVERSIONS needs a pixel
    "OUTCOME_APP_PROMOTION": "LINK_CLICKS",  # APP_INSTALLS needs a promoted app
}


class MetaError(Exception):
    """A Meta API error surfaced with a clean, token-free message."""


@dataclass
class ToolResult:
    """Same contract as fal_image.ToolResult so agent.run() handles both alike."""

    ok: bool
    actual_cost_usd: float = 0.0
    output_path: str | None = None
    output_url: str | None = None   # e.g. the created object id / ads manager URL
    receipt_url: str | None = None
    error: str | None = None
    denied: bool = False


# ─── currency helpers ────────────────────────────────────────────────────

# Currencies with no minor unit — value is whole units, not cents.
_ZERO_DECIMAL = {"JPY", "KRW", "VND", "CLP", "ISK", "HUF"}


def usd_to_minor(amount_usd: float, currency: str = "USD") -> int:
    """Convert a major-unit amount to the integer minor units Meta wants."""
    if currency.upper() in _ZERO_DECIMAL:
        return int(round(amount_usd))
    return int(round(amount_usd * 100))


def minor_to_usd(minor: str | int | None, currency: str = "USD") -> float:
    """Convert Meta's integer minor-unit string back to a major-unit float."""
    if minor in (None, ""):
        return 0.0
    val = int(minor)
    if currency.upper() in _ZERO_DECIMAL:
        return float(val)
    return val / 100.0


# ─── client ──────────────────────────────────────────────────────────────


class MetaAdsClient:
    """Fail-soft Graph Marketing API client. Raises MetaError on any failure.

    `meta` is the dict from meta_env.load_meta(): access_token, ad_account_id
    (act_...), page_id.
    """

    def __init__(self, meta: dict, timeout: float = 30.0):
        token = meta.get("access_token")
        account = meta.get("ad_account_id")
        if not token:
            raise MetaError("META_ACCESS_TOKEN is not set.")
        if not account:
            raise MetaError("META_AD_ACCOUNT_ID is not set.")
        self._token = token                     # kept private; never printed
        self.account_id = account               # act_...
        self.page_id = meta.get("page_id")
        self._client = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}"},
        )

    # -- low level ---------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{GRAPH_BASE}/{path.lstrip('/')}"

    def _post(self, path: str, data: dict | None = None, files=None) -> dict:
        try:
            r = self._client.post(self._url(path), data=data, files=files)
        except httpx.HTTPError as e:
            raise MetaError(f"network error calling Meta: {e}") from e
        return self._parse(r)

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            r = self._client.get(self._url(path), params=params)
        except httpx.HTTPError as e:
            raise MetaError(f"network error calling Meta: {e}") from e
        return self._parse(r)

    @staticmethod
    def _parse(r: httpx.Response) -> dict:
        """Return parsed JSON or raise MetaError with Meta's error message.

        Meta puts a token in query params on GET, never in the body we log — so
        error text here is safe to surface.
        """
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code >= 400 or (isinstance(body, dict) and "error" in body):
            err = body.get("error", {}) if isinstance(body, dict) else {}
            msg = err.get("error_user_msg") or err.get("message") or r.text[:300]
            code = err.get("code")
            sub = err.get("error_subcode")
            detail = f" (code {code}"
            detail += f"/{sub}" if sub else ""
            detail += ")" if code else ""
            raise MetaError(f"Meta API {r.status_code}: {msg}{detail if code else ''}")
        return body if isinstance(body, dict) else {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "MetaAdsClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- account / spend cap (the Veto ceiling) ----------------------------

    def get_account(self, fields: str | None = None) -> dict:
        """Read spend_cap, amount_spent, balance, currency, account_status."""
        fields = fields or "spend_cap,amount_spent,balance,currency,account_status,name"
        return self._get(self.account_id, params={"fields": fields})

    def set_account_spend_cap(self, cents: int) -> dict:
        """Set the account-level lifetime spend cap (cents). `0` clears it.

        Guardrails (enforced server-side by Meta; we surface the error):
          * cannot set below ~110% of amount_spent,
          * max 10 changes/day.
        Set this ONCE at provisioning as the hard ceiling — not per-tx.
        """
        if cents < 0:
            raise MetaError("spend_cap must be >= 0 (0 clears the cap).")
        return self._post(self.account_id, data={"spend_cap": str(int(cents))})

    # -- hierarchy ---------------------------------------------------------

    def create_campaign(
        self,
        *,
        name: str,
        objective: str = "OUTCOME_TRAFFIC",
        status: str = "PAUSED",
        special_ad_categories: list[str] | None = None,
    ) -> str:
        """Create a campaign (PAUSED). Returns campaign_id."""
        if objective not in VALID_OBJECTIVES:
            raise MetaError(
                f"objective '{objective}' is not a valid ODAX value. "
                f"Use one of: {', '.join(sorted(VALID_OBJECTIVES))}."
            )
        # special_ad_categories MUST be a present array (even empty) — the
        # Graph form encoder needs the literal JSON string "[]".
        cats = special_ad_categories or []
        data = {
            "name": name,
            "objective": objective,
            "status": status,
            "special_ad_categories": _json_array(cats),
            # Required by Meta (error code 4834011) when the budget lives on
            # the ad set rather than the campaign (our model). False = ad sets
            # do NOT share budget with each other — sharing would blur the
            # agent's per-adset ±20% budget-clamp guarantees.
            "is_adset_budget_sharing_enabled": "false",
        }
        resp = self._post(f"{self.account_id}/campaigns", data=data)
        return _require_id(resp, "campaign")

    def create_adset(
        self,
        *,
        name: str,
        campaign_id: str,
        daily_budget_cents: int,
        objective: str = "OUTCOME_TRAFFIC",
        billing_event: str = "IMPRESSIONS",
        optimization_goal: str | None = None,
        bid_strategy: str = "LOWEST_COST_WITHOUT_CAP",
        countries: list[str] | None = None,
        age_min: int = 18,
        age_max: int = 65,
        status: str = "PAUSED",
    ) -> str:
        """Create an ad set (PAUSED) with a daily budget in cents. Returns adset_id.

        optimization_goal defaults to a value compatible with the objective +
        IMPRESSIONS billing. Budget lives HERE (not on the campaign) for the
        simple MVP — setting it at both levels errors.
        """
        if daily_budget_cents <= 0:
            raise MetaError("daily_budget must be > 0 (received "
                            f"{daily_budget_cents} minor units).")
        opt_goal = optimization_goal or _OPT_GOAL_FOR_OBJECTIVE.get(
            objective, "LINK_CLICKS"
        )
        targeting = {
            "geo_locations": {"countries": countries or ["US"]},
            "age_min": age_min,
            "age_max": age_max,
        }
        data = {
            "name": name,
            "campaign_id": campaign_id,
            "daily_budget": str(int(daily_budget_cents)),
            "billing_event": billing_event,
            "optimization_goal": opt_goal,
            "bid_strategy": bid_strategy,
            "targeting": _json_obj(targeting),
            "status": status,
        }
        resp = self._post(f"{self.account_id}/adsets", data=data)
        return _require_id(resp, "adset")

    def upload_image(self, *, image_path: str | None = None, image_url: str | None = None) -> str:
        """Upload an image to the account library. Returns the image_hash.

        Provide EITHER a local `image_path` (multipart file) OR a hosted
        `image_url` (downloaded then uploaded). Meta nests the hash under the
        filename: images.<filename>.hash.
        """
        if not image_path and not image_url:
            raise MetaError("upload_image needs image_path or image_url.")

        if image_url and not image_path:
            # Download the hosted creative (e.g. the fal.ai URL) then upload.
            try:
                with httpx.Client(timeout=60.0) as c:
                    resp = c.get(image_url)
                    resp.raise_for_status()
                    content = resp.content
            except httpx.HTTPError as e:
                raise MetaError(f"couldn't download creative image: {e}") from e
            filename = "creative.jpg"
            files = {"filename": (filename, content, "image/jpeg")}
            data = self._post(f"{self.account_id}/adimages", files=files)
            return _extract_image_hash(data)

        p = Path(image_path)  # type: ignore[arg-type]
        if not p.exists():
            raise MetaError(f"image file not found: {p}")
        with p.open("rb") as fh:
            files = {"filename": (p.name, fh, "image/jpeg")}
            data = self._post(f"{self.account_id}/adimages", files=files)
        return _extract_image_hash(data)

    def create_creative(
        self,
        *,
        name: str,
        image_hash: str,
        link: str,
        message: str = "",
        call_to_action_type: str = "LEARN_MORE",
        page_id: str | None = None,
    ) -> str:
        """Create an ad creative from an image_hash + link. Returns creative_id.

        page_id is MANDATORY — a creative must attach to a Facebook Page you
        control. Falls back to the configured META_PAGE_ID.
        """
        pid = page_id or self.page_id
        if not pid:
            raise MetaError(
                "META_PAGE_ID is required — a creative must attach to a Facebook "
                "Page you administer. Set META_PAGE_ID in ~/.veto/meta.env."
            )
        link_data: dict = {
            "link": link,
            "image_hash": image_hash,
            "call_to_action": {
                "type": call_to_action_type,
                "value": {"link": link},
            },
        }
        if message:
            link_data["message"] = message
        object_story_spec = {"page_id": pid, "link_data": link_data}
        data = {
            "name": name,
            "object_story_spec": _json_obj(object_story_spec),
        }
        resp = self._post(f"{self.account_id}/adcreatives", data=data)
        return _require_id(resp, "creative")

    def create_ad(
        self,
        *,
        name: str,
        adset_id: str,
        creative_id: str,
        status: str = "PAUSED",
    ) -> str:
        """Create the ad (PAUSED) linking the ad set + creative. Returns ad_id.

        Note: `creative` must be an object {"creative_id": "..."}, not a bare id.
        """
        data = {
            "name": name,
            "adset_id": adset_id,
            "creative": _json_obj({"creative_id": creative_id}),
            "status": status,
        }
        resp = self._post(f"{self.account_id}/ads", data=data)
        return _require_id(resp, "ad")

    # -- enumeration (OBSERVE) ---------------------------------------------

    def list_campaigns(self, *, status_filter: str | None = None) -> list[dict]:
        """List the account's campaigns (the `data` list).

        Fields: id, name, objective, status, daily_budget, lifetime_budget.
        Budgets come back as minor-unit strings when set at the campaign level
        (CBO); convert with `minor_to_usd`. `status_filter` (e.g. "ACTIVE",
        "PAUSED") narrows via the `effective_status` filter Meta accepts.
        """
        params = {
            "fields": "id,name,objective,status,daily_budget,lifetime_budget",
            "limit": 100,
        }
        if status_filter:
            params["effective_status"] = _json_array([status_filter])
        resp = self._get(f"{self.account_id}/campaigns", params=params)
        data = resp.get("data")
        return data if isinstance(data, list) else []

    def list_adsets(
        self,
        *,
        campaign_id: str | None = None,
        status_filter: str | None = None,
    ) -> list[dict]:
        """List ad sets — the key OBSERVE read (current daily_budget + status).

        Scopes to one campaign when `campaign_id` is given, else the whole
        account. Fields: id, name, status, effective_status, daily_budget,
        lifetime_budget, optimization_goal, campaign_id, learning_stage_info,
        created_time, updated_time. `daily_budget` is a minor-unit string —
        convert with `minor_to_usd`.

        `learning_stage_info` (a dict, e.g. {"status": "LEARNING"|"SUCCESS"|…})
        plus `created_time` let the ad-ops discipline gate honor Meta's learning
        phase and require a minimum delivery age before it acts. `effective_status`
        is Meta's *delivery* state (ACTIVE/PAUSED/… incl. why it isn't running),
        distinct from the configured `status`.
        """
        node = campaign_id or self.account_id
        params = {
            "fields": (
                "id,name,status,effective_status,daily_budget,lifetime_budget,"
                "optimization_goal,campaign_id,learning_stage_info,"
                "created_time,updated_time"
            ),
            "limit": 200,
        }
        if status_filter:
            params["effective_status"] = _json_array([status_filter])
        resp = self._get(f"{node}/adsets", params=params)
        data = resp.get("data")
        return data if isinstance(data, list) else []

    def list_ads(self, *, adset_id: str | None = None) -> list[dict]:
        """List ads (the `data` list) — inventory for pause/resume/creative.

        Scopes to one ad set when `adset_id` is given, else the account.
        Fields: id, name, status, creative, adset_id.
        """
        node = adset_id or self.account_id
        params = {"fields": "id,name,status,creative,adset_id", "limit": 200}
        resp = self._get(f"{node}/ads", params=params)
        data = resp.get("data")
        return data if isinstance(data, list) else []

    # -- mutation (ACT — gated by Veto in the controller BEFORE calling) ----

    def update_adset_budget(self, adset_id: str, daily_budget_cents: int) -> dict:
        """Raise/lower a live ad set's daily budget (cents). The primary
        autonomous mutation. MUST be authorized by Veto before it's called."""
        if daily_budget_cents <= 0:
            raise MetaError(
                "daily_budget must be > 0 "
                f"(received {daily_budget_cents} minor units)."
            )
        return self._post(adset_id, data={"daily_budget": str(int(daily_budget_cents))})

    def update_campaign_budget(self, campaign_id: str, daily_budget_cents: int) -> dict:
        """Raise/lower a campaign-level (CBO) daily budget (cents). Only used
        when a campaign carries the budget; the MVP puts it on the ad set."""
        if daily_budget_cents <= 0:
            raise MetaError(
                "daily_budget must be > 0 "
                f"(received {daily_budget_cents} minor units)."
            )
        return self._post(campaign_id, data={"daily_budget": str(int(daily_budget_cents))})

    def set_status(self, entity_id: str, status: str) -> dict:
        """Generic pause/resume for a campaign/adset/ad. `status` is one of
        PAUSED | ACTIVE. Pausing costs $0; still gated + logged by the loop."""
        st = (status or "").upper()
        if st not in ("PAUSED", "ACTIVE", "ARCHIVED", "DELETED"):
            raise MetaError(
                f"status '{status}' invalid — use PAUSED or ACTIVE."
            )
        return self._post(entity_id, data={"status": st})

    # backward-friendly alias (task inventory names this `update_entity_status`)
    def update_entity_status(self, entity_id: str, status: str) -> dict:
        return self.set_status(entity_id, status)

    # -- insights ----------------------------------------------------------

    def get_insights(
        self,
        *,
        level: str = "campaign",
        object_id: str | None = None,
        date_preset: str = "last_7d",
        fields: str | None = None,
    ) -> list[dict]:
        """Read insights (returns the `data` list; empty on a fresh sandbox).

        `object_id` scopes to a campaign/adset/ad; omit for account-level.
        Spend/cpc/ctr come back as decimal strings in DOLLARS.

        The default fields expose `impressions` and `spend` (the volume signals)
        plus `actions` — a list of {action_type, value} the ad-ops discipline gate
        sums into a conversions/actions count before it will act on an ad set.
        """
        node = object_id or self.account_id
        fields = fields or "campaign_name,spend,impressions,clicks,ctr,cpc,reach,actions"
        params = {"level": level, "date_preset": date_preset, "fields": fields}
        resp = self._get(f"{node}/insights", params=params)
        data = resp.get("data")
        return data if isinstance(data, list) else []


# ─── module-level helpers (json-encode for the Graph form encoder) ────────


def _json_array(values: list) -> str:
    import json

    return json.dumps(values)


def _json_obj(obj: dict) -> str:
    import json

    return json.dumps(obj)


def _require_id(resp: dict, kind: str) -> str:
    obj_id = resp.get("id")
    if not obj_id:
        raise MetaError(f"Meta did not return an id for the {kind}: {resp}")
    return str(obj_id)


def _extract_image_hash(data: dict) -> str:
    """images.<filename>.hash — the hash is nested under the filename key."""
    images = data.get("images")
    if isinstance(images, dict) and images:
        first = next(iter(images.values()))
        if isinstance(first, dict) and first.get("hash"):
            return str(first["hash"])
    raise MetaError(f"no image hash in adimages response: {data}")
