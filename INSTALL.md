# Installing Nemesis v2

This is the full, step-by-step setup — including the MCP OAuth walkthrough, environment
variables, and troubleshooting. For the 30-second version, see the Quick Start in
[README.md](README.md).

Setup is four moves: **clone → `./setup.sh` → connect MCPs → `/nemesis init`**, then
`/nemesis doctor` to confirm everything is green.

---

## What you're installing

Nemesis v2 is two things working together:

1. **The brain** — a pure-Python knowledge-graph engine (`brain/`), driven by
   `python3 -m brain <command>`. It stores everything in a single SQLite file,
   `workspace/brain.db`. The brain **never** calls MCPs directly.
2. **The skills** — Claude Code slash-commands (`commands/*.md`, e.g. `/nemesis`,
   `/implement`, `/franco`). Only the skill layer (the LLM) makes MCP calls, then hands
   the response back to the brain to ingest. This is the **Franco two-phase pattern**.

Two facts that shape the whole install:

- **`brain.db` is never shipped between machines.** Each person rebuilds it locally by
  re-running the learning pipeline against the source artifacts. There is nothing to copy.
- **OAuth MCPs cannot be connected by a script.** `setup.sh` *validates and guides* —
  it tells you which MCPs are missing and how to connect them, but the actual OAuth flow
  happens inside Claude Code, which manages the tokens. No token ever touches this repo.

---

## Prerequisites

| Requirement | Why | Check |
|-------------|-----|-------|
| [Claude Code](https://claude.ai/code) | Runs the skills, owns the MCP OAuth | open it |
| Python 3.9+ | Runs the brain | `python3 --version` |
| `gh` CLI | PR creation + GitHub ingest | `gh --version` |
| `gh` authenticated | Same | `gh auth status` |

`networkx` is the only **hard** Python dependency for the brain core. Vector search
(`lancedb` + `sentence-transformers`) is optional and lazy-loaded — graph + FTS5 retrieval
work fine without it. `setup.sh` installs everything in `requirements.txt` for you.

---

## Step 1 — Clone

```bash
git clone https://github.com/sauravk-oss/nemesis.git
cd nemesis
```

---

## Step 2 — Run `./setup.sh`

```bash
./setup.sh
```

`setup.sh` is **idempotent** — safe to re-run any time. It walks six sections and prints a
green/amber/red summary:

1. **Python** — verifies 3.9+.
2. **Python dependencies** — `pip install -r requirements.txt` (networkx is required;
   lancedb/sentence-transformers are optional, reported as a warning if absent).
3. **GitHub CLI** — checks `gh auth status`, guides you to `gh auth login` if needed.
4. **MCP servers (validate-only)** — reads your local Claude config and reports which
   required MCPs are connected vs. missing. **It never connects anything and never writes
   tokens.**
5. **Environment file** — creates `.env` from `.env.example` if missing (all values
   optional).
6. **Brain** — runs `python3 -m brain init` (dirs + schema + 45 services + 16 skills).

### Read-only diagnostics

```bash
./setup.sh --check
```

`--check` installs nothing, writes nothing, and never touches `brain.db`. It's the same
diagnostic that `/nemesis init` (Phase A) and `/nemesis doctor` run. Use it to see status
without side effects.

### Reading the summary

```
Summary
  N ok / N warn / N fail
  Status: GREEN | AMBER | RED
```

- **GREEN** — everything passed.
- **AMBER** — usable; the warnings are optional things (vector search) or OAuth MCPs you
  still need to connect (Step 3).
- **RED** — a hard failure (e.g. Python too old, networkx wouldn't install). Fix the
  `[FAIL]` lines and re-run.

---

## Step 3 — Connect MCPs (OAuth, one-time)

This is the step a script cannot do for you. Nemesis ingests from Slack, Google Drive,
Gmail, and Calendar via **hosted OAuth connectors that Claude Code manages**. You connect
them once, inside Claude Code, and Claude Code stores the tokens — never this repo.

### Required MCPs

| MCP | Purpose in Nemesis |
|-----|--------------------|
| **Slack** | Slack channel/thread ingest (Ideation sources, `/standup`, `/slash`) |
| **Google Drive / Workspace** | Drive docs read + **feature-sync push/pull** (the sharing flow) |
| **Gmail** | Gmail thread ingest |
| **Google Calendar** | Calendar context for `/standup` |

> Canva is optional — it powers polished diagrams in `/diagram` and `/designer`. Connect it
> the same way if you want professional-grade visuals; everything else degrades to Mermaid.

### How to connect

1. Open Claude Code in the repo (`claude`).
2. Open the **connectors / MCP** panel and connect each MCP above. Claude Code launches
   the provider's OAuth flow in your browser; approve it. Claude Code stores the token.
3. Back in the repo, confirm they registered:

   ```bash
   ./setup.sh --check
   ```

   The "MCP servers" section should now show `[ OK ]` for each connected MCP. Internally,
   detection looks for the connector in your Claude config (either a local `mcpServers`
   entry in a `settings.json`, or a hosted connector under `claudeAiMcpEverConnected` in
   `~/.claude.json`). Either form counts as connected.

### If an MCP stays "not connected"

- Re-open the connectors panel and confirm the OAuth actually completed (some flows need
  a second "allow").
- Restart Claude Code so it re-reads its connector list.
- Run `/nemesis doctor` — it shows the same probe with remediation hints.

You can proceed to Step 4 with some MCPs missing; `/nemesis init` **degrades gracefully**
and skips any source whose MCP is down (it warns and continues — it never fails the whole
init).

---

## Step 4 — Bootstrap the brain: `/nemesis init`

Inside Claude Code:

```text
/nemesis init
```

This is the full bootstrap, in four phases:

- **Phase A — Validate.** Runs `./setup.sh --check` (read-only) to confirm deps, `gh` auth,
  and MCP connectors.
- **Phase B — Seed.** `brain init` → `brain register-sources` → `brain init-experts --level 1`.
  - `brain init` creates the workspace, schema, 45 seed services (with their `DEPENDS_ON`
    graph), and the 16-skill registry. It does **not** create experts.
  - `brain register-sources` reads `config/sources.json` and upserts a `DataSource` node
    per source (Slack channels, Drive docs, repos, DevRev) plus `RELATES_TO` edges.
  - `brain init-experts --level 1` seeds a `ProjectExpert` node per project at L1.
    Idempotent — never downgrades an existing expert.
- **Phase C — Bounded live L1 ingest.** For each source flagged `l1: true` in
  `config/sources.json`, the LLM fetches a small, bounded slice and the brain ingests it
  (Franco two-phase): Slack ~30 messages / 7 days, Drive top-5 docs (×8000 chars each),
  GitHub README + top-10 files via `gh`, DevRev ~5 items. Each source is checkpointed via
  a sync cursor so re-runs are incremental. Any disconnected MCP is skipped with a warning.
- **Phase D — Report.** Prints what was registered/seeded/ingested and persists an
  `init:<timestamp>` Signal node.

`/nemesis init` is safe to re-run — Phase B is idempotent and Phase C resumes from cursors.

### Doing it manually (without the skill)

```bash
python3 -m brain init
python3 -m brain register-sources
python3 -m brain init-experts --level 1
python3 -m brain stats        # confirm services + sources + experts exist
```

The live L1 ingest (Phase C) requires the LLM/MCP layer, so it only runs via `/nemesis init`.

---

## Step 5 — Health check: `/nemesis doctor`

```text
/nemesis doctor
```

or directly:

```bash
python3 -m brain doctor
```

`doctor` prints a green/amber/red table covering: Python deps importable, `gh auth status`,
`brain.db` reachable + stats, required MCPs connected, the 16 skills resolvable, data
sources registered (count vs `config/sources.json`), and experts seeded with their levels.
Each non-green row comes with a one-line remediation hint.

A fresh, fully-connected install lands on **GREEN**. **AMBER** typically means optional
vector search isn't installed or you haven't run the live ingest yet — both are fine.

---

## Environment variables

Everything in `.env` is **optional** — the code has defaults for all of it. `.env` is
gitignored; `.env.example` is tracked. Copy and edit only if you need to override:

```bash
cp .env.example .env
```

| Variable | Default | When to set |
|----------|---------|-------------|
| `BRAIN_WORKSPACE` | `<repo>/workspace` | Relocate `brain.db`/repos off-repo, or point tests at a scratch dir |
| `NEMESIS_V2_ROOT` | `~/Projects/Agents/nemesis_v2` | Only if you cloned elsewhere **and** a script complains |
| `NEMESIS_LOG_DIR` | `~/.nemesis_v2/logs` | Redirect script logs |
| `NEMESIS_TMP_DIR` | `/tmp` | Scratch dir for coverage profiles / go-test json |
| `REDASH_API_KEY` | _(unset)_ | Enables `/db-validator` payment-SQL validation (from <https://redash.razorpay.com/profile>) |

> Slack, Google Drive/Workspace, Gmail, Calendar, and Canva are **not** environment
> variables. They are OAuth MCPs connected via Claude Code (Step 3). Never put their tokens
> in `.env`.

Example — run the brain against a throwaway workspace:

```bash
BRAIN_WORKSPACE=/tmp/nm-test python3 -m brain init && python3 -m brain stats
```

---

## Sharing features between machines

`brain.db` is never copied. Instead, feature artifacts (overview, solution, tech-spec,
`test-report.md`, `change-report.md`, `pipeline-report.html`, implementation files) are
pushed to a Google Drive folder, and each teammate rebuilds their own brain from those
artifacts.

- **Push** (after working a feature): `/nemesis sync <slug>` — uploads changed artifacts to
  `nemesis/features/<slug>/` on Drive (allowlist `.md`/`.html`/`.json`, skips files >2 MB
  and `*-logs/`). Idempotent: unchanged files aren't re-uploaded.
- **Pull** (on a fresh machine, after Steps 1–3): `/nemesis new <slug> <drive-link>` (or
  `/nemesis pull <drive-link>`) — downloads every artifact, recreates the feature directory
  locally, then rebuilds brain state via `feature-create` → Franco ingest → `learn-flush`.
  `feature-health <slug>` should then be populated.

Requires the Google Drive/Workspace MCP connected (Step 3).

### The pipeline report

`/nemesis report <slug>` renders `pipeline-report.html` — a single self-contained file
showing the whole AI pipeline as collapsible tree nodes (every doc, the skills used and
their input/output, each iteration, the embedded test report, the archive, a Brain-powered
knowledge node, and a redirect Drive URL). `/implement` generates it automatically at the
end of Implementation (Step 9b); run `/nemesis report <slug>` to regenerate it at any phase.
It's pushed to Drive alongside the other artifacts by `/nemesis sync`.

---

## Troubleshooting

**`./setup.sh: Permission denied`**
The clone didn't preserve the exec bit. Run `chmod +x setup.sh` (or `bash setup.sh`).

**`No python3 found on PATH`**
Install Python 3.9+ and re-run. On macOS: `brew install python`.

**`networkx` won't install / `pip install` fails**
Check you're on Python 3.9+ and that `pip3` points at the same interpreter
(`python3 -m pip --version`). Behind a proxy, configure pip first. Then re-run `./setup.sh`.

**`gh` not authenticated**
`gh auth login`. PR creation and GitHub ingest need this; the rest of the brain works
without it (you'll just see a warning).

**MCPs show "not connected" even though I connected them**
Restart Claude Code so it re-reads its connector list, then `./setup.sh --check`. Detection
matches each required MCP against both local `mcpServers` entries and the hosted
`claudeAiMcpEverConnected` list in `~/.claude.json` — if the OAuth completed, one of those
will contain it.

**`brain.db not initialized`**
Run `python3 -m brain init` (or just `./setup.sh`). To start completely fresh, delete
`workspace/brain.db` and re-init — nothing else stores graph state.

**`/nemesis init` Phase C skipped a source**
That source's MCP is disconnected (Slack/Drive/Gmail/Calendar). Connect it (Step 3) and
re-run `/nemesis init` — it resumes from the sync cursor, so it won't re-ingest what's
already there.

**Vector search warning in `doctor`/`setup.sh`**
Optional. Enable with `pip install lancedb sentence-transformers`. Graph + FTS5 retrieval
work without it.

**Stray SQLite files at the repo root (`./init`, `./stats`, …)**
A mis-typed brain command created them. `make clean` removes them.

**Reset everything**
`make clean` (caches + stray files), or delete `workspace/` for a full rebuild, then
`./setup.sh` + `/nemesis init`.

---

## Verifying the install

```bash
python3 -m brain doctor    # expect mostly OK; AMBER is fine pre-ingest
python3 -m brain stats     # services, sources, experts, node/edge counts
make test                  # py_compile brain + scripts, smoke-test the CLIs
```

In Claude Code, `/nemesis` (no args) should render the features dashboard with
`Skills: 16 loaded` in the footer. You're ready.
