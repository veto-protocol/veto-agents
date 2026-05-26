"""Groups agent — 24/7 Telegram community bot, Veto-governed.

Two entry points:
- `run(prompt, cfg, console, auto_confirm)`  → one-shot CLI invocation (sanity-check)
- `run_daemon(cfg, console)`                 → long-running Telegram bot (production use)

The daemon is what `veto-agents groups run --daemon` calls. See DEPLOY.md
in the agents/groups/ directory of the repo for hosting recipes.
"""

from .agent import run, run_daemon

__all__ = ["run", "run_daemon"]
