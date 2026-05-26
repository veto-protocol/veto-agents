# Groups Agent — Deploy Guide

Run the Groups bot 24/7. Pick the option that matches what you (or your AI coding agent) already use. **Read [SECURITY.md](../../SECURITY.md) first if you're new to handling API keys.**

Required env vars at runtime:
- `TELEGRAM_BOT_TOKEN` (from @BotFather)
- `VETO_API_KEY` (run `veto-agents account` to see yours)
- `VETO_AGENT_ID` (same)
- `ANTHROPIC_API_KEY` *or* `NOUS_API_KEY` *or* `OPENAI_API_KEY` (pick one — the brain)

Optional:
- `EXA_API_KEY` (paid search for question-answering)
- `ASSEMBLYAI_API_KEY` (voice-note transcription)

---

## Fly.io  (recommended — free tier, 60-second deploy)

```bash
# from the cloned repo's agents/groups/ dir
fly launch --no-deploy --name veto-groups-yourname

# Set secrets (NEVER paste these in plain command-line invocations; Fly
# encrypts at rest + injects only at runtime):
fly secrets set TELEGRAM_BOT_TOKEN="<paste>"
fly secrets set VETO_API_KEY="<paste>"
fly secrets set VETO_AGENT_ID="<paste>"
fly secrets set ANTHROPIC_API_KEY="<paste>"
# Optional:
fly secrets set EXA_API_KEY="<paste>"
fly secrets set ASSEMBLYAI_API_KEY="<paste>"

fly deploy
fly logs   # confirm "Bot started" appears
```

Cost: free tier covers one small machine (shared-cpu-1x, 256MB) — fine for a community up to ~500 members. Larger groups: bump to `fly scale memory 512`.

---

## Railway  (push-and-go, $5/mo)

1. Fork this repo.
2. Connect Railway to the fork, select `agents/groups/` as the source directory.
3. Set the env vars listed above in Railway → Variables. Encrypted at rest.
4. Railway auto-deploys on push.

---

## Docker Compose  (any VPS — DigitalOcean, Hetzner, Linode)

```yaml
# agents/groups/compose.yaml
services:
  groups:
    image: veto-protocol/agents-groups:latest
    restart: unless-stopped
    env_file: .env       # NEVER COMMIT THIS FILE
    volumes:
      - groups-data:/home/veto/.local/share

volumes:
  groups-data:
```

Then:
```bash
# Create the env file with mode 0600
install -m 0600 /dev/stdin .env <<'EOF'
TELEGRAM_BOT_TOKEN=...
VETO_API_KEY=...
VETO_AGENT_ID=...
ANTHROPIC_API_KEY=...
EOF

# Make sure git won't catch it
echo ".env" >> .gitignore

docker compose up -d
docker compose logs -f
```

---

## Bare VPS + systemd  (Ubuntu / Debian / Hetzner / etc.)

```bash
# 1. Install Python + pipx + veto-agents
sudo apt update && sudo apt install -y python3.12 python3.12-venv pipx
pipx install veto-agents

# 2. Secrets, mode 0600, not on PATH, not in shell history
sudo install -d -m 0700 /etc/veto-agents
sudo tee /etc/veto-agents/secrets.env > /dev/null <<'EOF'
TELEGRAM_BOT_TOKEN=...
VETO_API_KEY=...
VETO_AGENT_ID=...
ANTHROPIC_API_KEY=...
EOF
sudo chmod 0600 /etc/veto-agents/secrets.env

# 3. systemd unit
sudo tee /etc/systemd/system/veto-groups.service > /dev/null <<'EOF'
[Unit]
Description=Veto Agents — Groups bot
After=network.target

[Service]
Type=simple
User=veto
EnvironmentFile=/etc/veto-agents/secrets.env
ExecStart=/usr/local/bin/veto-agents groups run --daemon
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# 4. Create the user + start
sudo useradd -r -s /usr/sbin/nologin veto || true
sudo systemctl daemon-reload
sudo systemctl enable --now veto-groups
sudo systemctl status veto-groups
```

---

## Verification

After deploy:

```bash
# Open Telegram, send /start to your bot.
# It should DM you with the configuration walkthrough.

# Tail logs on the deployment target:
fly logs                    # Fly
railway logs                # Railway
docker compose logs -f      # Compose
journalctl -u veto-groups -f  # systemd
```

The bot is healthy when you see:
- `Bot started successfully`
- `Connected to Veto authorize endpoint (https://veto-ai.com/api/v1)`
- A heartbeat every 60s

If it fails: 99% of the time it's a missing or wrong env var. Triple-check `TELEGRAM_BOT_TOKEN` first, then `VETO_API_KEY`, then your brain provider key.

---

## Cost estimate

For an active community (200 members, 30 questions/day, 5 voice notes/day):

| Item                          | Approx monthly cost |
|-------------------------------|---------------------|
| Hosting (Fly free tier)       | $0                  |
| Anthropic Claude Sonnet calls | $5–15               |
| Exa search (if enabled)       | $2–5                |
| AssemblyAI transcription      | $1–3                |
| Veto governance free tier     | $0                  |
| **Total**                     | **~$8–25 / month**  |

Veto enforces your caps, so you can't accidentally blow past whatever monthly limit you set during `/setup`.
