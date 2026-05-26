# Security — How Veto Agents handles your secrets

Plain English. Read this before pasting any API key.

## Where your API keys live

**Local machine (the CLI):**

When you paste an API key during `veto-agents install <agent>`, it goes into your **OS keychain** — the same secure store your browser uses for saved passwords.

- **macOS:** the login keychain. Search for "veto-agents" in Keychain Access to see, audit, or delete keys. Touch ID-protected by default on Apple Silicon.
- **Linux:** GNOME Keyring or KWallet (whatever your desktop has).
- **Windows:** Credential Manager.

Same library (`keyring`) used by gh CLI, AWS CLI, Vercel CLI, and npm. **Not** plaintext on disk. Not in shell history. Not in your dotfiles.

You can verify with `veto-agents creds list` (shows masked values) or directly in your OS's keychain UI.

**Removing a key:**

```bash
veto-agents creds remove REPLICATE_API_TOKEN
```

Or delete from Keychain Access / Secret Service / Credential Manager directly.

## When you deploy an agent to a server

The agent's 24/7 runtime (Fly, Railway, VPS, Docker) needs the keys too. **Never bake them into the Dockerfile or paste them in a shell command** — they end up in build cache, image layers, shell history, container logs.

Per platform:

### Fly.io
```bash
fly secrets set REPLICATE_API_TOKEN="<paste>"
fly secrets set ANTHROPIC_API_KEY="<paste>"
# Encrypted at rest, injected as env vars only at runtime, never in image.
```

### Railway
Set them in the Railway dashboard → Variables tab. Encrypted at rest. Or via CLI:
```bash
railway variables set REPLICATE_API_TOKEN="<paste>"
```

### Docker Compose
Use a `.env` file in your project root (gitignored — verify with `cat .gitignore | grep .env`):
```bash
# .env (NEVER COMMIT)
REPLICATE_API_TOKEN=...
ANTHROPIC_API_KEY=...
```
docker-compose picks it up automatically.

### Bare VPS + systemd
```bash
sudo install -d -m 0700 /etc/veto-agents
sudo tee /etc/veto-agents/secrets.env <<EOF
REPLICATE_API_TOKEN=...
EOF
sudo chmod 0600 /etc/veto-agents/secrets.env
```
Reference it from your systemd unit:
```ini
[Service]
EnvironmentFile=/etc/veto-agents/secrets.env
```

### What NOT to do

- ❌ `docker run -e REPLICATE_API_TOKEN=r8_abc123 ...` — your token now lives in `~/.zsh_history`, `ps aux`, and `docker inspect`.
- ❌ Putting keys directly in the Dockerfile with `ENV` — they bake into image layers and leak with `docker history`.
- ❌ Committing `.env`, `credentials.yaml`, or anything similar to git.
- ❌ Pasting keys into Slack, Discord, GitHub issues — even temporarily.

## What Veto sees

Veto's backend (`veto-ai.com`) sees:
- Your email (for sign-in)
- The fact that your agent attempted a paid action, the merchant, the amount, the timestamp (for receipt generation)
- The verdict it returned (allow / deny / escalate) and why

Veto's backend does **not** see:
- Your API keys for any third-party service (Replicate, Anthropic, Telegram, etc.) — those stay on YOUR machine or YOUR deploy target
- Your wallet's private key — wallets are non-custodial; we never have a key
- The content of what your agent generates (images, replies, etc.) — only metadata + costs

## If you suspect a key was leaked

1. Rotate the key with the provider immediately (their dashboard → revoke + regenerate).
2. Update Veto Agents: `veto-agents creds set <ENV_VAR> <new-value>`.
3. If the leak was to git: rewrite history with `git filter-repo` or treat the repo as compromised.
4. Email tomer@veto-ai.com if a Veto-issued API key was leaked — we'll revoke it.

## Reporting a security issue

Email tomer@veto-ai.com. Please don't open public GitHub issues for vulnerabilities. We respond within one business day and credit reporters in the disclosure if requested.
