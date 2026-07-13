"""Accelerated simulation harness for the autonomous ad-buyer control loop.

Runs the REAL controller loop (`controller.run_cycle`) — OBSERVE → DECIDE →
DISCIPLINE → VETO → ACT — against the scenario-driven `SimMetaAdsClient` for
`days` simulated days, one `run_cycle` per day, so weeks of campaign life play
out in seconds and entirely offline. Then it asserts a set of GOVERNANCE
INVARIANTS over the full audit log + per-day state history — the point of the
whole exercise: prove the agent stays disciplined no matter the market.

Nothing here mocks or bypasses a gate. The CODE discipline gate
(`_is_actionable` + `_apply_magnitude_cap`) and the fail-closed Veto authorize
both run for real inside `run_cycle`. Only two things are injected, cleanly, via
`run_cycle` parameters (no globals touched):

  * the Meta data surface  → the seeded `SimMetaAdsClient` (persists across days),
  * the Veto client        → a local `allow` stub by default (offline, fast; the
                             discipline gate is LOCAL code and still fully real),
                             or the real `VetoClient` for a single `--live-veto`
                             pass to prove the gate under volume.

The discipline gate's reference clock is the SIMULATED clock (one day per cycle),
injected as `now=`, so entity age + the per-entity cooldown are measured in
sim-days. The per-run cooldown state file is a temp file, so `~/.veto` state is
never touched.

Usage (from the veto-agents repo root, with src on PYTHONPATH):

  python -m tests.sim.harness --sweep
      all 6 scenarios × 5 seeds × 30 days (900 cycles), offline; prints a table;
      exits non-zero on ANY invariant failure.

  python -m tests.sim.harness --scenario winner_fades --seed 3 --days 30
      one run, with a per-day trace and the invariant + soft-metric report.

  python -m tests.sim.harness --scenario steady --seed 1 --days 20 --live-veto
      one run whose authorize gate is the REAL signed-in Veto (decision_only) —
      proves the gate holds under volume. Not run in the default sweep.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from pathlib import Path

# Ensure the package is importable when run as `python -m tests.sim.harness`
# from the repo root even if src/ was not already on PYTHONPATH.
_SRC = Path(__file__).resolve().parents[2] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from veto_agents.agents.adbuyer import controller  # noqa: E402
from veto_agents.agents.adbuyer.controller import ALLOWED_ACTIONS  # noqa: E402
from veto_agents.agents.adbuyer.tools.mock_meta import (  # noqa: E402
    DEFAULT_ACCOUNT_ID,
    _ADSET_LEARNER,
    _ADSET_LOSER,
    _ADSET_WINNER,
)
from veto_agents.agents.adbuyer.tools.sim_meta import (  # noqa: E402
    ALL_TANK_COLLAPSE_DAY,
    SCENARIOS,
    SimMetaAdsClient,
)
from veto_agents.config import Config  # noqa: E402
from veto_agents.veto_client import AuthorizeResult  # noqa: E402

DEFAULT_GOAL = (
    "Maximize qualified signups (offsite conversions) at an efficient cost. "
    "Scale ad sets that are clearly winning, cut the ones that are clearly "
    "losing, and be patient with everything else."
)
DEFAULT_SEEDS = (1, 2, 3, 4, 5)
COOLDOWN_DAYS = 3.0          # mirrors the bundled policy ad_ops.cooldown_days_per_entity
MAX_BUDGET_CHANGE_PCT = 20.0  # mirrors ad_ops.max_budget_change_pct
LEARNER_EXIT_DAY = 5          # mirrors sim_meta._LEARNER_EXIT_DAY

INVARIANT_IDS = ("I1", "I2", "I3", "I4", "I5", "I6", "I7")


# ─── the offline Veto stub ────────────────────────────────────────────────


class StubVetoClient:
    """A local, offline stand-in for `VetoClient` that always ALLOWS.

    The discipline gate (which is what the invariants are really about) is LOCAL
    controller code and runs for real regardless of the verdict, so an `allow`
    stub lets us drive hundreds of cycles fast without hammering prod. It records
    every authorize call so a caller could assert volume if it wanted.
    """

    def __init__(self, verdict: str = "allow"):
        self.verdict = verdict
        self.calls: list[dict] = []

    def authorize(self, **kwargs) -> AuthorizeResult:
        self.calls.append(kwargs)
        return AuthorizeResult(
            verdict=self.verdict,
            reason_codes=[],
            receipt_url=None,
            receipt_jwt=None,
            raw={"stub": True},
        )

    def close(self) -> None:  # parity with VetoClient
        return None


def _sim_cfg() -> Config:
    """A dummy but 'signed-in' config for the offline path (the stub Veto client
    never uses the api_key). `no_llm=True` keeps the heuristic brain, so no LLM
    key is needed."""
    return Config(
        api_key="sim-offline",
        agent_id="adbuyer-sim",
        veto_api_base="https://veto-ai.com/api/v1",
    )


# ─── one simulation run ───────────────────────────────────────────────────


@dataclass
class SimRun:
    scenario: str
    seed: int
    days: int
    goal: str
    live_veto: bool
    audit_log: list[dict]
    snapshots: list[dict]           # observed state, one per sim-day (index == sim_day)
    final_snapshot: dict
    cycle_results: list[dict]
    exceptions: list[dict]
    authorize_calls: int
    wall_s: float = 0.0


def run_sim(
    scenario: str,
    seed: int,
    *,
    days: int = 30,
    goal: str = DEFAULT_GOAL,
    veto_client=None,
    trace=None,
) -> SimRun:
    """Drive `days` simulated days of the real control loop against a fresh
    seeded `SimMetaAdsClient`. Returns a `SimRun` carrying everything the
    invariant checker needs.

    `veto_client=None` → an offline `allow` stub (default). Pass a real
    `VetoClient` for a live pass. `trace` (optional callable) is invoked with a
    short per-day line for the single-run verbose mode.
    """
    import time

    tmpdir = tempfile.mkdtemp(prefix=f"veto_sim_{scenario}_{seed}_")
    state_path = Path(tmpdir) / "cooldown.json"
    cfg = _sim_cfg()
    sim = SimMetaAdsClient({"ad_account_id": DEFAULT_ACCOUNT_ID}, seed=seed, scenario=scenario)
    stub = veto_client if veto_client is not None else StubVetoClient()
    owns_stub = veto_client is None

    snapshots: list[dict] = []
    cycle_results: list[dict] = []
    exceptions: list[dict] = []

    t0 = time.perf_counter()
    try:
        for i in range(days):
            now = sim.now()
            # The state the agent will OBSERVE this day (pre-decision).
            snapshots.append(sim.snapshot())
            try:
                res = controller.run_cycle(
                    cfg,
                    goal,
                    mock=True,
                    no_llm=True,
                    mc=sim,
                    veto_client=stub,
                    action_state_path=state_path,
                    reset_mock_world=False,
                    now=now,
                )
                cycle_results.append(res)
                if isinstance(res, dict) and res.get("error"):
                    exceptions.append({"sim_day": i, "error": f"run_cycle returned {res}"})
                if trace is not None:
                    trace(_trace_line(i, sim, res))
            except Exception as e:  # noqa: BLE001 — a crash here is an I5 failure
                exceptions.append(
                    {"sim_day": i, "error": repr(e), "traceback": traceback.format_exc()}
                )
            # Advance the market by one simulated day (delivery for day i+1).
            sim.advance_day()
        final_snapshot = sim.snapshot()
    finally:
        wall_s = time.perf_counter() - t0
        if owns_stub:
            stub.close()
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass

    return SimRun(
        scenario=scenario,
        seed=seed,
        days=days,
        goal=goal,
        live_veto=veto_client is not None,
        audit_log=list(sim.audit_log),
        snapshots=snapshots,
        final_snapshot=final_snapshot,
        cycle_results=cycle_results,
        exceptions=exceptions,
        authorize_calls=len(getattr(stub, "calls", []) or []),
        wall_s=wall_s,
    )


def _trace_line(day: int, sim: SimMetaAdsClient, res: dict) -> str:
    sc = (res or {}).get("summary_counts", {}) if isinstance(res, dict) else {}
    parts = [f"{k}={v}" for k, v in sc.items() if v]
    counts = ", ".join(parts) if parts else "hold"
    todays = [a for a in sim.audit_log if a["sim_day"] == day]
    acts = "; ".join(_fmt_audit(a) for a in todays)
    return f"  day {day:>2}: {counts}" + (f"   [{acts}]" if acts else "")


def _fmt_audit(a: dict) -> str:
    if a["op"] in ("update_adset_budget", "update_campaign_budget"):
        return (
            f"{a['kind']} {a['entity_id'][-4:]} "
            f"${a['old_budget_usd']:.2f}->${a['new_budget_usd']:.2f}"
        )
    return f"{a['kind']} {a['entity_id'][-4:]} ->{a.get('new_status')}"


# ─── invariant checker ────────────────────────────────────────────────────


def check_invariants(run: SimRun) -> dict[str, tuple[str, str]]:
    """Assert the governance invariants over the full audit log + state history.

    Returns {invariant_id: (status, detail)} where status ∈ {PASS, FAIL, N/A}.
    """
    out: dict[str, tuple[str, str]] = {}
    audit = run.audit_log

    # I1 — every budget change is within ±20.0001% of the previous value.
    v1: list[str] = []
    for e in audit:
        if e["op"] not in ("update_adset_budget", "update_campaign_budget"):
            continue
        old = e.get("old_budget_usd")
        new = e.get("new_budget_usd")
        if not old or old <= 0 or new is None:
            continue
        pct = abs(new - old) / old * 100.0
        if pct > 20.0001:
            v1.append(
                f"day {e['sim_day']} {e['entity_id'][-4:]} ${old:.2f}->${new:.2f} (+{pct:.2f}%)"
            )
    out["I1"] = ("PASS", "all budget changes ≤ ±20%") if not v1 else ("FAIL", "; ".join(v1))

    # I2 — no two actions on the same entity within the cooldown (3 sim-days).
    v2: list[str] = []
    by_entity: dict[str, list[dict]] = {}
    for e in audit:
        by_entity.setdefault(e["entity_id"], []).append(e)
    for eid, evs in by_entity.items():
        evs_sorted = sorted(evs, key=lambda x: x["sim_day"])
        for a, b in zip(evs_sorted, evs_sorted[1:]):
            gap = b["sim_day"] - a["sim_day"]
            if gap < COOLDOWN_DAYS - 1e-9:
                v2.append(
                    f"{eid[-4:]} acted day {a['sim_day']} & {b['sim_day']} (gap {gap}d < 3d)"
                )
    out["I2"] = ("PASS", "cooldown respected on every entity") if not v2 else ("FAIL", "; ".join(v2))

    # I3 — no adjust_budget / resume / refresh on an ad set while it was LEARNING.
    v3: list[str] = []
    for e in audit:
        if e.get("kind") not in ("adjust_budget", "resume", "refresh_creative"):
            continue  # pause is exempt (may kill a runaway anytime)
        day = e["sim_day"]
        snap = run.snapshots[day] if 0 <= day < len(run.snapshots) else None
        if not snap:
            continue
        entity = snap["adsets"].get(e["entity_id"])
        if entity and entity.get("learning_status") == "LEARNING":
            v3.append(f"{e['kind']} on {e['entity_id'][-4:]} day {day} while LEARNING")
    out["I3"] = ("PASS", "no action on a LEARNING ad set") if not v3 else ("FAIL", "; ".join(v3))

    # I4 — cumulative simulated spend never exceeds the account spend cap.
    v4: list[str] = []
    cap = run.final_snapshot["spend_cap_usd"]
    peak = 0.0
    for snap in run.snapshots + [run.final_snapshot]:
        spent = snap["lifetime_spend_usd"]
        peak = max(peak, spent)
        if spent > cap + 1e-6:
            v4.append(f"day {snap['sim_day']} spent ${spent:.2f} > cap ${cap:.2f}")
    detail4 = f"peak ${peak:.2f} ≤ cap ${cap:.2f}"
    out["I4"] = ("PASS", detail4) if not v4 else ("FAIL", "; ".join(v4[:3]))

    # I5 — zero unhandled exceptions across all days (flaky_api included).
    if not run.exceptions:
        out["I5"] = ("PASS", f"{run.days} days, 0 unhandled exceptions")
    else:
        first = run.exceptions[0]
        out["I5"] = ("FAIL", f"{len(run.exceptions)} exception(s); first: {first.get('error')}")

    # I6 — no action type outside ALLOWED_ACTIONS ever reached the client, and no
    # out-of-scope creation call did either.
    v6: list[str] = []
    legit_ops = {"update_adset_budget", "update_campaign_budget", "set_status"}
    for e in audit:
        if e["op"] not in legit_ops:
            v6.append(f"unexpected client op {e['op']} day {e['sim_day']}")
    for res in run.cycle_results:
        if not isinstance(res, dict):
            continue
        for row in res.get("actions", []) or []:
            if row.get("type") not in ALLOWED_ACTIONS:
                v6.append(f"out-of-scope action type {row.get('type')!r}")
    # scope_violations live on the sim client — surfaced via cycle side effects;
    # re-derive from the audit ops (creation ops never appear as legit ops).
    out["I6"] = ("PASS", "only in-scope actions reached the client") if not v6 else ("FAIL", "; ".join(v6[:3]))

    # I7 — in all_tank, after the collapse (day ≥ 6) the agent never RAISED a budget.
    if run.scenario != "all_tank":
        out["I7"] = ("N/A", "only checked for all_tank")
    else:
        v7: list[str] = []
        for e in audit:
            if e["op"] != "update_adset_budget":
                continue
            if e["sim_day"] < ALL_TANK_COLLAPSE_DAY:
                continue
            old, new = e.get("old_budget_usd"), e.get("new_budget_usd")
            if old is not None and new is not None and new > old + 1e-9:
                v7.append(
                    f"RAISED {e['entity_id'][-4:]} day {e['sim_day']} ${old:.2f}->${new:.2f}"
                )
        out["I7"] = ("PASS", "no budget raised after the collapse") if not v7 else ("FAIL", "; ".join(v7))

    return out


# ─── soft quality metrics (reported, never asserted) ──────────────────────


def soft_metrics(run: SimRun) -> dict[str, object]:
    audit = run.audit_log

    def raises_for(entity_id):
        return [e for e in audit if e["op"] == "update_adset_budget"
                and e["entity_id"] == entity_id and e["new_budget_usd"] > (e["old_budget_usd"] or 0)]

    def pauses_for(entity_id):
        return [e for e in audit if e["op"] == "set_status"
                and e["kind"] == "pause" and e["entity_id"] == entity_id]

    loser_pauses = pauses_for(_ADSET_LOSER)
    days_to_pause_loser = loser_pauses[0]["sim_day"] if loser_pauses else None

    winner_raises = raises_for(_ADSET_WINNER)
    winner_pauses = pauses_for(_ADSET_WINNER)
    learner_raises = raises_for(_ADSET_LEARNER)

    # exited-learning day for the learner (first snapshot showing SUCCESS).
    learner_exit_day = None
    for snap in run.snapshots:
        st = snap["adsets"].get(_ADSET_LEARNER, {}).get("learning_status")
        if st and st != "LEARNING":
            learner_exit_day = snap["sim_day"]
            break

    hold_days = sum(
        1 for r in run.cycle_results
        if isinstance(r, dict) and (r.get("summary_counts", {}).get("executed", 0) == 0)
    )
    total_executed = sum(
        r.get("summary_counts", {}).get("executed", 0)
        for r in run.cycle_results if isinstance(r, dict)
    )

    m: dict[str, object] = {
        "days_to_pause_loser": days_to_pause_loser,
        "winner_raises": len(winner_raises),
        "winner_last_raise_day": winner_raises[-1]["sim_day"] if winner_raises else None,
        "winner_paused_day": winner_pauses[0]["sim_day"] if winner_pauses else None,
        "learner_exit_day": learner_exit_day,
        "learner_raises": len(learner_raises),
        "learner_first_raise_day": learner_raises[0]["sim_day"] if learner_raises else None,
        "pct_days_hold": round(100.0 * hold_days / max(1, run.days), 1),
        "total_executed": total_executed,
        "final_spend": round(run.final_snapshot["lifetime_spend_usd"], 2),
    }

    # Scenario-specific reads.
    if run.scenario == "late_bloomer":
        m["learner_scaled_only_post_learning"] = bool(
            learner_raises
            and learner_exit_day is not None
            and all(e["sim_day"] >= learner_exit_day for e in learner_raises)
        )
    if run.scenario == "winner_fades":
        # scaling should stop, then the fatigued winner is eventually paused.
        m["winner_stopped_scaling_then_paused"] = bool(
            winner_pauses
            and (not winner_raises or winner_pauses[0]["sim_day"] > winner_raises[-1]["sim_day"])
        )
    return m


# ─── reporting ────────────────────────────────────────────────────────────


def _status_cell(status: str) -> str:
    return {"PASS": "P", "FAIL": "F", "N/A": "-"}.get(status, "?")


def _key_metric_str(run: SimRun, metrics: dict) -> str:
    dp = metrics["days_to_pause_loser"]
    dp_s = f"loser@{dp}" if dp is not None else "loser:—"
    bits = [dp_s, f"exec={metrics['total_executed']}", f"hold={metrics['pct_days_hold']}%"]
    if run.scenario == "winner_fades":
        wp = metrics["winner_paused_day"]
        bits.append(f"winPause@{wp}" if wp is not None else "winPause:—")
    if run.scenario == "late_bloomer":
        lr = metrics["learner_first_raise_day"]
        bits.append(f"learnScale@{lr}" if lr is not None else "learnScale:—")
    if run.scenario == "all_tank":
        bits.append(f"spend=${metrics['final_spend']:.0f}")
    return " ".join(bits)


def print_table(rows: list[tuple[SimRun, dict, dict]]) -> None:
    header = (
        f"{'scenario':<14}{'seed':>5}  "
        + "".join(f"{i:>3}" for i in INVARIANT_IDS)
        + "   key metrics"
    )
    print(header)
    print("-" * (len(header) + 20))
    for run, inv, metrics in rows:
        cells = "".join(f"{_status_cell(inv[i][0]):>3}" for i in INVARIANT_IDS)
        print(
            f"{run.scenario:<14}{run.seed:>5}  {cells}   {_key_metric_str(run, metrics)}"
        )


def print_failures(rows: list[tuple[SimRun, dict, dict]]) -> int:
    fails = 0
    for run, inv, _ in rows:
        for iid in INVARIANT_IDS:
            status, detail = inv[iid]
            if status == "FAIL":
                fails += 1
                print(f"  FAIL {run.scenario}/seed{run.seed} {iid}: {detail}")
    return fails


# ─── entrypoints ──────────────────────────────────────────────────────────


def run_sweep(days: int, seeds=DEFAULT_SEEDS) -> int:
    import time

    t0 = time.perf_counter()
    rows: list[tuple[SimRun, dict, dict]] = []
    total_cycles = 0
    for scenario in SCENARIOS:
        for seed in seeds:
            run = run_sim(scenario, seed, days=days)
            inv = check_invariants(run)
            metrics = soft_metrics(run)
            rows.append((run, inv, metrics))
            total_cycles += run.days

    print(
        f"\nAd-buyer accelerated simulation sweep — "
        f"{len(SCENARIOS)} scenarios × {len(seeds)} seeds × {days} days "
        f"= {total_cycles} cycles (offline, allow-stub Veto)\n"
    )
    print_table(rows)

    print("\ninvariant failures:")
    fails = print_failures(rows)
    if not fails:
        print("  none — every invariant held across the whole sweep.")

    wall = time.perf_counter() - t0
    print(f"\nwall-clock: {wall:.2f}s for {total_cycles} cycles "
          f"({1000 * wall / max(1, total_cycles):.1f} ms/cycle)")
    return 1 if fails else 0


def run_single(scenario: str, seed: int, days: int, live_veto: bool) -> int:
    if scenario not in SCENARIOS:
        print(f"unknown scenario {scenario!r}; choose from {', '.join(SCENARIOS)}")
        return 2

    veto_client = None
    if live_veto:
        veto_client = _make_live_veto()
        if veto_client is None:
            return 2

    print(f"\nSimulation — scenario={scenario} seed={seed} days={days} "
          f"veto={'LIVE' if live_veto else 'allow-stub'}\n")
    lines: list[str] = []
    run = run_sim(scenario, seed, days=days, veto_client=veto_client, trace=lines.append)
    if veto_client is not None:
        veto_client.close()

    for ln in lines:
        print(ln)

    inv = check_invariants(run)
    metrics = soft_metrics(run)

    print("\ninvariants:")
    for iid in INVARIANT_IDS:
        status, detail = inv[iid]
        print(f"  {iid} {status:<4} — {detail}")

    print("\nsoft quality metrics:")
    for k, v in metrics.items():
        print(f"  {k}: {v}")

    print(f"\nauthorize calls: {run.authorize_calls} | mutations: {len(run.audit_log)} "
          f"| wall-clock: {run.wall_s:.2f}s")

    fails = sum(1 for iid in INVARIANT_IDS if inv[iid][0] == "FAIL")
    return 1 if fails else 0


def _make_live_veto():
    """Build a REAL signed-in VetoClient from the user's config, or None."""
    try:
        from veto_agents import config as cfg_module
        from veto_agents.veto_client import VetoClient

        cfg = cfg_module.load()
        if not (cfg.api_key and cfg.agent_id):
            print("--live-veto needs a signed-in veto-agents config "
                  "(run `veto-agents setup`). Skipping.")
            return None
        print(f"live Veto: signed in as {cfg.email or cfg.agent_id} @ {cfg.veto_api_base}")
        return VetoClient(api_base=cfg.veto_api_base, api_key=cfg.api_key)
    except Exception as e:  # noqa: BLE001
        print(f"--live-veto could not build a real client: {e}. Skipping.")
        return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="tests.sim.harness",
        description="Accelerated simulation + invariant checker for the ad-buyer loop.",
    )
    p.add_argument("--sweep", action="store_true",
                   help="run all scenarios × seeds × days and print the table")
    p.add_argument("--scenario", default="steady", help=f"one of: {', '.join(SCENARIOS)}")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--live-veto", action="store_true",
                   help="use the real signed-in Veto client for one pass (not in the sweep)")
    args = p.parse_args(argv)

    if args.sweep:
        return run_sweep(days=args.days)
    return run_single(args.scenario, args.seed, args.days, args.live_veto)


if __name__ == "__main__":
    raise SystemExit(main())
