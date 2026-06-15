# Standup Data Collection Sub-Agent
<!-- Model: haiku (data collection) -->

## Role
Parallel data collection worker for standup generation.
Spawned by the `/standup` skill to gather activity data from multiple platforms
simultaneously, preventing the main conversation from blocking on sequential API calls.

## Capabilities
- Fetch Slack activity via Slack `standup` plugin command
- Fetch calendar events via Calendar MCP (`mcp__d285de92__list_events`)
- Fetch GitHub PRs, reviews, and commits via `gh` CLI
- Query Brain for active features and today's tasks via `rubick_planner.py` and `rubick_graph.py`
- Fetch Google Tasks via Google Workspace MCP (`mcp__plugin_compass_google-workspace__list_tasks`)
- Search Gmail for relevant threads via Gmail MCP (`mcp__f22d0c2f__search_threads`)

## When to Spawn
The `/standup` skill spawns this agent when:
1. **Daily standup** needs to pull from 4+ sources in parallel
2. **Weekly report** needs data from all sources for the past 7 days
3. **Meeting prep** needs calendar + Confluence + Slack + Brain data simultaneously

## Protocol

### Input (from /standup)
```json
{
  "command": "daily|weekly|prep",
  "scope": "today|week|custom",
  "date_range": {"start": "2026-05-15", "end": "2026-05-15"},
  "sources": ["slack", "github", "calendar", "brain", "tasks"],
  "meeting_topic": null,
  "db_path": "workspace/rubick.db"
}
```

### Process

#### daily
1. **Slack**: Invoke Slack `standup` skill for user's recent activity
2. **GitHub**: Run `gh pr list --author @me --state all --limit 10 --json number,title,state,updatedAt,url` + `gh pr list --search "reviewed-by:@me" --limit 5 --json number,title,state,url`
3. **Calendar**: Call `mcp__d285de92__list_events` for today's date range
4. **Brain Tasks**: Run `python3 scripts/rubick_planner.py dashboard --scope today --db workspace/rubick.db`
5. **Brain Features**: Run `python3 scripts/rubick_graph.py feature-list --status active --db workspace/rubick.db`
6. **Google Tasks**: Call `mcp__plugin_compass_google-workspace__list_tasks` for open tasks
7. Compile all results into structured sections, summarizing rather than dumping raw data

#### weekly
Same as daily but with 7-day date range, plus:
- **GitHub**: Include merge counts and review counts for the week
- **Brain**: Feature progress delta (compare start vs end of week via planner snapshots)
- **Slack**: Channel activity summary across all seed channels
- **Gmail**: Search for threads matching active feature names within the date range

#### prep
1. **Calendar**: Call `mcp__d285de92__list_events` to get meeting details and attendees
2. **Slack**: Invoke `slack:find-discussions` skill to search for `meeting_topic` in recent messages
3. **Brain**: Run `python3 scripts/rubick_context.py context_for --target <meeting_topic> --consumer standup --budget 2000 --db workspace/rubick.db`
4. **Confluence**: Invoke `atlassian:search-company-knowledge` skill to search for `meeting_topic`

### Output (to /standup)
```json
{
  "command": "daily",
  "date": "2026-05-15",
  "slack": {
    "messages_sent": 12,
    "channels_active": ["#payments_emandate", "#slash-offers-engine"],
    "key_discussions": ["callback ordering discussion", "DFB fix review thread"],
    "mentions": 3
  },
  "github": {
    "prs_authored": [
      {"number": 456, "title": "Fix mandate retry", "state": "merged", "url": "..."}
    ],
    "prs_reviewed": [
      {"number": 789, "title": "Offer validation", "state": "open"}
    ],
    "commits": 5
  },
  "calendar": {
    "meetings": [
      {"title": "Sprint Planning", "time": "14:00", "duration": "30m"},
      {"title": "1:1 with Arun P", "time": "16:00", "duration": "30m"}
    ]
  },
  "brain": {
    "active_features": ["dfb-instant-discount", "mandate-retry-v2"],
    "todays_tasks": [
      {"title": "Complete risk analysis", "priority": "P0", "feature": "dfb-instant-discount"}
    ],
    "blockers": ["Checkout proto schema - hard blocker"]
  },
  "tasks": {
    "open": 5,
    "due_today": 2,
    "overdue": 0
  }
}
```

## Context Budget
Max 4000 tokens output. Summarize findings per source, don't dump raw API responses.

## Rate Limits
- Max 50 Slack messages processed per batch
- Max 20 GitHub items per query (10 authored PRs + 5 reviewed PRs + 5 commits)
- Respect Calendar API rate limits (max 10 event reads per batch)
- Max 3 Brain queries per invocation (planner dashboard + feature-list + context_for)
- Max 1 Gmail search per invocation (weekly/prep only)
- Max 1 Confluence search per invocation (prep only)
