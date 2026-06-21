#!/usr/bin/env bash
# ============================================================================
# Nemesis v2 — setup & health-check script
#
#   ./setup.sh           Full idempotent setup (installs deps, runs brain init)
#   ./setup.sh --check    Read-only diagnostics — no installs, no writes
#
# --check is consumed by  /nemesis init  (Phase A)  and  /nemesis doctor .
# It NEVER installs, NEVER writes tokens, NEVER touches brain.db.
#
# This script validates OAuth MCPs (Slack / Google / Gmail / Calendar) but
# CANNOT connect them — those require Claude Code's interactive OAuth flow.
# Setup only tells you which ones are missing and how to connect them.
# ============================================================================

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    -h|--help)
      grep -E '^#( |$)' "$0" | sed -E 's/^# ?//' | head -20
      exit 0 ;;
    *) echo "Unknown argument: $arg (use --check or --help)"; exit 2 ;;
  esac
done

# ---- status helpers (text-only, no emoji) ---------------------------------
PASS=0; WARN=0; FAIL=0
ok()   { printf '  [ OK ]  %s\n' "$1"; PASS=$((PASS+1)); }
warn() { printf '  [WARN]  %s\n' "$1"; WARN=$((WARN+1)); }
fail() { printf '  [FAIL]  %s\n' "$1"; FAIL=$((FAIL+1)); }
info() { printf '          %s\n' "$1"; }
hdr()  { printf '\n=== %s ===\n' "$1"; }

if [ "$CHECK_ONLY" -eq 1 ]; then
  echo "Nemesis v2 — read-only diagnostics (--check)"
else
  echo "Nemesis v2 — setup"
fi
echo "Repo: $REPO_ROOT"

# ---- 1. Python -------------------------------------------------------------
hdr "1. Python"
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  fail "No python3 found on PATH. Install Python 3.9+ and re-run."
  echo ""; echo "Summary: $PASS ok / $WARN warn / $FAIL fail"
  exit 1
fi
PY_VER="$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || echo "?")"
PY_OK="$("$PY" -c 'import sys; print(1 if sys.version_info[:2] >= (3,9) else 0)' 2>/dev/null || echo 0)"
if [ "$PY_OK" = "1" ]; then
  ok "Python $PY_VER ($PY)"
else
  fail "Python $PY_VER is too old — need >= 3.9"
fi

# ---- 2. Python dependencies ------------------------------------------------
hdr "2. Python dependencies"
# networkx is the only hard requirement for the brain CLI.
if "$PY" -c 'import networkx' >/dev/null 2>&1; then
  ok "networkx importable (brain core)"
else
  if [ "$CHECK_ONLY" -eq 1 ]; then
    fail "networkx missing — run ./setup.sh (without --check) to install"
  else
    info "Installing requirements.txt ..."
    if "$PY" -m pip install -r requirements.txt; then
      if "$PY" -c 'import networkx' >/dev/null 2>&1; then
        ok "networkx installed"
      else
        fail "pip install completed but networkx still not importable"
      fi
    else
      fail "pip install -r requirements.txt failed"
    fi
  fi
fi
# Optional vector-search deps (lazy-loaded) — informational only.
if "$PY" -c 'import lancedb, sentence_transformers' >/dev/null 2>&1; then
  ok "vector search available (lancedb + sentence-transformers)"
else
  warn "vector search not installed (optional) — graph + FTS5 retrieval still work"
  info "enable with: pip install lancedb sentence-transformers"
fi

# ---- 3. GitHub CLI ---------------------------------------------------------
hdr "3. GitHub CLI (gh)"
if command -v gh >/dev/null 2>&1; then
  if gh auth status >/dev/null 2>&1; then
    ok "gh authenticated"
  else
    warn "gh installed but not authenticated"
    info "fix: gh auth login   (needed for PR creation + repo ingest)"
  fi
else
  warn "gh not installed — PR creation + GitHub ingest will be unavailable"
  info "install: https://cli.github.com/  then  gh auth login"
fi

# ---- 4. MCP servers (validate-only — never connects) ----------------------
hdr "4. MCP servers (validate-only)"
"$PY" - "$REPO_ROOT" <<'PYEOF'
import json, os, sys
repo = sys.argv[1]
sys.path.insert(0, repo)
try:
    from brain.config import REQUIRED_MCP
except Exception as e:
    print(f"  [WARN]  could not load REQUIRED_MCP from brain.config ({e})")
    sys.exit(0)

# Two ways an MCP can be present:
#  (a) local server declared under mcpServers in a settings.json, or
#  (b) a hosted OAuth connector in ~/.claude.json claudeAiMcpEverConnected.
configured = set()   # local mcpServers keys (lowercased)
connected = set()    # hosted connector names (lowercased)
saw_settings = False

for p in (os.path.expanduser("~/.claude/settings.json"),
          os.path.join(repo, ".claude", "settings.json")):
    try:
        with open(p) as fh:
            data = json.load(fh)
        saw_settings = True
        for k in (data.get("mcpServers") or {}):
            configured.add(k.lower())
    except FileNotFoundError:
        continue
    except Exception as e:
        print(f"  [WARN]  {p} present but unreadable ({e})")

try:
    with open(os.path.expanduser("~/.claude.json")) as fh:
        for name in (json.load(fh).get("claudeAiMcpEverConnected") or []):
            connected.add(str(name).lower())
except Exception:
    pass

if not saw_settings and not connected:
    print("  [WARN]  no MCP config found (no .claude/settings.json, no claude.ai connectors)")
    print("          connect MCPs inside Claude Code, then re-run --check")

missing = []
for mcp in REQUIRED_MCP:
    tokens = [mcp["server"], *mcp.get("aliases", [])]
    via_local = any(any(t.lower() in c or c in t.lower() for c in configured) for t in tokens)
    via_conn = any(any(t.lower() in c or c in t.lower() for c in connected) for t in tokens)
    if via_local or via_conn:
        how = "connector" if via_conn and not via_local else "local"
        print(f"  [ OK ]  {mcp['key']:<16} ({how}) {mcp['purpose']}")
    else:
        print(f"  [WARN]  {mcp['key']:<16} not connected — {mcp['purpose']}")
        missing.append(mcp)

if missing:
    print("")
    print("  OAuth MCPs cannot be auto-connected by this script.")
    print("  Connect each missing one inside Claude Code (it manages the tokens):")
    for mcp in missing:
        print(f"    - {mcp['key']} ({mcp['auth']})")
PYEOF

# ---- 5. .env ---------------------------------------------------------------
hdr "5. Environment file"
if [ -f .env ]; then
  ok ".env present"
elif [ -f .env.example ]; then
  if [ "$CHECK_ONLY" -eq 1 ]; then
    warn ".env not found (optional — all vars have defaults)"
    info "create with: cp .env.example .env"
  else
    cp .env.example .env
    ok "created .env from .env.example (all values optional)"
  fi
else
  warn ".env.example missing — skipping"
fi

# ---- 6. brain init ---------------------------------------------------------
hdr "6. Brain (Living Index)"
if [ "$CHECK_ONLY" -eq 1 ]; then
  # read-only: just report whether brain.db exists + is reachable
  if "$PY" -m brain stats >/dev/null 2>&1; then
    ok "brain.db reachable"
  else
    warn "brain.db not initialized — run ./setup.sh (without --check) or: python -m brain init"
  fi
else
  if [ "$FAIL" -eq 0 ]; then
    info "Running: python -m brain init ..."
    if "$PY" -m brain init; then
      ok "brain initialized"
      info "next: python -m brain register-sources && python -m brain init-experts --level 1"
      info "  (or just run  /nemesis init  inside Claude Code for the full bootstrap)"
    else
      fail "brain init failed — see output above"
    fi
  else
    warn "skipping brain init — resolve [FAIL] items above first"
  fi
fi

# ---- summary ---------------------------------------------------------------
hdr "Summary"
echo "  $PASS ok / $WARN warn / $FAIL fail"
if [ "$FAIL" -gt 0 ]; then
  echo "  Status: RED — fix [FAIL] items above."
  exit 1
elif [ "$WARN" -gt 0 ]; then
  echo "  Status: AMBER — usable; [WARN] items are optional or need OAuth."
  exit 0
else
  echo "  Status: GREEN — all checks passed."
  exit 0
fi
