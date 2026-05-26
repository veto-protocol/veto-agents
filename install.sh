#!/usr/bin/env bash
# Veto Agents — installer
#
#   curl -fsSL https://raw.githubusercontent.com/veto-protocol/veto-agents/main/install.sh | bash
#
# Quiet, opinionated. Installs Veto Agents into a private isolated env on
# your machine and puts the `veto-agents` binary on your PATH.

set -euo pipefail

# ── colors ────────────────────────────────────────────────────────
if [ -t 1 ]; then
  BOLD="$(printf '\033[1m')"
  CYAN="$(printf '\033[36m')"
  GREEN="$(printf '\033[32m')"
  YELLOW="$(printf '\033[33m')"
  RED="$(printf '\033[31m')"
  DIM="$(printf '\033[2m')"
  RESET="$(printf '\033[0m')"
else
  BOLD=""; CYAN=""; GREEN=""; YELLOW=""; RED=""; DIM=""; RESET=""
fi

ok()   { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '  %s·%s %s\n' "$YELLOW" "$RESET" "$*"; }
err()  { printf '  %s✗%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

# ── banner ────────────────────────────────────────────────────────
printf '\n'
printf '  %s       ▃▄▆▇▇██▇▇▆▄▃%s\n' "$BOLD$CYAN" "$RESET"
printf '  %s    ▂▆██████████████▆▂%s\n' "$BOLD$CYAN" "$RESET"
printf '  %s  ▗▟██████████████████▙▖%s\n' "$BOLD$CYAN" "$RESET"
printf '  %s ▗██████████████████████▖%s\n' "$BOLD$CYAN" "$RESET"
printf '  %s ████████████████████████▖                ▗▅▆▋%s\n' "$BOLD$CYAN" "$RESET"
printf '  %s▐██████████▉▔▔▔▝██████▛▔▔       ▂▂▁       ▐██▊         ▁▂▂▁%s\n' "$BOLD$CYAN" "$RESET"
printf '  %s▟███████████▍   ▐█████   ▗▎  ▄▇█████▆▃  ▐███████▋   ▁▅▇█████▆▃%s\n' "$BOLD$CYAN" "$RESET"
printf '  %s▐████████████    ████▌   █▎ ▟█▛▔   ▝██▙  ▔▐██▊▔▔   ▗██▛▔  ▔▝██▇▖%s\n' "$BOLD$CYAN" "$RESET"
printf '  %s▐████████████▙   ▝██▉   ▟█ ▐██▃▃▃▃▃▃▐██▎  ▐██▊    ▕██▉      ▝██▊%s\n' "$BOLD$CYAN" "$RESET"
printf '  %s ▜████████████▍   ▜█▘  ▗█▍ ▐██▀▀▀▀▀▀▀▀▀▘  ▐██▊    ▕██▊      ▕███%s\n' "$BOLD$CYAN" "$RESET"
printf '  %s ▝▜████████████   ▝▛   █▛  ▐██▍           ▐██▊     ███      ▗██▋%s\n' "$BOLD$CYAN" "$RESET"
printf '  %s   ▀███████████▙      ▟▀    ▀██▅▃▁▁▂▂▄    ▕███▃▂▃  ▝██▇▃▂▂▂▄██▛%s\n' "$BOLD$CYAN" "$RESET"
printf '  %s    ▔▀██████████▖     ▔      ▔▀▜█████▀▘    ▝▜████    ▀▀████▛▀▔%s\n' "$BOLD$CYAN" "$RESET"
printf '  %s       ▔▀▀▀█████▛%s\n' "$BOLD$CYAN" "$RESET"
printf '\n'

# ── title block ───────────────────────────────────────────────────
# We say "Veto Agents" explicitly so users know they're installing the
# agent CLI, not the main Veto governance CLI (different package).
printf '  %sVeto Agents%s   %sthe consumer CLI for 24/7 specialist AI agents%s\n' \
  "$BOLD$CYAN" "$RESET" "$DIM" "$RESET"
printf '  %spowered by veto-ai.com — install one agent, add more anytime%s\n\n' \
  "$DIM" "$RESET"

# ── OS check ──────────────────────────────────────────────────────
OS="$(uname -s)"
case "$OS" in
  Darwin)  PLATFORM="macos" ;;
  Linux)   PLATFORM="linux" ;;
  *)       err "Unsupported OS: $OS. Use macOS or Linux. On Windows, run inside WSL." ;;
esac

# ── Python check (silent unless missing) ──────────────────────────
_PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      _PY="$cand"
      break
    fi
  fi
done

if [ -z "$_PY" ]; then
  if [ "$PLATFORM" = "macos" ]; then
    err "Need Python 3.10+. Install with: ${CYAN}brew install python@3.12${RESET}, then re-run this."
  else
    err "Need Python 3.10+. Install with your package manager (e.g. ${CYAN}sudo apt install python3.12 python3.12-venv${RESET}), then re-run."
  fi
fi

# ── Helper (private, silent) — installs the isolated env runner ───
if ! command -v pipx >/dev/null 2>&1; then
  if [ "$PLATFORM" = "macos" ] && command -v brew >/dev/null 2>&1; then
    brew install pipx >/dev/null 2>&1 || true
  fi
  if ! command -v pipx >/dev/null 2>&1; then
    "$_PY" -m pip install --user --quiet pipx >/dev/null 2>&1 || \
      err "Couldn't bootstrap the install helper."
  fi
  command -v pipx >/dev/null 2>&1 || export PATH="$HOME/.local/bin:$PATH"
fi

# ── Install ───────────────────────────────────────────────────────
printf '  %s…%s installing veto-agents' "$DIM" "$RESET"
if ! pipx install --force "veto-agents[all]" >/dev/null 2>&1; then
  printf '\r\033[K'
  err "Install failed. Try: ${CYAN}pipx install --force 'veto-agents[all]'${RESET}"
fi
printf '\r\033[K' && ok "Installed."

# Ensure ~/.local/bin (or pipx's chosen location) is on PATH for future shells.
pipx ensurepath >/dev/null 2>&1 || true
if ! command -v veto-agents >/dev/null 2>&1; then
  if [ -d "$HOME/.local/bin" ]; then
    export PATH="$HOME/.local/bin:$PATH"
  fi
fi

# ── Explanation block ─────────────────────────────────────────────
cat <<EOF

  ${BOLD}What you just installed${RESET}

  ${DIM}Veto Agents lets you install AI agents that work for you 24/7.${RESET}
  ${DIM}Each one has a specific job — generate media, deploy code, run a${RESET}
  ${DIM}Telegram community, do research — and spends real money to do it${RESET}
  ${DIM}(LLM calls, APIs, infrastructure). Veto enforces the caps you set,${RESET}
  ${DIM}so no agent can blow your budget. Every action is cryptographically${RESET}
  ${DIM}signed, so you have a complete audit trail.${RESET}

  ${DIM}You pick one agent to start. You can add more anytime.${RESET}

EOF

# ── PATH check ────────────────────────────────────────────────────
SHELL_NAME="$(basename "${SHELL:-bash}")"
case "$SHELL_NAME" in
  zsh)  RC_FILE="$HOME/.zshrc" ;;
  bash) RC_FILE="$HOME/.bashrc" ;;
  *)    RC_FILE="" ;;
esac

ON_PATH=false
if "$SHELL" -l -c 'echo "$PATH"' 2>/dev/null | tr ':' '\n' | grep -qx "$HOME/.local/bin"; then
  ON_PATH=true
fi

# ── Y/n prompt — start now or later ───────────────────────────────
# When invoked via `curl | bash`, the script's stdin is the pipe, not the
# terminal. We redirect /dev/tty in for the prompt + the launched CLI so
# the interactive wizard actually works.
if [ -e /dev/tty ] && command -v veto-agents >/dev/null 2>&1; then
  printf '  %sSet up your first agent now?%s  %s[Y/n]%s ' "$BOLD" "$RESET" "$DIM" "$RESET"
  if read -r REPLY < /dev/tty 2>/dev/null; then
    REPLY="${REPLY:-Y}"
    case "$REPLY" in
      [Yy]*)
        printf '\n'
        exec veto-agents < /dev/tty
        ;;
      *)
        printf '\n  No rush. When you are ready:\n\n'
        printf '    %sveto-agents%s\n\n' "$CYAN" "$RESET"
        exit 0
        ;;
    esac
  fi
fi

# Fallback (no TTY, or PATH issue) — give the user a clear next step.
if $ON_PATH; then
  printf '  When you are ready, run:\n\n'
  printf '    %sveto-agents%s\n\n' "$CYAN" "$RESET"
else
  printf '  %sOne more thing.%s Your shell needs to pick up the new binary.\n' "$BOLD" "$RESET"
  if [ -n "$RC_FILE" ]; then
    printf '  Open a new terminal tab, or run:\n\n'
    printf '    %ssource %s && veto-agents%s\n\n' "$CYAN" "$RC_FILE" "$RESET"
  else
    printf '  Open a new terminal tab, then run:\n\n'
    printf '    %sveto-agents%s\n\n' "$CYAN" "$RESET"
  fi
fi
