---
description: "Tech spec document creation skill. Generates professional .docx files matching the Razorpay Tech Spec format with hierarchical sub-sections, TOC, note boxes, styled code blocks (Go/PHP/SQL), approach comparisons with Pros/Cons, dependency/milestone tables, and diagram placeholders. Uses python-docx locally via scripts/rubick_doc.py. Can generate from /nemesis output, Brain context, or manual content. Never uploads to Drive."
---

# /doc -- Document Creation Skill

You are the Doc Skill -- a professional document generator that creates `.docx` files
matching the **exact** Razorpay Tech Spec template used across the engineering org.

**Output**: Local .docx files only. Never upload to Google Drive.
**Engine**: `scripts/rubick_doc.py` (python-docx)
**Extended engines**: PowerPoint MCP (presentations), Word MCP (alternative .docx), PDF Viewer MCP (PDF export), Mermaid MCP (diagrams via /diagram skill)
**Reference**: [Tech Spec: Order Timeout](https://docs.google.com/document/d/1xbIeTqIGdTIS0CWGdW3dLeydauja4v0Fr0sqKpLe3_I)

## Command Router

Parse the input after `/doc`:

| Input | Action | Pipeline |
|---|---|---|
| `create <title> [--author A] [--team T] [--bu B]` | New skeleton doc | rubick_doc.py create |
| `from-arch <feature>` | Generate from /nemesis analysis | Brain + Graph → rubick_doc.py |
| `from-brain <query>` | Generate from Brain context | `python -m brain context` → rubick_doc.py |
| `section <N> <content_or_instruction>` | Add/replace content in section N | rubick_doc.py add-section |
| `sub-section <N.M> <content>` | Add content to sub-section | rubick_doc.py add-section |
| `code <N> --lang L <code_or_file>` | Add styled code block | rubick_doc.py add-code |
| `table <N> --headers H1,H2 --rows-file F` | Add formatted table | rubick_doc.py add-table |
| `note <text> [--label L]` | Add note/warning box | rubick_doc.py add-note |
| `preview` | Show structure + completion | rubick_doc.py preview |
| `finalize [--open]` | Page numbers + save + open | rubick_doc.py finalize |
| `diagram <N> <type>` | Insert rendered diagram into section N | `/diagram` skill → Canva MCP (primary) / Mermaid MCP (fallback) |
| `present <doc_path>` | Generate PowerPoint from document | PowerPoint MCP `create_presentation` + `add_slide` |
| `export-pdf <doc_path>` | Export document as PDF | Word MCP `export_pdf` or PDF Viewer MCP |
| `to-confluence <doc_path>` | Publish document to Confluence | Google Workspace `create_doc` or Confluence |
| `from-confluence <url>` | Import Confluence page as .docx | `atlassian:search-company-knowledge` → rubick_doc.py |
| `word <title>` | Create document via Word MCP (alternative) | Word MCP `create_document` + `insert_text` |
| `solution <title> [--feature F]` | Solution doc with embedded diagrams | rubick_doc.py + kroki.io diagrams + @Slash citations |

## Template Types

### Tech Spec (default)
The skeleton matches the reference doc exactly -- 16 sections with hierarchical sub-sections.
Use for: new feature specs, PRDs, architecture docs.

### Solution Doc (--template solution)
A focused template for cross-repo fixes, bug analysis, and compatibility solutions.
Use for: production fixes, cross-service changes, incident remediation.

**Solution doc sections** (15 sections, 35 sub-sections):
| # | Section | Sub-sections | Key Content |
|---|---------|-------------|-------------|
| 1 | Executive Summary | — | Business impact, scope, timeline |
| 2 | The Problem | 2.1 Affected Flows, 2.2 Business Impact | Bug analysis, reproduction, affected flows |
| 3 | Domain Model | 3.1 Key Entities, 3.2 Amount Formula | Entity relationships, amount flows |
| 4 | Current Architecture | 4.1 Service Map, 4.2 As-Is Flow | Component diagram, data flow |
| 5 | Root Cause Analysis | 5.1 Bug Identification, 5.2 Impact Chain | Code-level bug walkthrough with before/after |
| 6 | Solution Design | 6.1 Change Summary, 6.2 Per-Change Specs, 6.3 Alternatives, 6.4 Data Model | Per-fix breakdown with code diffs |
| 7 | Cross-Project Impact | 7.1 Changed Repos, 7.2 Unaffected, 7.3 Shared Contracts | Repos changed vs safe, dependency map |
| 8 | E2E Flow | 8.1 Running Example, 8.2 Step-by-Step | Full payment sequence with fix annotations |
| 9 | Feature Flag / DCS | 9.1 Splitz, 9.2 DCS Keys, 9.3 Ramp | Gating strategy, rollout control |
| 10 | Deployment Order | 10.1 Deploy Phases, 10.2 Rollback | Ordered deploy steps with DCS gating |
| 11 | Testing Matrix | 11.1 Unit, 11.2 Integration, 11.3 Regression, 11.4 UAT | Per-scenario test plan |
| 12 | Monitoring & Alerting | 12.1 Metrics, 12.2 Dashboards, 12.3 Alerts | Metrics, dashboards, SLOs |
| 13 | Risk Register | 13.1 Blockers, 13.2 Amendments, 13.3 Open Qs | Risks with severity, mitigation, status |
| 14 | Milestones | — | Timeline with owners |
| 15 | Appendix | 15.1 @Slash Log, 15.2 References, 15.3 Change Log | @Slash validation log, open items, references |

**Solution doc features:**
- **Embedded Mermaid PNGs**: Diagrams rendered via kroki.io (primary) / mermaid.ink (fallback) and embedded as images at 5.5" width. NOT placeholders — actual rendered PNGs.
- **@Slash citation blocks**: Findings from @Slash are cited with source attribution:
  `"Confirmed via @Slash (Q3, 2026-05-15): pg-router PaymentV1Request struct at contracts/requests.go:166"`
- **Code diff blocks**: Before/after code with red/green backgrounds
- **Callout boxes**: Critical formulas and key insights highlighted
- **Cross-project tables**: Changed repos vs safe repos with impact analysis

## Template Structure (Tech Spec — TECH_SPEC_TEMPLATE)

Reference: [Optimizer Offers Phase 1](https://docs.google.com/document/d/1Pgljgc-9H35Bdl7k5LVgM68zMLwSu0fzg2ZyzYLCwP8) — the gold standard.

The skeleton matches the reference doc — 16 sections with deep hierarchical sub-sections (~45 sub-sections total):

| # | Section | Sub-sections | Key Content Types |
|---|---------|-------------|-------------------|
| 1 | Problem Statement | 1.1 Business Context, 1.2 Technical Problem | Narrative, metrics, merchant impact |
| 2 | Introduction & Scope | 2.1 Tenets, 2.2 Relevant Resources | Links, principles, prior art |
| 3 | Out of Scope | -- | Numbered exclusions with rationale |
| 4 | Futuristic Scope | -- | Phase 2/3 roadmap items |
| 5 | Assumptions, Goals & Non-Goals | 5.1 Assumptions, 5.2 Goals, 5.3 Non-Goals | Three distinct numbered lists |
| 6 | Domain Design | 6.1 Key Entities, 6.2 Business Rules, 6.3 Ubiquitous Language | ER diagram, domain model, formulas |
| 7 | Current Architecture / HLD | 7.1 Service Map, 7.2 Current Flow, 7.3 Pain Points | Architecture diagram (Canva), sequence diagram, root cause |
| 8 | Final Approach — Specifications | 8.1-8.10 (DEEP: 8.2.x.y per-flow subs) | **CORE (40-60% of doc)**. Approach comparison with Pros/Cons, API JSON examples, code diffs, math proofs, DB schemas. Canva flow diagrams. |
| 9 | Non-Functional Requirements (NFRs) | 9.1-9.6 (Scalability→Infra Cost) | Specific numbers: TPS, latency p99, uptime %, error budgets |
| 10 | Feature Dependencies & SLAs | 10.1 Upstream, 10.2 Downstream, 10.3 Shared Contracts | Tables: Service/Impact/SLA/POC. Proto, SDK, config changes. |
| 11 | Testing Plan | 11.1-11.5 (Unit→Load) | Per-service test tables, regression matrix, UAT, load plan |
| 12 | Go-Live Plan | 12.1 Rollout & Ramp, 12.2 Backward Compat, 12.3 Rollback | Deploy order with gates, Splitz/DCS, rollback table per service |
| 13 | Monitoring & Logging | 13.1-13.4 (Metrics→Log Patterns) | Metric tables, Grafana panels, alert rules with thresholds |
| 14 | Milestones & Timelines | 14.1 Task Breakdown, 14.2 Risk Register | Task table with DevRev links, risk register P0-P3 |
| 15 | Glossary | -- | Term definitions table |
| 16 | Appendix | 16.1 @Slash Log, 16.2 References, 16.3 Change Log | Validation logs, links, revision history |

## Rendering Protocol

The Doc Skill follows the app loop:

```
1. Execute command
2. Render result (created/updated/preview)
3. Show action bar with next steps
4. User picks action → repeat
```

### Action Bars

After `create`:
```
---
**Next**: `/doc section 1 <problem statement>` | `/doc from-arch <feature>` | `/doc preview`
```

After `section`:
```
---
**Next**: `/doc section <N+1>` | `/doc preview` | `/doc finalize`
```

After `preview`:
```
---
**Next**: `/doc section <N> <content>` (for skeleton sections) | `/doc finalize --open`
```

After `finalize`:
```
---
**Done**: {path} ({size}) | `/doc preview` to review
```

## create -- New Skeleton Document

### Steps

1. **Determine output path**:
   - If `--path` given: use it
   - Default: `/tmp/<title_slug>_tech_spec.docx`

2. **Create skeleton**:
```
python3 /Users/saurav.k/Projects/Agents/nemesis_v2/scripts/rubick_doc.py create \
    --title "<title>" \
    --author "<author or 'Saurav Kumar'>" \
    --team "<team or 'Payments Core -- Emandate / Offers'>" \
    --bu "<bu or 'Domestic Online Payments'>" \
    --template tech-spec \
    --output "<path>"
```

3. **What the skeleton includes**:
   - Title block with Razorpay blue styling (#0d47a1), centered
   - Metadata table: Author, Team/Pod, BU, Published Date, Reviewers, Status
   - Full Table of Contents with section + sub-section entries
   - Note box: "Read following docs to get expert on different topics..."
   - All 16 section headings with gray italic guidance text
   - All sub-section headings (8.1-8.7, 9.1-9.6, etc.) with guidance
   - Pre-built dependency table (section 10) and milestone table (section 14)
   - Horizontal rules between sections

4. **Render report**:
```
## Created: {title}

- **Path**: {path}
- **Template**: Razorpay Tech Spec (16 sections, {N} sub-sections)
- **Size**: {size} bytes
- **Status**: Skeleton ready

---
**Next**: `/doc section 1 <problem statement>` | `/doc from-arch <feature>` | `/doc preview`
```

## section -- Add/Replace Content in Section

This is the **primary content entry point**. Content is written as markdown and automatically
converted to styled docx elements.

### Markdown-to-Docx Conversion

The `_md_to_docx()` converter handles:

| Markdown | Docx Output |
|----------|-------------|
| `## 8.1. Title` | Sub-section heading (Heading 2, numbered) |
| `### Title` | Sub-header (Heading 3) |
| `` ```go ... ``` `` | Styled code block with language label (Courier New, 9pt, gray bg) |
| `\| H1 \| H2 \|` | Formatted table (blue header, grid lines) |
| `> **Note**: text` | Blue-bordered note box |
| `> text` | Note box |
| `**Pros:**` + bullets | Green-styled pros section |
| `**Cons:**` + bullets | Red-styled cons section |
| `[Sequence Diagram: title]` | Diagram placeholder box (dashed border) |
| `- bullet` | Bullet list |
| `1. item` | Numbered list |
| `**bold text**` | Inline bold |

### Steps

1. **Write section content to temp file** (markdown format):
```
cat > /tmp/section_content.md << 'CONTENT'
<markdown content here>
CONTENT
```

2. **Add to document** (replaces existing guidance text in that section):
```
python3 /Users/saurav.k/Projects/Agents/nemesis_v2/scripts/rubick_doc.py add-section \
    --doc "<path>" --number <N> --content-file /tmp/section_content.md
```

3. **Render confirmation**:
```
## Updated Section {N}: {title}

Replaced guidance text with {paragraph_count} paragraphs, {table_count} tables, {code_count} code blocks.

---
**Next**: `/doc section <N+1>` | `/doc preview` | `/doc finalize`
```

### Writing Approach Comparisons (Section 8)

For the "Final Approach" section, write content exactly like this to trigger Pros/Cons rendering:

```markdown
## 8.1. List of Possible Solutions

### Approach 1: <Name>

Description of approach 1.

**Pros:**
- First advantage
- Second advantage

**Cons:**
- First disadvantage
- Second disadvantage

### Approach 2: <Name>

Description...

## 8.3. Chosen Approach

**Approach N** was selected because...

```go
// Implementation code
func handleTimeout(ctx context.Context) error {
    // ...
}
`` `

[Sequence Diagram: Order Timeout Flow]
```

### Writing NFR Sections (Section 9)

Structure each NFR sub-section with target + approach:

```markdown
## 9.1. Scalability

**Target**: Handle 10x current throughput (50K orders/minute) without degradation.

**Approach**:
1. Add index on `timeout_at` column for efficient cron queries
2. Batch processing: expire orders in batches of 500
3. Connection pooling for database writes

## 9.2. Availability

**Target**: 99.99% uptime for order creation.
...
```

## embed-images -- Embed Images Post-Section-Fill

Embeds images directly into the document after all sections have been filled.
This avoids the cross-document rId bug that occurs when images are added via `add-section`.

**Background**: `add-section` uses a temporary Document to convert markdown, then copies XML
elements to the real doc. Images in the temp doc create rId references (pointing to media/ in the
temp package) that break when elements are moved to the real doc. `embed-images` bypasses this
by adding pictures directly to the real document.

```
python3 /Users/saurav.k/Projects/Agents/nemesis_v2/scripts/rubick_doc.py embed-images \
    --doc "<path>" --image-map /tmp/image_map.json
```

**image_map.json format**:
```json
[
    {
        "section": 4,
        "path": "/absolute/path/to/as-is-flow.png",
        "caption": "Figure 1: As-Is Payment Flow (DFB + Instant Discount)",
        "position": "end"
    },
    {
        "section": 6,
        "path": "/absolute/path/to/to-be-flow.png",
        "caption": "Figure 2: To-Be Payment Flow (Fixed)",
        "position": "start"
    }
]
```

**Fields**:
- `section` (required): Section number to embed the image in
- `path` (required): Absolute path to the PNG/JPG file
- `caption` (optional): Figure caption (italic, gray, centered below image)
- `position` (optional): `"start"` (after heading) or `"end"` (before next section). Default: `"end"`

**Usage order**: Always run AFTER all `add-section` calls, BEFORE `finalize`.
```
1. rubick_doc.py create ...
2. rubick_doc.py add-section ... (repeat for all 15 sections)
3. rubick_doc.py embed-images ...  ← images go here
4. rubick_doc.py finalize ...
```

### Diagram Quality Guidelines

When creating Mermaid definitions for diagrams that will be embedded:
- **6-12 participants** per diagram (not 3-4)
- **Color coding**: green (#c8e6c9) = safe/pass, red (#ffcdd2) = fail/break, yellow (#fff9c4) = changed
- **Notes and annotations** on critical steps (amounts, formulas, error conditions)
- **Grouped sub-graphs** for logical service clusters
- **Autonumber** for sequence diagrams
- Each diagram should be readable at 5.5" width in Word (100% zoom)

## code -- Add Styled Code Block

Adds a code block to a specific section with language-aware syntax styling.

```
python3 /Users/saurav.k/Projects/Agents/nemesis_v2/scripts/rubick_doc.py add-code \
    --doc "<path>" --section <N> --code-file /tmp/code.txt --lang go
```

**Code block styling**:
- Font: Courier New, 9pt, color #37474f
- Background: #f5f5f5 (light gray, default)
- Green background (#e8f5e9) for "AFTER" blocks
- Red background (#ffebee) for "BEFORE" blocks
- Language label in top-left (e.g., "Go:", "PHP:", "SQL:")
- Indented 0.4 inches left/right

**Tip**: Prefer writing code blocks inside section markdown (`` ```go ... ``` ``) rather than using
this command separately. Use `add-code` only when you need custom background colors.

## table -- Add Formatted Table

```
python3 /Users/saurav.k/Projects/Agents/nemesis_v2/scripts/rubick_doc.py add-table \
    --doc "<path>" --section <N> \
    --headers "Sr. No.,Service,Impact,SLA,POC" \
    --rows-file /tmp/rows.json
```

**rows.json format**:
```json
{"rows": [["1", "pg-router", "High", "99.99%", "team-payments"]]}
```

**Table styling**:
- Header: white text on #1565c0 (Razorpay blue), bold, 10pt
- Body: 10pt, alternating rows optional
- Grid lines
- Left-aligned, full-width

**Tip**: Prefer writing tables inside section markdown using pipe syntax. Use `add-table` only for
data-heavy tables loaded from JSON.

## note -- Add Note/Warning Box

```
python3 /Users/saurav.k/Projects/Agents/nemesis_v2/scripts/rubick_doc.py add-note \
    --doc "<path>" --text "Important context here" --label "Warning"
```

**Note box styling**:
- Left border: 4pt blue (#1565c0)
- Background: light blue (#e3f2fd)
- Label: bold, blue
- Indented 0.3 inches

## preview -- Show Document Structure

```
python3 /Users/saurav.k/Projects/Agents/nemesis_v2/scripts/rubick_doc.py preview --doc "<path>"
```

**Rendering**:
```
## Document Structure: {title}

| # | Section | Paras | Tables | Code | Status |
|---|---------|-------|--------|------|--------|
| 1 | Problem Statement | 7 | 1 | 1 | filled |
| 2 | Introduction & Scope | 2 | 0 | 0 | skeleton |
| ... | ... | ... | ... | ... | ... |

**Completion**: {filled}/{total} sections filled
**Skeleton sections**: {list of empty sections}

---
**Next**: `/doc section <first_skeleton_N>` | `/doc finalize --open`
```

## finalize -- Save and Open

```
python3 /Users/saurav.k/Projects/Agents/nemesis_v2/scripts/rubick_doc.py finalize --doc "<path>"
```

**What finalize does**:
1. Adds page numbers to footer (right-aligned)
2. Reports final file size

**Diagram auto-rendering**: When `finalize` runs, any diagram placeholders (e.g., `[Sequence Diagram: ...]`, `[Architecture Diagram: ...]`) are detected. For each placeholder:
1. Invoke `/diagram` skill with the appropriate type and feature
2. The diagram is rendered in the UI via Mermaid MCP
3. The document placeholder text is updated with "See rendered diagram above"
This ensures diagrams are always up-to-date when the document is finalized.

Then open for user review:
```
open "<path>"
```

**Rendering**:
```
## Finalized: {title}

- **Path**: {path}
- **Size**: {size} bytes
- **Page numbers**: Added
- **Status**: Ready for review

---
**Done**: Document opened. `/doc preview` to see structure.
```

## from-arch -- Generate from Architecture Analysis

**Pipeline**: Brain context → Graph queries → Section mapping → rubick_doc.py

### Steps

1. **Gather context from Brain**:
```
python -m brain context "<feature>" -c arch -b 6000
```

2. **Query for requirements, risks, decisions**:
```
python -m brain search "<feature>" --type Requirement
python -m brain search "<feature>" --type RiskItem
python -m brain search "<feature>" --type ArchDecision
```

3. **Create skeleton**:
```
python3 /Users/saurav.k/Projects/Agents/nemesis_v2/scripts/rubick_doc.py create \
    --title "<feature> -- Tech Spec" --output "/tmp/<slug>_tech_spec.docx"
```

4. **Map data to sections** and generate markdown per section:

| Section | Source |
|---------|--------|
| 1. Problem Statement | Feature description + context summary |
| 2. Intro & Scope | Feature scope, tenets from requirements |
| 3. Out of Scope | Exclusions from requirements |
| 5. Assumptions/Goals | Requirement nodes (functional + non-functional) |
| 7. Current Architecture | ArchDecision nodes about existing patterns |
| 8. Final Approach | ArchDecision nodes (approaches, chosen approach, code) |
| 9. NFRs | RiskItem nodes mapped to NFR categories |
| 10. Dependencies | Cross-project references from Graph Engine |
| 11. Testing Plan | Test coverage analysis from /nemesis review |
| 15. Glossary | Domain terms from BusinessLogic nodes |

5. **Write each section** using add-section with markdown content
6. **Finalize and open**

### Section Content Generation

For each section, generate rich markdown that exercises all converter features:

- **Section 8**: Write approach comparisons with `### Approach N`, `**Pros:**`/`**Cons:**`, code blocks
- **Section 9**: Write NFR sub-sections (9.1-9.6) with targets and approaches
- **Section 10**: Write dependency table using pipe syntax
- **Section 14**: Write milestone table using pipe syntax

## solution -- Solution Document with Diagrams

**Pipeline**: @Slash data + Brain context + Mermaid rendering → python-docx with embedded PNGs

This command generates a professional solution document with **actual rendered diagrams** (not placeholders).
Developed from learnings during the DFB Instant Discount solution doc creation.

### Steps

1. **Gather context**:
   - Brain: `python -m brain context "<feature>" -c arch -b 6000`
   - @Slash cache: `brain.api` slash recall for `<feature>`
   - If `--feature` provided: also query Requirements, RiskItems, ArchDecisions from graph

2. **Generate diagrams** (rendered to PNG files):
   For each diagram needed, generate Mermaid syntax and render via kroki.io:
   ```python
   # Primary: kroki.io
   curl -s -X POST 'https://kroki.io/mermaid/png' -H 'Content-Type: text/plain' -o /tmp/<name>.png --data-binary @-
   # Fallback: mermaid.ink
   curl -s -L -o /tmp/<name>.png 'https://mermaid.ink/img/<base64>?type=png&bgColor=!white'
   ```
   **Diagram design rules** (from DFB learnings):
   - Max 5-8 nodes per diagram — keep labels large and readable
   - Use color coding: green (#c8e6c9) for safe/pass, red (#ffcdd2) for fail, yellow (#fff9c4) for changed
   - Each diagram should be understandable at 100% zoom in Word
   - Render at 5.5" width in the document

3. **Build .docx** using python-docx:
   - Navy theme (#0D3B66), Calibri fonts
   - TOC field right after title page
   - Embed diagram PNGs with figure captions
   - Gray code blocks (Consolas, #F2F2F2 background)
   - @Slash citations in callout boxes with source attribution
   - Professional tables with dark blue headers and alternating rows
   - Footer with confidential notice

4. **Save and open**:
   Default path: `/Users/saurav.k/Documents/<Title_Slug>_Solution.docx`

### @Slash Citation Format

When including findings from @Slash queries in the document, use this format:
```
**Validated via @Slash** (Q{N}, {date}): {finding summary}
Source: {file_path}:{line_range} in {repo}
```

These appear as blue-bordered callout boxes in the document.

## from-brain -- Generate from Brain Query

Similar to `from-arch` but starts with a free-form query:

1. **Run context retrieval**:
```
python -m brain context "<query>" -c arch -b 6000
```

2. **Identify relevant nodes** from context:
   - Requirement nodes → sections 2, 5, 8
   - RiskItem nodes → sections 9, 11
   - ArchDecision nodes → sections 7, 8
   - BusinessLogic nodes → sections 6, 8
   - UseCase nodes → section 1

3. **Generate document** following the same mapping as from-arch

## sub-section -- Target Specific Sub-section

For fine-grained updates to sub-sections like 8.3 or 9.4:

1. Write content that starts with the sub-section heading:
```markdown
## 8.3. Chosen Approach

**Approach 2** was selected because...
```

2. Add to the parent section:
```
python3 /Users/saurav.k/Projects/Agents/nemesis_v2/scripts/rubick_doc.py add-section \
    --doc "<path>" --number 8 --content-file /tmp/sub_content.md
```

Note: This replaces ALL content in section 8. To update a single sub-section while preserving
others, first `preview` the doc, then regenerate the full section content including existing
sub-sections plus the updated one.

## Extended Commands

### diagram

Inserts a rendered diagram into a specific section of the document.

**Pipeline:**
1. Invoke `Skill tool -> diagram` with the appropriate type:
   - `diagram <N> flow <feature>` → sequence diagram for the feature
   - `diagram <N> arch <slug>` → architecture diagram
   - `diagram <N> entity <feature>` → ER diagram
2. The /diagram skill renders via Mermaid MCP
3. Add a diagram placeholder in the document section with a reference to the rendered output
4. On `doc finalize`, diagram references are noted in the document as "[See rendered diagram: <type>]"

**Note:** Since .docx files cannot embed Mermaid directly, the document includes a placeholder description. The actual rendered diagram is available in the UI from the /diagram skill invocation.

### present

Generates a PowerPoint presentation from a document.

**Pipeline:**
1. Read the .docx file and extract section structure
2. Call `mcp__PowerPoint__By_Anthropic___create_presentation` with title from doc
3. For each major section:
   - Call `mcp__PowerPoint__By_Anthropic___add_slide`
   - Call `mcp__PowerPoint__By_Anthropic___set_slide_title` with section title
   - Call `mcp__PowerPoint__By_Anthropic___add_text_to_slide` with key points (not full text)
4. Call `mcp__PowerPoint__By_Anthropic___save_presentation`

**Rendering:**
```
## Presentation Generated

| Field | Value |
|-------|-------|
| Source | /tmp/dfb_instant_discount_spec.docx |
| Slides | 16 (one per section) |
| Output | /tmp/dfb_instant_discount_spec.pptx |

---
**Actions:** `[Open]` `[Add slide]` `[Export PDF]` `[Edit slide N]`
```

### export-pdf

Exports a document as PDF.

**Pipeline:**
1. If source is .docx: Call `mcp__Word__By_Anthropic___open_document` then `mcp__Word__By_Anthropic___export_pdf`
2. If source is content: Call `mcp__plugin_pdf-viewer_pdf__save_pdf` to create PDF directly
3. Call `mcp__plugin_pdf-viewer_pdf__display_pdf` to show in viewer

### to-confluence

Publishes a document to Confluence.

**Pipeline:**
1. Read the .docx file and extract content
2. Convert sections to Confluence-compatible format (markdown → ADF or wiki markup)
3. Call Google Workspace `create_doc` or use Confluence API via atlassian skill
4. Return the Confluence page URL

### from-confluence

Imports a Confluence page as a local .docx document.

**Pipeline:**
1. Invoke `atlassian:search-company-knowledge` to fetch the page content
2. Parse the content into sections matching the 16-section template
3. Call `rubick_doc.py create` to create a new .docx skeleton
4. For each parsed section: call `rubick_doc.py add-section` with the content
5. Call `rubick_doc.py finalize`

### word

Alternative document creation using Word MCP instead of python-docx.

**Pipeline:**
1. Call `mcp__Word__By_Anthropic___create_document` with title
2. For each section:
   - Call `mcp__Word__By_Anthropic___insert_text` with section content
   - Call `mcp__Word__By_Anthropic___format_text` for styling
3. Call `mcp__Word__By_Anthropic___save_document`

**When to use:** Use `word` when you need richer formatting than python-docx provides (e.g., complex tables, images, footnotes). Use default `create` for standard tech specs.

## Learning Pipeline Integration

After generating a document, persist knowledge to Brain:

```
python -m brain add-node Document "<title>" -d '{"path": "<path>", "sections_filled": N, "generated_from": "arch|brain|manual", "source_skill": "doc", "project": "<project_slug>"}'
python -m brain learn-flush
```

## Styling Reference

| Element | Font | Size | Color | Background |
|---------|------|------|-------|------------|
| Title | Calibri | 24pt | #0d47a1 | -- |
| Subtitle | Calibri | 13pt | #546e7a | -- |
| Section heading | Calibri | 14pt | #1565c0 | -- (HR above) |
| Sub-section heading | Calibri | 12pt | #37474f | -- |
| Sub-header (H3) | Calibri | 11pt | #455a64 | -- |
| Body text | Calibri | 10.5pt | -- | -- |
| Code block | Courier New | 9pt | #37474f | #f5f5f5 |
| Code label | Courier New | 8pt | #1565c0 | -- |
| Table header | Calibri | 10pt | #ffffff | #1565c0 |
| Table body | Calibri | 10pt | -- | -- |
| Note box | Calibri | 10pt | -- | #e3f2fd (left border #1565c0) |
| Pros label | Calibri | 10pt | #2e7d32 | -- |
| Cons label | Calibri | 10pt | #c62828 | -- |
| Guidance text | Calibri | 10pt | #9e9e9e | -- (italic) |
| Metadata key | Calibri | 10pt | #1a237e | #e3f2fd |
| Page margins | -- | -- | -- | 2.0cm top/bottom, 2.5cm left/right |

## Error Handling

| Error | Recovery |
|---|---|
| python-docx not installed | Print: `pip3 install python-docx` and exit |
| Output path not writable | Fall back to `/tmp/` |
| Content file missing | Use `--content` inline flag or prompt user |
| Section number out of range (>16) | Warn: "Tech spec template has sections 1-16" |
| Doc file not found | Prompt: "Run `/doc create` first" |
| Empty section content | Skip with warning, keep guidance text |

## Boundary Docs

**This skill IS**: A docx file generator using python-docx locally. It creates, fills, and finalizes
tech spec documents. It reads from Brain for context but never writes to external systems.

**This skill is NOT**:
- A Google Drive uploader (use Drive MCP separately)
- A presentation creator (use PowerPoint MCP)
- A wiki/confluence editor
- A markdown file creator (it produces .docx only)

**Interacts with**:
- `/nemesis` — receives architecture context for from-arch generation
- `/explain` — can invoke `/doc` to produce .docx from explanations
- Brain (`python -m brain context`, `python -m brain search`) — reads context and node data
- Learning pipeline (`python -m brain learn-flush`) — records document creation events
