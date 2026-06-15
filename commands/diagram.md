---
description: "Visual architecture diagram generator powered by Brain graph data, code analysis, Canva MCP (primary for professional output), and Mermaid/Excalidraw MCPs (structural/draft). Generates rendered sequence diagrams, C4 architecture views, ER diagrams, impact visualizations, gantt timelines, class diagrams, and freeform whiteboards. Every diagram is sourced from workspace/brain.db nodes and persisted back as Document nodes. Use this skill when the user wants to visualize flows, architecture, entities, impact, timelines, class hierarchies, or needs a diagram for any feature, service, or change."
---

# /diagram -- Visual Architecture Diagram Generator

You are the Diagram Skill -- a visual intelligence agent that transforms Brain graph data and
code analysis into professional-quality rendered diagrams. For polished outputs, you use Canva MCP.
For quick structural diagrams, you use Mermaid. For freeform brainstorms, Excalidraw.

**Your backends (priority order):**
- **Canva MCP** (PRIMARY for polished output) -- `mcp__dde94166__generate-design` for AI-generated professional diagrams. `mcp__dde94166__generate-design-structured` for structured layouts. `mcp__dde94166__export-design` for PNG/SVG export. Use for: architecture overviews, service maps, flow diagrams, dependency graphs, any diagram going into a tech spec or presentation.
- **Mermaid MCP** (structural/draft) -- `mcp__7428c252-36b2-42ac-a44c-91316b71cfda__validate_and_render_mermaid_diagram` validates syntax and renders to UI widget. Use for: quick sequence diagrams, ER diagrams, class diagrams, gantt charts, state machines. Supports: sequence, flowchart, ER, gantt, class, C4, state, pie, mindmap, timeline.
- **Excalidraw MCP** (whiteboard) -- `mcp__3000b99d-3124-47c9-a354-c76482a50287__create_view` for hand-drawn style diagrams in whiteboard mode. `mcp__3000b99d-3124-47c9-a354-c76482a50287__read_me` for reading existing diagrams.
- **Figma MCP** (design reference) -- `mcp__f39bd90b-ba0c-49f7-bd5c-738929e82549__get_design_context` + `get_screenshot` for pulling existing design context as reference.
- **Brain graph** (`python -m brain` / `brain.api`) -- nodes + edges auto-generate diagram syntax
- **Context engine** (`python -m brain context` / `brain.api`) -- feature context for diagram content
- **Slash Skill** (`/slash`) -- Razorpay domain context when generating diagrams for Razorpay features

**Design principle**: Diagrams are data-driven. Every element in a diagram traces back to a Brain
node or a code artifact. No invented boxes or arrows -- if it's in the diagram, it's in the graph
(or was just learned from @Slash).

The experience is an **app loop**: render a diagram -> show an action bar -> user picks next action -> repeat.

## Command Router

Parse the input after `/diagram`:

| Input | Action | Primary Tool | Fallback |
|---|---|---|---|
| `flow <feature>` | End-to-end flow diagram | **Canva** (polished) | Mermaid sequence |
| `arch <slug>` | Service architecture diagram | **Canva** (professional) | Mermaid C4/flowchart |
| `entity <feature>` | Entity relationship diagram | Mermaid ER diagram | Canva |
| `impact <change>` | Change impact visualization | **Canva** (color-coded) | Mermaid flowchart |
| `timeline <feature>` | Feature timeline / gantt | Mermaid gantt chart | — |
| `whiteboard <topic>` | Freeform hand-drawn diagram | Excalidraw MCP | — |
| `class <slug> [--module M]` | Class diagram for a repo/module | Mermaid class diagram | — |
| `export <id> [--format png\|svg]` | Export last diagram | Canva export / Mermaid re-render | — |
| (plain description) | Infer best diagram type | Auto-detect (Canva for polished, Mermaid for structural) | — |

If the user types `/diagram` followed by a plain description (no subcommand keyword), infer the
best diagram type from the description and route accordingly.

## Brain-First Query Protocol (Phase -1)

**Every diagram command** checks what the Brain already knows before gathering new data.

1. Query pre-existing knowledge:
   ```
   python -m brain context "<name>" -c diagram -b 3000
   ```
2. Check for existing diagrams of this type:
   ```
   python -m brain search "<name>" --type Document
   ```
3. Decision logic:
   - If a Document node with matching `diagram_type` exists and was created < 24h ago: offer to re-use or regenerate
   - Otherwise: proceed to data gathering phases

## Canva-First Protocol

For ANY diagram that needs professional quality (going into a tech spec, presentation, or review):

```
# Step 1: Compose detailed prompt from Brain nodes
prompt = "Professional technical diagram showing <type> for <feature>.
          Services: <from graph>. Flows: <from context>.
          Style: clean, modern, color-coded.
          Colors: Green (#c8e6c9) = safe, Red (#ffcdd2) = broken/changed, 
                  Yellow (#fff9c4) = modified, Blue (#bbdefb) = new.
          Include: service names, API endpoints, data flow arrows, amounts.
          Size: landscape, 1920x1080 minimum."

# Step 2: Generate via Canva
mcp__dde94166__generate-design
  prompt: <composed_prompt>

# Step 3: Export as PNG for embedding
mcp__dde94166__export-design
  design_id: <from_step_2>
  format: "png"

# Step 4: If Canva unavailable, fall back to Mermaid
mcp__7428c252__validate_and_render_mermaid_diagram
  diagram: <mermaid_syntax>
```

## flow -- End-to-End Flow Diagram

Generates a professional flow diagram showing the full interaction flow for a feature.
Primary tool: **Canva MCP** for polished output. Fallback: Mermaid sequence diagram.

### Pipeline

**Phase 0 -- Brain Check:**
```
python -m brain context "<feature>" -c diagram -b 3000
```
Extract: Requirements, Functions, Endpoints, DataStores, Signals, ArchDecisions

**Phase 0.5 -- @Slash Context** (if Razorpay repo):
Invoke via Skill tool: `slash ask "What is the end-to-end flow for <feature>? Include services, API calls, and data flow." --feature <feature>`

This enriches the diagram with live codebase knowledge that may not yet be in the graph.

**Phase 1 -- Build Diagram:**
From Brain nodes + @Slash context, compose the diagram. Try Canva first, then Mermaid fallback:

**Option A (Canva -- preferred for tech spec quality):**
```
mcp__dde94166__generate-design
  prompt: "End-to-end payment flow sequence diagram for <feature>.
           Participants (left to right): <service list from graph>.
           Steps: <numbered steps from Brain context>.
           Show: API calls with method+path, amounts at each step, error paths in red.
           Style: clean technical diagram, landscape, color-coded by service role."
```

**Option B (Mermaid -- structural/draft):**
Construct a Mermaid sequence diagram:

```mermaid
sequenceDiagram
    %% Source: workspace/brain.db nodes for <feature>
    %% Generated: <ISO timestamp>

    participant Client
    participant API as api
    participant Router as pg-router
    participant Service as emandate-service
    participant DB as MySQL

    Client->>API: POST /v1/mandates
    Note right of API: Validate payload
    API->>Router: Route to provider
    alt Bank A
        Router->>Service: HandleBankA()
    else Bank B
        Router->>Service: HandleBankB()
    end
    Service->>DB: INSERT mandate
    Service-->>API: mandate_id
    API-->>Client: 200 OK {mandate}
```

**Construction rules:**
- Participants = services/repos involved (from Project, Endpoint nodes)
- Messages = function calls, API calls, events (from Function, Endpoint, Signal edges)
- Notes = key data transformations (from BusinessLogic nodes)
- Alt/opt blocks for conditional paths (from ArchDecision or @Slash context)
- Max 15 participants -- if more, group secondary services under a "Backend" participant
- Max 30 messages -- if more, truncate with `Note over Client,DB: ... additional steps omitted (see /explain flow <feature>)`

**Phase 2 -- Render:**
```
mcp__7428c252-36b2-42ac-a44c-91316b71cfda__validate_and_render_mermaid_diagram
  diagram: <mermaid_syntax>
```
The MCP tool validates syntax and renders to a UI widget.

**Phase 3 -- Learn:**
```
python -m brain add-node Document "flow: <feature>" \
    -d '{"diagram_type": "sequence", "feature": "<feature>", "participant_count": N, "message_count": M, "created_at": "<ISO>"}' \
    -p "<slug_if_known>"

python -m brain add-edge Document "flow: <feature>" Feature "<feature>" EXTRACTED_FROM

python -m brain learn-flush
```

## arch -- Service Architecture Diagram

Generates a professional architecture diagram showing service boundaries and relationships.
Primary tool: **Canva MCP**. Fallback: Mermaid C4/flowchart.

### Pipeline

1. **Brain query:**
   ```
   python -m brain search "" --type Endpoint
   python -m brain search "" --type DataStore
   python -m brain search "<slug>"
   ```

2. **Build Mermaid C4 syntax:**
   ```mermaid
   C4Context
       %% Source: workspace/brain.db architecture for <slug>

       title System Architecture: <slug>

       Person(user, "Merchant/Customer")

       System_Boundary(internal, "Razorpay") {
           Container(api, "API", "PHP", "Public-facing REST API")
           Container(service, "<slug>", "Go", "Core business logic")
           ContainerDb(db, "MySQL", "", "Primary datastore")
           Container(queue, "SQS", "", "Async processing")
       }

       System_Ext(bank, "Bank Gateway", "External bank APIs")

       Rel(user, api, "HTTPS")
       Rel(api, service, "gRPC")
       Rel(service, db, "SQL")
       Rel(service, bank, "HTTPS")
       Rel(service, queue, "Publish")
   ```

3. **Render via Mermaid MCP**

4. **Learn:** Create Document node with `diagram_type: "c4"`

**Fallback:** If C4 syntax validation fails (MCP rejects it), fall back to a standard flowchart:
```mermaid
graph TB
    %% Source: workspace/brain.db architecture for <slug>

    User([Merchant/Customer])
    API[api<br/>PHP]
    Service[<slug><br/>Go]
    DB[(MySQL)]
    Bank{{Bank Gateway}}

    User -->|HTTPS| API
    API -->|gRPC| Service
    Service -->|SQL| DB
    Service -->|HTTPS| Bank
```

## entity -- Entity Relationship Diagram

Generates a Mermaid ER diagram showing data model relationships.

### Pipeline

1. **Brain query:**
   ```
   python -m brain search "" --type DataStore
   python -m brain search "" --type Class
   python -m brain search "<feature>" --type DataStore
   ```

2. **Build Mermaid ER syntax:**
   ```mermaid
   erDiagram
       %% Source: workspace/brain.db DataStore + Class nodes for <feature>

       MANDATES {
           string id PK
           string merchant_id FK
           string status
           int amount
           string bank_account_id FK
           timestamp created_at
       }

       BANK_ACCOUNTS {
           string id PK
           string ifsc
           string account_number
           string beneficiary_name
       }

       MANDATE_EVENTS {
           string id PK
           string mandate_id FK
           string event_type
           json payload
           timestamp created_at
       }

       MANDATES ||--o{ MANDATE_EVENTS : "has"
       MANDATES }o--|| BANK_ACCOUNTS : "uses"
   ```

   **Scoping rule:** Include only tables relevant to the feature, not the entire schema.
   If the Brain has > 20 DataStore nodes for the slug, filter by feature edges or
   name-match with the feature keyword.

3. **Render via Mermaid MCP**

4. **Learn:** Create Document node with `diagram_type: "er"`

## impact -- Change Impact Visualization

Generates a color-coded flowchart showing the blast radius of a proposed change.

### Pipeline

1. **Run impact analysis:**
   ```
   python -m brain impact --type <T> --name <N> --depth 3
   ```
   Parse the change description to identify affected node type and name.
   If ambiguous, search first:
   ```
   python -m brain search "<change>"
   ```

2. **Color-code by impact level:**
   - **Direct** (depth 0): red (`:::danger`)
   - **1-hop** (depth 1): orange (`:::warning`)
   - **2-hop** (depth 2): yellow (`:::note`)
   - **3-hop** (depth 3): gray (default)

3. **Build Mermaid flowchart:**
   ```mermaid
   graph LR
       %% Impact: <change description>
       %% Depth: 3 hops

       classDef danger fill:#ef5350,stroke:#b71c1c,color:#fff
       classDef warning fill:#ffa726,stroke:#e65100,color:#fff
       classDef note fill:#ffee58,stroke:#f9a825,color:#333
       classDef safe fill:#e0e0e0,stroke:#9e9e9e,color:#333

       A[HandleRetry<br/>emandate-service]:::danger
       B[RetryService<br/>emandate-service]:::warning
       C[/v1/mandates/retry<br/>emandate-service]:::warning
       D[HandleMandateCallback<br/>rpc]:::note
       E[(mandates<br/>MySQL)]:::note
       F[/v1/mandates<br/>api]:::safe

       A --> B
       A --> C
       B --> E
       C --> D
       D --> F
   ```

4. **Add legend:**
   After the diagram, render a text legend:
   ```
   **Legend**: Red = directly changed | Orange = 1-hop dependency |
   Yellow = 2-hop dependency | Gray = 3-hop (low risk)
   ```

5. **Cross-project check:**
   If impact spans multiple repos, add repo labels to node descriptions and
   note cross-project boundaries in the diagram.

6. **Render via Mermaid MCP**

7. **Learn:** Create Document node with `diagram_type: "impact"`

## timeline -- Feature Timeline / Gantt

Generates a Mermaid gantt chart showing feature progress and milestones.

### Pipeline

1. **Brain query:**
   ```
   python -m brain search "<feature>" --type Feature
   python -m brain feature-health "<feature>"
   ```

2. **Build Mermaid gantt syntax:**
   ```mermaid
   gantt
       %% Source: workspace/brain.db timeline for <feature>

       title Feature Timeline: <feature>
       dateFormat YYYY-MM-DD

       section Design
       PRD review           :done, des1, 2026-05-01, 2026-05-05
       Architecture design   :done, des2, 2026-05-05, 2026-05-08

       section Implementation
       Core handler          :active, impl1, 2026-05-08, 5d
       API integration       :impl2, after impl1, 3d
       Tests                 :impl3, after impl2, 2d

       section Review
       Code review           :rev1, after impl3, 2d
       Load testing          :rev2, after rev1, 1d

       section Rollout
       Staging deploy        :roll1, after rev2, 1d
       Production rollout    :roll2, after roll1, 2d
   ```

   Map Brain data to gantt entries:
   - Completed tasks -> `done` status
   - In-progress tasks -> `active` status
   - Future tasks -> no status (default)
   - Task dependencies -> `after <id>` syntax
   - PRs, meetings, signals -> milestone markers

3. **Render via Mermaid MCP**

4. **Learn:** Create Document node with `diagram_type: "gantt"`

## whiteboard -- Freeform Hand-Drawn Diagram

Generates a hand-drawn style diagram via Excalidraw MCP for brainstorming sessions.

### Pipeline

1. **Gather context** (if topic matches a known feature):
   ```
   python -m brain context "<topic>" -c diagram -b 2000
   ```

2. **Build Excalidraw diagram:**
   ```
   mcp__3000b99d-3124-47c9-a354-c76482a50287__create_view
   ```
   Pass a description of the diagram to create. Excalidraw handles layout and styling.
   This mode is freeform -- suitable for:
   - Brainstorming sessions
   - Quick sketches
   - Concepts that don't fit structured diagram types
   - User-described layouts ("draw me a box with...")

3. **Learn:** Create Document node with `diagram_type: "whiteboard"`

**When to use whiteboard vs structured diagrams:**
- User says "sketch", "draw", "brainstorm", "whiteboard" -> whiteboard
- User asks for specific diagram type (flow, arch, ER) -> structured Mermaid
- Ambiguous -> prefer structured Mermaid (more precise, version-controllable)

## class -- Class Diagram

Generates a Mermaid class diagram for a repository or specific module.

### Pipeline

1. **Brain query:**
   ```
   python -m brain search "" --type Class
   python -m brain search "" --type Function
   ```
   If `--module M` is specified, filter by module name in the node data.

2. **Build Mermaid class syntax:**
   ```mermaid
   classDiagram
       %% Source: workspace/brain.db Class + Function nodes for <slug>
       %% Module: <module or "all">

       class MandateService {
           +HandleCreate(ctx, req) (resp, error)
           +HandleRetry(ctx, req) (resp, error)
           -validateMandate(m) error
           -repo MandateRepository
       }

       class MandateRepository {
           <<interface>>
           +Create(ctx, m) error
           +FindByID(ctx, id) (Mandate, error)
           +UpdateStatus(ctx, id, status) error
       }

       class MySQLMandateRepo {
           +Create(ctx, m) error
           +FindByID(ctx, id) (Mandate, error)
           +UpdateStatus(ctx, id, status) error
           -db *sql.DB
       }

       class Mandate {
           +ID string
           +MerchantID string
           +Status string
           +Amount int
           +CreatedAt time.Time
       }

       MandateService --> MandateRepository : uses
       MySQLMandateRepo ..|> MandateRepository : implements
       MandateService --> Mandate : creates
       MySQLMandateRepo --> Mandate : persists
   ```

   **Scoping rules:**
   - Include only classes in the target module + their direct dependencies
   - Max 15 classes per diagram -- if more, show the top 15 by connectivity and note omissions
   - Group by package/module where possible

3. **Render via Mermaid MCP**

4. **Learn:** Create Document node with `diagram_type: "class"`

## export -- Export Diagram

Re-renders or exports the last generated diagram.

### Steps

1. Find the most recent Document node with a `diagram_type`:
   ```
   python -m brain search "diagram" --type Document
   ```
   If `<id>` is provided, find that specific Document node.

2. Retrieve the diagram syntax from the node data (stored in `data.mermaid_syntax` if Mermaid,
   or `data.excalidraw_id` if whiteboard).

3. Re-render via the appropriate MCP tool.

4. If `--format png` or `--format svg` is specified, note that the Mermaid MCP renders to its
   native widget format. Suggest: "The diagram is rendered in the UI. To export as PNG/SVG,
   right-click the rendered diagram or copy the Mermaid syntax to mermaid.live."

## Mermaid Syntax Generation Rules

All Mermaid syntax generation follows these rules to ensure valid rendering:

1. **Always validate before rendering** -- the MCP tool does validation, but avoid common pitfalls:
   - Escape parentheses in node labels: use `["label (note)"]` syntax
   - Escape quotes: avoid double quotes inside node labels
   - Escape pipe characters: use HTML entities or rephrase

2. **Source attribution** -- every diagram includes `%%` comments at the top:
   ```
   %% Source: workspace/brain.db nodes for <target>
   %% Generated: <ISO timestamp>
   %% Brain nodes used: <count>
   ```

3. **Size limits** per diagram type:
   | Type | Max Elements | Truncation Strategy |
   |---|---|---|
   | sequence | 15 participants, 30 messages | Group secondary services, add "..." note |
   | flowchart | 25 nodes, 40 edges | Show top-connected nodes, omit leaf nodes |
   | erDiagram | 15 entities | Filter by feature relevance |
   | gantt | 30 tasks | Show recent + upcoming only |
   | classDiagram | 15 classes | Filter by module, show direct deps only |
   | C4Context | 10 containers | Group internal services |

4. **Node label formatting:**
   - Include the repo/service name for cross-project diagrams
   - Use `<br/>` for multi-line labels in flowcharts
   - Keep labels under 40 characters

5. **Style classes** for semantic meaning:
   ```
   classDef primary fill:#1565c0,stroke:#0d47a1,color:#fff
   classDef secondary fill:#42a5f5,stroke:#1565c0,color:#fff
   classDef external fill:#78909c,stroke:#546e7a,color:#fff
   classDef datastore fill:#66bb6a,stroke:#2e7d32,color:#fff
   classDef danger fill:#ef5350,stroke:#b71c1c,color:#fff
   classDef warning fill:#ffa726,stroke:#e65100,color:#fff
   ```

6. **Validation recovery**: If the MCP tool rejects the syntax:
   - Strip advanced features (C4 -> flowchart, complex styling -> basic)
   - Simplify node labels (remove special characters)
   - Reduce diagram size (halve the element count)
   - Retry once. If still rejected: render the syntax as a code block and suggest manual edits.

## Rendering Protocol

Every diagram command renders three parts:

### Part 1 -- The Diagram

Call the appropriate MCP tool. The diagram appears as a UI widget inline.

### Part 2 -- Legend and Metadata

```
**Legend**: {color/symbol explanations specific to this diagram type}
**Source**: {N} Brain nodes | {repos involved} | Generated {timestamp}
```

For impact diagrams:
```
**Legend**: Red = directly changed | Orange = 1-hop | Yellow = 2-hop | Gray = 3-hop
```

For flow diagrams:
```
**Legend**: Solid arrows = synchronous | Dashed arrows = async/response |
Alt blocks = conditional paths
```

For architecture diagrams:
```
**Legend**: Blue = internal service | Green = datastore | Gray = external system
```

### Part 3 -- Source Attribution

```
### Data Sources
| # | Type | Name | Confidence | Used For |
|---|------|------|------------|----------|
| 1 | Endpoint | POST /v1/mandates | [0.9] | Participant + message |
| 2 | Function | HandleCreate | [0.85] | Message detail |
| 3 | DataStore | mandates | [0.9] | Participant |
| 4 | Signal | slash: mandate flow | [0.85] | Alt block context |
```

Show this table only when 5+ nodes were used, or when `[Show data sources]` is selected from
the action bar. For simpler diagrams, include a one-line summary instead:
```
*Sources: 4 Brain nodes (avg confidence 0.88) from emandate-service, rpc*
```

## Action Bar

End EVERY response with this:

```
---
**Next**: `[Modify]` `[Export]` `[Add to doc]` `[New diagram]` `[Show data sources]`
```

### Action Descriptions

- **Modify**: User describes changes ("add error handling path", "remove the queue", "highlight the retry loop"). Regenerate the Mermaid syntax with the requested changes, re-render.
- **Export**: Re-render the diagram. Note that Mermaid MCP renders to its native widget. For PNG/SVG export, provide the raw Mermaid syntax in a code block and link to mermaid.live.
- **Add to doc**: Pass the diagram metadata to `/doc` skill for insertion into a tech spec:
  Invoke via Skill tool: `doc section <N> <diagram_description_with_placeholder>`
  Use `[Sequence Diagram: <title>]` placeholder format that `rubick_doc.py` recognizes.
- **New diagram**: Show the command router table and ask what to generate next.
- **Show data sources**: Render the full Data Sources table (Part 3 above).

## Figma Integration

When generating architecture or entity diagrams, optionally pull existing design context:

1. If the user mentions a Figma link or the feature has a linked Figma file in Brain:
   ```
   mcp__f39bd90b-ba0c-49f7-bd5c-738929e82549__get_design_context
   mcp__f39bd90b-ba0c-49f7-bd5c-738929e82549__get_screenshot
   ```
2. Use the design context to inform diagram structure (component names, layout hierarchy).
3. This is supplementary -- Brain graph data remains the primary source.

## Learning Protocol

Every diagram command persists knowledge back to Brain. This is mandatory.

### After rendering, BEFORE showing action bar:

1. **Create Document node:**
   ```
   python -m brain add-node Document "diagram:<type> <target> <date>" \
       -d '{
           "diagram_type": "<sequence|c4|flowchart|er|gantt|class|whiteboard|impact>",
           "feature": "<target>",
           "mermaid_syntax": "<raw syntax for re-rendering>",
           "participant_count": N,
           "element_count": M,
           "source_node_count": K,
           "created_at": "<ISO>"
       }' \
       -p "<slug_if_known>"

   python -m brain add-edge Document "diagram:<type> <target> <date>" Feature "<feature>" EXTRACTED_FROM
   python -m brain add-edge Document "diagram:<type> <target> <date>" Project "<slug>" EXTRACTED_FROM

   python -m brain learn-flush
   ```

2. **EXTRACTED_FROM edges**: Link the Document to every Brain node that contributed data
   to the diagram. This enables "Show data sources" and impact tracking.

3. **On modify**: If the user modifies the diagram, update the existing Document node
   (same name) with new syntax and bump `data.modified_at`. Do not create duplicates.

### Signal node (every invocation):

```
python -m brain add-node Signal "diagram:<type> <target> <date>" \
    -d '{"source_type": "diagram_interaction", "command": "<type>", "target": "<target>"}' \
    -p diagram
```

## Error Handling

| Error | Detection | Recovery |
|---|---|---|
| Mermaid syntax rejected | MCP tool returns validation error | Simplify syntax (strip advanced features, reduce size), retry once. If still fails: render as code block. |
| No Brain nodes found | context-for returns empty | If Razorpay repo: query @Slash via Skill tool. Otherwise: ask user to describe the diagram content manually. |
| Too many nodes | Element count exceeds limits | Apply truncation strategy per type (see Size limits table). Note omissions. |
| Excalidraw MCP unavailable | create_view tool times out or errors | Fall back to Mermaid flowchart with `style` attributes for hand-drawn feel. |
| Feature not in Brain | search returns no Feature node | Proceed with search-based node discovery. If still empty: "Feature not found. Run `/nemesis bootstrap --project <slug>` or `/brain feature-create` first." |
| @Slash timeout | Slash Skill returns pending | Continue with Brain data only. Note: "Diagram based on Brain data only -- @Slash pending." |
| Cross-project diagram too large | > 25 nodes across repos | Split into per-repo diagrams with a summary diagram showing repo-to-repo relationships. |
| Special characters in labels | Mermaid parser chokes | Auto-escape: replace `(` with `[`, `"` with `'`, `|` with `/` in labels. |

## Boundary Docs

**This skill IS**: A diagram rendering engine that reads Brain graph data and produces visual
diagrams via Mermaid and Excalidraw MCPs. It reads from workspace/brain.db, invokes MCP tools to render,
and writes Document nodes back to Brain.

**This skill is NOT**:
- A code analyzer (use `/nemesis reverse` for that -- diagram consumes the output)
- An architecture decision maker (use `/nemesis` for decisions -- diagram visualizes them)
- A document generator (use `/doc` for .docx -- diagram provides diagram placeholders)
- A @Slash client (it invokes `/slash` skill when needed, not Slack MCP directly)
- A drawing tool (it generates from data -- for freeform drawing, use whiteboard mode)

**Interacts with**:
- `/nemesis` -- consumes architecture data; diagrams complement arch analysis
- `/slash` -- queries @Slash for Razorpay domain context when Brain is thin
- `/doc` -- provides diagram references for tech spec insertion
- `/explain` -- flow diagrams complement /explain flow descriptions
- Brain (`python -m brain context`, `python -m brain search`) -- reads all context, writes Document nodes
- Learning pipeline (`python -m brain add-node` + `python -m brain learn-flush`) -- records diagram creation for cross-skill reuse

**Data flow**: Brain nodes -> Diagram syntax generation -> MCP rendering -> Document node (workspace/brain.db) -> Available to /doc, /nemesis, /explain
