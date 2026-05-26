#!/usr/bin/env bash
# veto-agents installer
#
# Usage:
#   curl -fsSL https://veto-ai.com/install-agents.sh | bash
#
# What this does (in order):
#   1. Sanity-checks the OS (macOS / Linux / WSL only — Windows users
#      should use PowerShell or WSL)
#   2. Makes sure python3 (>=3.10) is on PATH
#   3. Makes sure pipx is on PATH (installs it via the user's package
#      manager if not — brew on macOS, pip --user fallback otherwise)
#   4. Runs `pipx install --force veto-agents` to give them an isolated
#      `veto-agents` binary on their PATH (no global Python pollution)
#   5. Prints the first three commands to try
#
# pipx is the right primitive here: it gives the user a single binary they
# can update with `pipx upgrade veto-agents` and uninstall cleanly with
# `pipx uninstall veto-agents` — no virtualenv they have to manage by hand,
# no global pip mess.

set -euo pipefail

# ── ANSI ──────────────────────────────────────────────────────────
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

say()    { printf '  %s\n' "$*"; }
ok()     { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
warn()   { printf '  %s·%s %s\n' "$YELLOW" "$RESET" "$*"; }
fail()   { printf '  %s✗%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }
step()   { printf '\n%s%s%s\n' "$BOLD" "$*" "$RESET"; }

banner() {
  printf '\n%sveto-agents%s — AI agents that pay for things, with the safety built in.\n' "$BOLD$CYAN" "$RESET"
  printf '%shttps://github.com/veto-protocol/veto-agents%s\n' "$DIM" "$RESET"
}

banner

# ── OS check ──────────────────────────────────────────────────────
OS="$(uname -s)"
case "$OS" in
  Darwin)  PLATFORM="macos" ;;
  Linux)   PLATFORM="linux" ;;
  *)       fail "Unsupported OS: $OS. Use macOS / Linux / WSL. Windows users: see the README for PowerShell instructions." ;;
esac

step "1. Checking Python"

PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      PY="$cand"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  say "Python 3.10+ is required and wasn't found on PATH."
  if [ "$PLATFORM" = "macos" ]; then
    say "Install via: ${CYAN}brew install python@3.12${RESET}"
  else
    say "Install via your package manager — e.g. ${CYAN}sudo apt install python3.12 python3.12-venv${RESET}"
  fi
  fail "Re-run this script after Python is installed."
fi
ok "Using $($PY --version) at $(command -v $PY)"

# ── pipx ──────────────────────────────────────────────────────────
step "2. Checking pipx"

if ! command -v pipx >/dev/null 2>&1; then
  say "pipx not found — installing it so veto-agents lives in its own isolated env."
  if [ "$PLATFORM" = "macos" ] && command -v brew >/dev/null 2>&1; then
    brew install pipx >/dev/null 2>&1 || fail "brew install pipx failed."
    pipx ensurepath >/dev/null 2>&1 || true
  else
    "$PY" -m pip install --user --quiet pipx || fail "pip install --user pipx failed."
    "$PY" -m pipx ensurepath >/dev/null 2>&1 || true
  fi
  # pipx's ensurepath updates the shell rc but doesn't affect the current
  # shell; surface the right binary path for THIS process.
  if [ -d "$HOME/.local/bin" ]; then
    export PATH="$HOME/.local/bin:$PATH"
  fi
  if ! command -v pipx >/dev/null 2>&1; then
    fail "pipx still not on PATH. Open a new shell and re-run this script."
  fi
fi
ok "pipx ready: $(pipx --version 2>/dev/null || echo unknown)"

# ── Install ───────────────────────────────────────────────────────
step "3. Installing veto-agents"

# --force so re-runs upgrade in place. --include-deps so the agent runners
# can pull whatever they need at their own pace.
if ! pipx install --force veto-agents 2>&1; then
  fail "pipx install veto-agents failed. See output above."
fi

# pipx prints its own success line; we just confirm.
if ! command -v veto-agents >/dev/null 2>&1; then
  # Most likely cause: pipx's bin dir not yet on PATH in this process.
  if [ -d "$HOME/.local/bin" ]; then
    export PATH="$HOME/.local/bin:$PATH"
  fi
  if ! command -v veto-agents >/dev/null 2>&1; then
    warn "veto-agents installed but not on PATH yet."
    say  "Run: ${CYAN}pipx ensurepath${RESET} and open a new shell."
    exit 0
  fi
fi

ok "veto-agents $($(command -v veto-agents) --version 2>/dev/null | awk '{print $2}')"

# ── Make sure veto-agents is on the user's interactive-shell PATH ──
# `pipx ensurepath` writes to the user's shell rc (~/.zshrc on macOS by
# default, ~/.bashrc on Linux). We always run it — idempotent if already
# done — so the next shell session has the binary on PATH.
step "4. Wiring PATH"

pipx ensurepath >/dev/null 2>&1 || true

# Detect the user's likely interactive shell rc so we can give a precise
# "source this file" hint. zsh on macOS, bash elsewhere is the realistic
# default; honor $SHELL if it's set.
SHELL_NAME="$(basename "${SHELL:-bash}")"
case "$SHELL_NAME" in
  zsh)  RC_FILE="$HOME/.zshrc" ;;
  bash) RC_FILE="$HOME/.bashrc" ;;
  *)    RC_FILE="" ;;
esac

# Is ~/.local/bin already on the PATH that the user's interactive shell
# will see? We test by spawning a fresh login shell and grepping its PATH.
ON_PATH=false
if "$SHELL" -l -c 'echo "$PATH"' 2>/dev/null | tr ':' '\n' | grep -qx "$HOME/.local/bin"; then
  ON_PATH=true
fi

if $ON_PATH; then
  ok "~/.local/bin already on your shell PATH"
  NEXT_HINT="${CYAN}veto-agents${RESET}"
else
  ok "Added ~/.local/bin to your shell PATH (via pipx ensurepath)"
  if [ -n "$RC_FILE" ] && [ -f "$RC_FILE" ]; then
    warn "Open a new terminal tab, OR run: ${CYAN}source $RC_FILE${RESET}"
    NEXT_HINT="${CYAN}source $RC_FILE && veto-agents${RESET}"
  else
    warn "Open a new terminal tab so the PATH update takes effect."
    NEXT_HINT="${CYAN}veto-agents${RESET}  ${DIM}(open a new tab first)${RESET}"
  fi
fi

# ── First-run nudge ───────────────────────────────────────────────
cat <<EOF

${BOLD}Next:${RESET}
  ${NEXT_HINT}                          # walks you through first-time setup
  ${CYAN}veto-agents install media${RESET}            # add your first agent
  ${CYAN}veto-agents media "<your prompt>"${RESET}    # run it

${DIM}docs: https://github.com/veto-protocol/veto-agents${RESET}
${DIM}status: pipx-installed, upgrade with: pipx upgrade veto-agents${RESET}

EOF
