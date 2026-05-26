# Groups agent — 24/7 Telegram bot runtime.
#
# Build:    docker build -t veto-groups -f agents/groups/Dockerfile .
# Run:      docker run -d --restart unless-stopped \
#             -e TELEGRAM_BOT_TOKEN=... \
#             -e VETO_API_KEY=... \
#             -e VETO_AGENT_ID=... \
#             -e ANTHROPIC_API_KEY=... \
#             --name veto-groups veto-groups
#
# Or use Compose / Fly / Railway — see DEPLOY.md.

FROM python:3.12-slim AS base

# We install veto-agents from PyPI so the image is small + reproducible.
# For local-source builds (development): replace the `pip install` line
# with `pip install /workspace` and bind-mount the repo.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir veto-agents

# Run as non-root for safety
RUN useradd -m -u 1000 veto
USER veto
WORKDIR /home/veto

# Veto's state dir
ENV XDG_DATA_HOME=/home/veto/.local/share

# The agent reads credentials from env vars (preferred for deployments) or
# from ~/.veto-agents/credentials.yaml (which would be a mounted volume).
# Minimum required env vars:
#   TELEGRAM_BOT_TOKEN   — from @BotFather
#   VETO_API_KEY         — from `veto-agents account` after sign-in
#   VETO_AGENT_ID        — same
#   ANTHROPIC_API_KEY    — or NOUS_API_KEY / OPENAI_API_KEY etc.
# Optional:
#   EXA_API_KEY          — paid search (skipped if absent)
#   ASSEMBLYAI_API_KEY   — transcription (skipped if absent)

# Entrypoint runs the groups agent in long-running mode.
CMD ["veto-agents", "groups", "run", "--daemon"]
