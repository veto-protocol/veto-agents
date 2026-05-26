"""Groups agent — Telegram community bot.

Daemon mode (`veto-agents groups run --daemon`):
  - Polls Telegram for updates via python-telegram-bot
  - For each message in a group where the bot is @-mentioned:
      1. Build a brief context (recent history + the question)
      2. Estimate cost (Anthropic Claude Sonnet ≈ $0.003 per typical reply)
      3. Call Veto authorize → allow/deny/escalate
      4. If allow: call Anthropic, post the reply, attach receipt URL
      5. If deny: post "I'd need a higher cap for that — current cap is $X"
      6. If escalate: ping admin via DM with the verdict URL

Admin DM commands (sent privately to the bot, NOT in the group):
  /start       → onboarding DM
  /usage       → spend so far this month
  /receipts    → last 20 receipts with veto-ai.com/r/<uuid> links
  /setcap monthly|per-question|escalate-above <amount>
  /settone friendly|formal|playful|custom <prompt>
  /pause / /resume
  /alert "<regex>" → DM the admin when this matches in the group

v0.1 ships: @-mention reply via Claude, /usage, /receipts, /setcap,
            /settone, /pause, /resume, /alert.
v0.2 adds:  /summary today|week, voice-note transcription (AssemblyAI),
            /find <query>, /source url/file/pinned.

Self-hosted only — see DEPLOY.md for Fly / Railway / Compose / systemd
recipes. Veto is in the loop on every paid action, not at the door.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

from rich.console import Console

from ...credentials import get as get_credential
from ...veto_client import AuthorizeResult, VetoClient


logger = logging.getLogger(__name__)

# Estimated cost per typical reply via Claude Sonnet 4.6
# (~1.5k input tokens + ~250 output tokens at current pricing)
EST_REPLY_COST_USD = 0.005

# Per-chat in-memory history window (number of messages we feed Claude as
# context). Tightening this would lower per-reply cost; widening improves
# answer quality on long threads.
HISTORY_WINDOW = 25


def run(prompt: str, *, cfg, console: Console, auto_confirm: bool = False) -> None:
    """One-shot CLI invocation — used for `veto-agents groups <prompt>` from
    the terminal. Useful for testing the brain without the Telegram daemon.
    Falls through to a single authorized Anthropic call + prints the reply.
    """
    if not (cfg.api_key and cfg.agent_id):
        console.print(
            "[red]✗[/red] Not signed in. Run [cyan]veto-agents setup[/cyan]."
        )
        return

    api_key = get_credential("ANTHROPIC_API_KEY") or get_credential("OPENAI_API_KEY") or get_credential("NOUS_API_KEY")
    if not api_key:
        console.print(
            "[red]✗[/red] No LLM provider key. Run "
            "[cyan]veto-agents creds set ANTHROPIC_API_KEY <key>[/cyan] "
            "(or any provider — OpenAI, Nous Hermes, etc.)."
        )
        return

    console.print(f"\n[bold]Brief:[/bold] {prompt}\n")

    # Veto authorize
    client = VetoClient(api_base=cfg.veto_api_base, api_key=cfg.api_key)
    try:
        result = client.authorize(
            agent_id=cfg.agent_id,
            action_type="api_call",
            merchant="api.anthropic.com",
            amount=EST_REPLY_COST_USD,
            currency="USD",
            description=f"groups one-shot: {prompt[:120]}",
            context={"agent_type": "groups", "tool_name": "anthropic.claude", "mode": "cli"},
        )
    except Exception as e:
        console.print(f"  [red]✗ Veto authorize failed:[/red] {e}")
        return
    finally:
        # We close after the reply below; keep open in success case
        pass

    if result.verdict != "allow":
        console.print(f"  [red]✗[/red] {result.verdict.upper()}: {', '.join(result.reason_codes) or 'policy'}")
        if result.receipt_url:
            console.print(f"  Receipt: {result.receipt_url}")
        client.close()
        return

    console.print(f"  [green]✓[/green] Veto allowed · receipt {result.receipt_url}")

    # Real Anthropic call
    try:
        reply = _anthropic_chat(prompt, api_key=api_key)
    except Exception as e:
        console.print(f"  [red]✗ Anthropic call failed:[/red] {e}")
        client.close()
        return

    console.print(f"\n[bold]Reply:[/bold]\n{reply}\n")
    client.close()


def run_daemon(cfg, console: Console) -> None:
    """Long-running Telegram bot. Started by `veto-agents groups run --daemon`."""
    if not (cfg.api_key and cfg.agent_id):
        console.print("[red]✗[/red] Not signed in. Run [cyan]veto-agents setup[/cyan].")
        return

    bot_token = get_credential("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        console.print(
            "[red]✗[/red] No Telegram bot token. Get one from @BotFather (free, 60s), then\n"
            "  [cyan]veto-agents creds set TELEGRAM_BOT_TOKEN <token>[/cyan]"
        )
        return

    brain_key = (
        get_credential("ANTHROPIC_API_KEY")
        or get_credential("OPENAI_API_KEY")
        or get_credential("NOUS_API_KEY")
    )
    if not brain_key:
        console.print(
            "[red]✗[/red] No LLM provider key. Run\n"
            "  [cyan]veto-agents creds set ANTHROPIC_API_KEY <key>[/cyan]\n"
            "(or any provider — OpenAI / Hermes-via-Nous-Portal)."
        )
        return

    try:
        from telegram.ext import (
            ApplicationBuilder,
            CommandHandler,
            MessageHandler,
            filters,
            ContextTypes,
        )
    except ImportError:
        console.print(
            "[red]✗[/red] The Groups agent needs extra libraries that aren't installed yet.\n"
            "  Reinstall with the groups extras:\n\n"
            "    [cyan]curl -fsSL https://raw.githubusercontent.com/veto-protocol/veto-agents/main/install.sh | bash[/cyan]\n\n"
            "  (or run [dim]pipx install --force 'veto-agents[groups]'[/dim] if you prefer.)\n"
        )
        return

    console.print("\n[bold cyan]Veto Agents — Groups daemon starting[/bold cyan]")
    console.print(f"  [dim]agent_id:[/dim] {cfg.agent_id}")
    console.print(f"  [dim]brain:[/dim]    {('anthropic' if get_credential('ANTHROPIC_API_KEY') else 'openai/nous')}")
    console.print(f"  [dim]state:[/dim]    {cfg.veto_api_base}")
    console.print()

    # Per-chat sliding-window message history for context
    chat_history: dict[int, deque] = {}
    # Per-chat config (overrides from /setcap, /settone, /pause)
    chat_config: dict[int, dict[str, Any]] = {}

    veto_client = VetoClient(api_base=cfg.veto_api_base, api_key=cfg.api_key)

    # ── Handlers ─────────────────────────────────────────────

    async def on_message(update, context):
        msg = update.effective_message
        if msg is None or msg.text is None:
            return
        chat_id = update.effective_chat.id
        bot_user = await context.bot.get_me()

        # Track history (everyone's messages, not just mentions)
        history = chat_history.setdefault(chat_id, deque(maxlen=HISTORY_WINDOW))
        history.append({"from": msg.from_user.first_name if msg.from_user else "?", "text": msg.text})

        # Respond only when @-mentioned (or in 1:1 DM)
        is_dm = update.effective_chat.type == "private"
        is_mention = bot_user.username and f"@{bot_user.username}" in msg.text
        if not (is_dm or is_mention):
            return

        # Paused?
        if chat_config.get(chat_id, {}).get("paused"):
            return

        question = msg.text.replace(f"@{bot_user.username}", "").strip() if is_mention else msg.text

        # Build context block for the LLM
        history_text = "\n".join(f"{m['from']}: {m['text']}" for m in list(history)[-12:])
        tone = chat_config.get(chat_id, {}).get("tone", "friendly")
        system_prompt = (
            f"You are a community assistant in a Telegram group. Tone: {tone}. "
            f"Reply concisely (2-3 sentences max) based on recent group context "
            f"and the user's question. If you don't know, say so plainly."
        )
        user_prompt = f"Recent group context:\n{history_text}\n\nQuestion: {question}"

        # Veto authorize
        try:
            result = veto_client.authorize(
                agent_id=cfg.agent_id,
                action_type="api_call",
                merchant="api.anthropic.com",
                amount=EST_REPLY_COST_USD,
                currency="USD",
                description=f"groups reply: {question[:80]}",
                context={
                    "agent_type": "groups",
                    "tool_name": "anthropic.claude",
                    "chat_id": str(chat_id),
                    "telegram_user": str(msg.from_user.id) if msg.from_user else "",
                },
            )
        except Exception as e:
            logger.exception("Veto authorize failed")
            await msg.reply_text(f"⚠ Couldn't reach Veto: {e}")
            return

        if result.verdict == "deny":
            reasons = ", ".join(result.reason_codes) or "policy"
            await msg.reply_text(
                f"I'd need approval for that — Veto denied ({reasons}).\n"
                f"Receipt: {result.receipt_url or '—'}"
            )
            return
        if result.verdict == "escalate":
            await msg.reply_text(
                f"Escalating to your admin — they'll review and reply.\n"
                f"Receipt: {result.receipt_url or '—'}"
            )
            return

        # Allowed → call the LLM
        try:
            reply = await asyncio.to_thread(_anthropic_chat, user_prompt, brain_key, system_prompt)
        except Exception as e:
            logger.exception("LLM call failed")
            await msg.reply_text(f"⚠ Brain call failed: {e}")
            return

        # Append "receipt" link in a subtle way
        if result.receipt_url:
            reply = f"{reply}\n\n_receipt: {result.receipt_url}_"
        await msg.reply_text(reply, parse_mode="Markdown")

    async def on_start(update, context):
        await update.message.reply_text(
            "Hi! I'm your Veto-governed community bot. Add me to a group as admin "
            "and @-mention me to ask questions. DM me /usage to see spend so far."
        )

    async def on_usage(update, context):
        # In v0.1 this is a placeholder — real per-bot usage aggregation
        # lives in the Veto backend (we'd query /api/v1/receipts/?agent_id=...)
        await update.message.reply_text(
            f"Usage breakdown coming in v0.2. For now, see your receipts:\n"
            f"https://veto-ai.com/agents/{cfg.agent_id}/receipts"
        )

    async def on_setcap(update, context):
        # Simple in-memory cap override; persistent caps live in the Veto policy
        # YAML — we'd PATCH to /api/v1/policies/<id> in v0.2.
        args = context.args
        if len(args) != 2:
            await update.message.reply_text("Usage: /setcap monthly|per-question|escalate-above <amount>")
            return
        kind, amount = args[0], args[1]
        try:
            amt = float(amount)
        except ValueError:
            await update.message.reply_text("Amount must be a number.")
            return
        chat_id = update.effective_chat.id
        chat_config.setdefault(chat_id, {})[f"cap_{kind}"] = amt
        await update.message.reply_text(
            f"✓ {kind} cap set to ${amt:.2f}.\n"
            f"_Note: in v0.1 caps apply in-memory; v0.2 will write through to your "
            f"Veto policy server-side (so the cap survives daemon restarts)._",
            parse_mode="Markdown",
        )

    async def on_settone(update, context):
        args = context.args
        if not args:
            await update.message.reply_text("Usage: /settone friendly|formal|playful|<custom>")
            return
        tone = " ".join(args)
        chat_id = update.effective_chat.id
        chat_config.setdefault(chat_id, {})["tone"] = tone
        await update.message.reply_text(f"✓ Tone set to: {tone}")

    async def on_pause(update, context):
        chat_id = update.effective_chat.id
        chat_config.setdefault(chat_id, {})["paused"] = True
        await update.message.reply_text("Paused. Run /resume to bring me back.")

    async def on_resume(update, context):
        chat_id = update.effective_chat.id
        chat_config.setdefault(chat_id, {})["paused"] = False
        await update.message.reply_text("Back. Reply when @-mentioned.")

    # ── Wire + run ───────────────────────────────────────────

    app = ApplicationBuilder().token(bot_token).build()
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("usage", on_usage))
    app.add_handler(CommandHandler("setcap", on_setcap))
    app.add_handler(CommandHandler("settone", on_settone))
    app.add_handler(CommandHandler("pause", on_pause))
    app.add_handler(CommandHandler("resume", on_resume))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    console.print("[green]✓[/green] Bot starting. Press Ctrl-C to stop.\n")
    try:
        app.run_polling(allowed_updates=["message"], close_loop=False)
    finally:
        veto_client.close()


# ── LLM call (sync — wrapped in to_thread above for async caller) ─────────

def _anthropic_chat(user_prompt: str, api_key: str, system_prompt: str = "") -> str:
    """Single Claude call. Sync. The daemon runs this in a thread so the
    asyncio event loop stays responsive."""
    try:
        import anthropic
    except ImportError:
        return (
            "[anthropic package not installed; install the groups extra: "
            "pipx inject veto-agents 'veto-agents[groups]']"
        )
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=system_prompt or "You are a concise, helpful community assistant.",
        messages=[{"role": "user", "content": user_prompt}],
    )
    parts = []
    for block in resp.content:
        if hasattr(block, "text"):
            parts.append(block.text)
    return "\n".join(parts).strip() or "(no reply)"
