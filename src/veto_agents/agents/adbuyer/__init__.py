"""Ad-buyer agent — a 24/7 AUTONOMOUS, Veto-governed Meta (FB/IG) ad buyer.

Deploy once with a standing GOAL + a Veto policy, then walk away. The agent runs
its own control loop forever:

    OBSERVE  pull Meta insights + current campaigns/adsets/budgets + account
             amount_spent / spend_cap.
    DECIDE   an LLM brain proposes in-scope actions (adjust_budget / pause /
             resume / refresh_creative) on EXISTING entities.
    GOVERN   every spend-implicating action is authorized by Veto BEFORE any
             Meta write — governing the agent's OWN intent (fail-closed).
    ACT      apply the one mutation via the Meta client.

There is no per-action human consent gate: the human deployed once + set policy;
Veto is the ongoing guardrail. See agents/adbuyer/README.md for the full story.
"""

from typing import TYPE_CHECKING

__all__ = ["run", "run_daemon"]

# Lazy (PEP 562) re-export so that `import veto_agents.agents.adbuyer` (or a
# sibling subpackage) does NOT eagerly drag in `.agent` and its heavy
# `controller` → meta / veto / credentials chain. `from veto_agents.agents.adbuyer
# import run` still works: it triggers the import on first attribute access.
if TYPE_CHECKING:  # for type-checkers / IDEs only, never executed at runtime
    from .agent import run, run_daemon


def __getattr__(name: str):
    if name in ("run", "run_daemon"):
        from . import agent
        return getattr(agent, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(list(globals().keys()) + __all__)
