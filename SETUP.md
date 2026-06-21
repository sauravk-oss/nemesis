# Setup has moved

This guide was rewritten and split. It previously described an older "Rubick" pipeline
(`rubick.db`, `rubick_*.py`, shareable brain tarballs) that no longer matches the code —
notably, **`brain.db` is never shipped between machines**; each person rebuilds it locally.

Use the current docs instead:

- **[INSTALL.md](INSTALL.md)** — full step-by-step: prerequisites, `./setup.sh`, the MCP
  OAuth walkthrough, `/nemesis init`, `/nemesis doctor`, environment variables, feature
  sharing, and troubleshooting.
- **[README.md](README.md)** — overview + Quick Start + the brain CLI surface.
- **[SKILL.md](SKILL.md)** — the orchestrator/skill surface and protocols.

### TL;DR

```bash
git clone <url> && cd nemesis
./setup.sh                 # deps + gh + MCP validate + brain init   (./setup.sh --check for read-only)
```

Then, inside Claude Code: connect the OAuth MCPs (Slack, Google Drive, Gmail, Calendar) →
`/nemesis init` → `/nemesis doctor`.
