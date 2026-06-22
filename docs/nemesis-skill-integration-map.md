# Nemesis Skill Integration Map

**Purpose:** Define exactly which Razorpay skills to invoke at each Nemesis phase  
**Date:** 2026-06-17  
**Status:** Implementation Guide

---

## Razorpay Skill Repositories

| Repo | Skills Available | Purpose |
|------|------------------|---------|
| **agent-skills** | Core engineering skills | System design, code review, testing, deploy |
| **merchant-skills** | Product/PM skills | Brainstorming, spec writing, user stories |
| **claude-plugins** | Infrastructure MCPs | Slack, Drive, Calendar, Kubernetes, etc. |
| **ai-skill-scanner** | Skill discovery | Find available skills dynamically |
| **agentic-skill-metrics** | Skill usage tracking | Monitor skill invocation patterns |

---

## Phase-by-Phase Skill Integration

### Phase -1: Brain-First (MANDATORY)

**No Razorpay skills** — uses internal Brain API only

```bash
python3 -m brain context "<feature>" -c arch -b 4000
python3 -m brain search "<feature>"
```

---

### Phase 1: Ideation (Overview Engine)

#### Step 2.5: Structured Brainstorming
**Skill:** `product-management:brainstorm`  
**When:** After raw source collection, before deep analysis  
**Input:** Feature brief + collected sources (Slack, docs, PRs)  
**Output:** Problem statement, user stories, success metrics, scope boundaries

```
Skill("product-management:brainstorm", 
     "Feature: <name>\n" +
     "Sources: <summary>\n" +
     "Context: <business_context>")
```

**Fallback:** Manual decomposition if skill fails to resolve

---

#### Step 5 Pause Point 4: Strategy Review
**Skill:** `compass:reviewing-strategy`  
**When:** After design decisions identified, before generating artifacts  
**Input:** Overview summary + design options  
**Output:** Alignment check with Razorpay standards, capacity, dependencies

```
Skill("compass:reviewing-strategy", 
     "Overview: <summary>\n" +
     "Design Options: <options>\n" +
     "Services: <list>")
```

**Fallback:** Proceed with Brain context validation

---

### Phase 2: Solutioning (Solution Design Engine)

#### Step 1.3: System Design Validation
**Skill:** `engineering:system-design`  
**When:** After loading overview, before code tracing  
**Input:** Overview summary + service architecture + proposed changes  
**Output:** Scalability, reliability, consistency, failure mode analysis

```
Skill("engineering:system-design", 
     "Overview: <summary>\n" +
     "Services: <architecture>\n" +
     "Changes: <proposed_changes>")
```

**Mandatory Pause Point:** Present concerns, ask user to address before code tracing

---

#### Step 2 (After Code Trace): Early Code Review
**Skill:** `engineering:code-review`  
**When:** After identifying code paths to modify, before solution design  
**Input:** Proposed code changes summary + file paths  
**Output:** Complexity hotspots, missing error handling, concurrency issues, regressions

```
Skill("engineering:code-review", 
     "Changes: <file_list>\n" +
     "Proposed: <code_summary>\n" +
     "Risk: <areas_of_concern>")
```

**Mandatory Pause Point:** Ask user if any code paths were missed

---

#### Step 5.5: Testing Strategy Formalization
**Skill:** `engineering:testing-strategy`  
**When:** After change design, before risk analysis  
**Input:** Solution summary + services + code changes  
**Output:** Structured test plan (unit, integration, SLIT, E2E)

```
Skill("engineering:testing-strategy", 
     "Solution: <summary>\n" +
     "Services: <list>\n" +
     "Changes: <code_changes>")
```

**Output stored:** Signal node `testing_strategy:<feature>` for Implementation phase

---

#### Step 5.6: Strategy Review
**Skill:** `compass:reviewing-strategy`  
**When:** After complete solution design, before risk analysis  
**Input:** Complete solution design summary  
**Output:** Alignment with Razorpay standards, team capacity, tech debt, coordination

```
Skill("compass:reviewing-strategy", 
     "Solution: <complete_design>\n" +
     "Timeline: <estimate>\n" +
     "Dependencies: <cross_team>")
```

**Output:** Warnings integrated into solution artifact

---

#### Step 5.7: Pre-Mortem Risk Analysis
**Skill:** `pre-mortem`  
**When:** After solution design + testing strategy, before generating solution.md  
**Input:** Solution summary + deployment plan  
**Output:** Structured risk discovery (what could go wrong)

```
Skill("pre-mortem", 
     "Solution: <summary>\n" +
     "Deployment: <plan>\n" +
     "Services: <affected>")
```

**Output:** RPN scores computed, risks categorized, mitigations defined

---

### Phase 3: Tech Spec (Document Generation Engine)

#### Step 0.5: Spec Structure Validation
**Skill:** `tech-spec-generator`  
**When:** Before generating content, after loading artifacts  
**Input:** Feature overview + solution summary  
**Output:** Section readiness report, flagged sections needing extra input

```
Skill("tech-spec-generator", 
     "Overview: <summary>\n" +
     "Solution: <summary>\n" +
     "Template: TECH_SPEC_TEMPLATE")
```

**Mandatory Pause Point:** Ask user if flagged sections need more data

---

#### Steps 1-4: Section-by-Section Generation
**Skills per section group:**

| Sections | Skill | Input | Output |
|----------|-------|-------|--------|
| 1-4 (Problem & Context) | `product-management:write-spec` | Overview | Problem statement, scope, requirements |
| 5, 14 (Assumptions, References) | `engineering:documentation` | Overview + solution | Assumptions list, reference links |
| 6 (Architecture) | `engineering:architecture` | Solution + service map | Architecture diagrams + description |
| 7 (System Design) | `engineering:system-design` | Solution + constraints | Design deep-dive (THE CORE) |
| 8 (Tech Debt) | `engineering:tech-debt` | Solution + codebase | Tech debt implications |
| 9 (APIs) | `compass:razorpay-api-review` | Solution + API contracts | API design review |
| 10 (Testing) | `engineering:testing-strategy` | Solution | Test strategy |
| 11-12 (Deployment) | `engineering:deploy-checklist` | Solution + rollout plan | Deploy checklist + rollback |

**Invocation pattern:**
```
for section_group in TECH_SPEC_TEMPLATE:
    skill = SECTION_SKILL_MAP[section_group]
    content = Skill(skill, context_for_section)
    insert_into_doc(section_group, content)
```

---

#### Step 4.5: @Slash Fact-Check (Enhanced)
**5-8 @Slash queries** targeting Razorpay NFR standards:
- Razorpay tech spec template requirements
- NFR standards (latency, availability, error rates)
- Monitoring requirements (dashboards, alerts, runbooks)
- Testing standards (coverage thresholds, SLIT requirements)
- Deploy procedures (canary, rollback, feature flags)
- Security requirements (PCI, data classification, access control)

```
slash ask "What are Razorpay's NFR standards for payment services (latency, availability)?"
slash ask "What monitoring requirements exist for new payment features?"
slash ask "What is the standard canary rollout process?"
```

---

### Phase 4: Implementation (Code Generation + PR Engine)

#### Delegation to `/implement` Skill
**Skill:** `implement` (internal Nemesis skill)  
**Input:** `solution.md` or `solution.html`  
**Output:** Code files + tests + quality gates + PR

```
Skill("implement", "<feature-slug>")
```

**Sub-skills invoked by `/implement`:**

1. **Code Generation**: Uses solution.md as spec
2. **Test Generation**: 
   - `quality-engineer` — Unit tests, integration tests
   - `slit-generator-v2` — SLIT tests (Go services only)
3. **Quality Gates**:
   - `engineering:code-review` — Pre-commit review
   - Linters (go fmt, go vet, golangci-lint, eslint, phpcs)
4. **PR Validation**:
   - `gatekeeper` — PR merge criteria enforcement

**Implementation pause points:**
1. After code generation → user approval
2. After test generation → coverage review
3. After quality gates → fix failures
4. Before PR creation → final review

---

### Phase 5: E2E Testing (Test Execution + Validation)

#### Test Execution
**Skill:** `quality-engineer`  
**Input:** Implementation artifacts + test scenarios  
**Output:** Test results, coverage report, failure analysis

```
Skill("quality-engineer", 
     "Tests: <generated_tests>\n" +
     "Coverage: <target>\n" +
     "Scenarios: <e2e_scenarios>")
```

---

#### Deploy Readiness
**Skill:** `engineering:deploy-checklist`  
**Input:** Implementation complete, tests passing  
**Output:** Pre-deploy checklist, rollback plan, monitoring setup

```
Skill("engineering:deploy-checklist", 
     "Feature: <name>\n" +
     "Services: <deployed>\n" +
     "Tests: <results>")
```

---

## Specialized Agent Definitions

### Scout Agent (End-to-End Analysis)

**Purpose:** Deep codebase reconnaissance for large/complex features  
**When to use:** Cross-project features, unknown architecture, ambiguous scope

**Skills invoked:**
1. `engineering:architecture` — Map service dependencies
2. `compass:reviewing-strategy` — Validate scope against capacity
3. `engineering:system-design` — Identify scalability bottlenecks
4. `product-management:brainstorm` — Structure problem space

**Output:** Detailed reconnaissance report (architecture map, complexity estimate, risk flags)

**Agent template:** `agents/scout-agent.md` (created)

---

### Implementation Agent (Code + Tests + PR)

**Purpose:** Generate production-ready code with full test coverage  
**When to use:** After Solutioning complete, solution.md exists

**Skills invoked:**
1. `quality-engineer` — Unit test generation
2. `slit-generator-v2` — SLIT test generation (Go)
3. `engineering:code-review` — Pre-commit review
4. `gatekeeper` — PR merge criteria validation

**Output:** Mergeable GitHub PR with code + tests + quality gates passed

**Agent template:** `agents/implement-agent.md` (already exists, enhance with skills)

---

### Review Agent (Comprehensive Code Audit)

**Purpose:** Multi-dimensional code review before merge  
**When to use:** PR ready, before merge approval

**Skills invoked:**
1. `engineering:code-review` — General code review
2. `compass:razorpay-api-review` — API contract review
3. `engineering:testing-strategy` — Test coverage validation
4. `engineering:deploy-checklist` — Deploy readiness check
5. `pre-mortem` — Last-minute risk discovery

**Output:** Review report with 5-dimension scores + merge recommendation

**Agent template:** `agents/review-agent.md` (already exists, enhance with skills)

---

### Test Generation Agent

**Purpose:** Generate comprehensive test suites  
**When to use:** Code written, tests missing or incomplete

**Skills invoked:**
1. `quality-engineer` — Unit + integration test generation
2. `slit-generator-v2` — SLIT test generation (Go services)
3. `engineering:testing-strategy` — Coverage target validation

**Output:** Test files with >90% coverage target

**Agent template:** `agents/test-gen-agent.md` (already exists, enhance with skills)

---

## Skill Fallback Strategy

**Priority:** Razorpay skills > Internal analysis > @Slash queries

### If Skill Fails to Resolve

1. **Log the failure** (track skill availability issues)
2. **Use Brain context** as primary fallback
3. **Query @Slash** for Razorpay-specific questions
4. **Proceed with phase** using available context
5. **Flag skill gap** in output artifacts

**Never block a phase on skill failure** — graceful degradation required.

---

## Skill Invocation Pattern (Standard)

```typescript
// Pseudocode for all phases
try {
    result = Skill(skill_name, context)
    if result.success:
        integrate_into_phase(result)
        log_skill_usage(skill_name, "success")
    else:
        log_skill_usage(skill_name, "failed")
        fallback_to_brain_context()
except SkillNotFound:
    log_skill_usage(skill_name, "not_found")
    fallback_to_brain_context()
```

---

## Skill Usage Metrics (Track via agentic-skill-metrics)

| Metric | What to Track | Purpose |
|--------|---------------|---------|
| **Invocation Count** | Per skill, per phase | Usage patterns |
| **Success Rate** | Skill resolved vs failed | Reliability |
| **Latency** | Time to complete | Performance |
| **Fallback Rate** | Brain context used instead | Skill availability |
| **User Satisfaction** | Pause point feedback | Quality |

**Export to:** `agentic-skill-metrics` repo (if available)

---

## Implementation Checklist

- [x] Update `commands/nemesis.md` with Skill() invocations at each phase
- [x] Expand SKILLS banner to 16 + add Step 0 Skill Registry preload to dashboard init
- [x] Add 16-skill `SKILL_REGISTRY` to `brain/config.py` + `brain skills` CLI command
- [x] Make `brain init` load/register the skill registry (Signal node) and print count
- [x] Create `agents/scout-agent.md` (new) — 4 skills, read-only reconnaissance
- [x] Enhance `agents/implement-agent.md` with code-review + quality-engineer + slit-generator-v2 + gatekeeper
- [x] Enhance `agents/review-agent.md` with 5 Razorpay skills (5-dimension audit)
- [x] Enhance `agents/test-gen-agent.md` with testing-strategy + quality-engineer + slit-generator-v2
- [x] Add skill fallback logic (Razorpay skill > Brain > @Slash > proceed) to all phases + agents
- [x] Add skill usage logging to Brain (`skill-use:<phase>:<skill>:<feature>` Signal)
- [x] Wire specialized agents into nemesis.md (Specialized Agents table)
- [ ] Test skill resolution via ToolSearch (runtime check)
- [ ] Document skill gaps (which skills are not available) — fill in at first live run
- [ ] Create skill usage dashboard (if agentic-skill-metrics available)

---

## Next Steps

1. **Verify skill availability**: Use ToolSearch to check which Razorpay skills are actually loaded
2. **Update Nemesis protocol**: Integrate Skill() calls at documented touchpoints
3. **Create specialized agents**: Scout, Implementation (enhanced), Review (enhanced), Test Gen (enhanced)
4. **Test end-to-end**: Run GPay Bifrost through updated Nemesis with all skills
5. **Measure improvement**: Compare v1 (no skills) vs v2 (all skills) output quality

---

**End of Skill Integration Map**
