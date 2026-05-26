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
say()  { printf '  %s\n' "$*"; }

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
printf '  %sAI agents that pay for things, governed by Veto.%s   %s%sveto-ai.com%s\n\n' \
  "$DIM" "$RESET" "$BOLD" "$CYAN" "$RESET"

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
_INSTALLER=""
if command -v pipx >/dev/null 2>&1; then
  _INSTALLER="pipx"
else
  # First-time: install the isolated-env helper for them, silently.
  if [ "$PLATFORM" = "macos" ] && command -v brew >/dev/null 2>&1; then
    brew install pipx >/dev/null 2>&1 || true
  fi
  if ! command -v pipx >/dev/null 2>&1; then
    "$_PY" -m pip install --user --quiet pipx >/dev/null 2>&1 || \
      err "Couldn't bootstrap the install helper. Try: ${CYAN}$_PY -m pip install --user pipx${RESET} then re-run."
  fi
  command -v pipx >/dev/null 2>&1 || export PATH="$HOME/.local/bin:$PATH"
  _INSTALLER="pipx"
fi

# ── Install ───────────────────────────────────────────────────────
# Install with all optional extras so every agent works out of the box.
# (Adds python-telegram-bot for Groups, anthropic for Media/Groups replies, etc.)
printf '  %s…%s installing veto-agents' "$DIM" "$RESET"
if ! pipx install --force "veto-agents[all]" >/dev/null 2>&1; then
  printf '\r\033[K'
  err "Install failed. Try: ${CYAN}pipx install --force 'veto-agents[all]'${RESET}"
fi
# \r + \033[K clears the in-place progress line before printing the ✓ — otherwise
# the trailing chars of "installing veto-agents" peek through (it's longer than
# "installed").
printf '\r\033[K' && ok "veto-agents installed"

# Ensure ~/.local/bin (or pipx's chosen location) is on PATH for future shells.
pipx ensurepath >/dev/null 2>&1 || true
if ! command -v veto-agents >/dev/null 2>&1; then
  if [ -d "$HOME/.local/bin" ]; then
    export PATH="$HOME/.local/bin:$PATH"
  fi
fi

# ── PATH check + hint ─────────────────────────────────────────────
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

# ── Done ──────────────────────────────────────────────────────────
printf '\n'

if $ON_PATH; then
  printf '  %sReady.%s Try:\n\n' "$BOLD$GREEN" "$RESET"
  printf '    %sveto-agents%s\n\n' "$CYAN" "$RESET"
else
  printf '  %sAlmost done.%s One more thing — your shell needs to pick up the new binary.\n' "$BOLD" "$RESET"
  if [ -n "$RC_FILE" ]; then
    printf '  Either open a new terminal tab, or run:\n\n'
    printf '    %ssource %s && veto-agents%s\n\n' "$CYAN" "$RC_FILE" "$RESET"
  else
    printf '  Open a new terminal tab, then run:\n\n'
    printf '    %sveto-agents%s\n\n' "$CYAN" "$RESET"
  fi
fi
