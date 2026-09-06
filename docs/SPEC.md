# Ledger Sentinel: Autonomous Month-End Close That Learns

**Track 2: Autonomous Office of the CFO**
**Hackathon:** Syndicate by Maximor (Sept 5-6, 2026)

## One-liner

An autonomous month-end close agent that runs bank reconciliation, accrual detection,
and flux analysis end to end, escalates only genuine judgment calls to a human, and
turns every human decision into a guardrailed policy rule so it escalates less next month
without losing accuracy. Every proposed journal entry ships with an evidence chain.

## Why this wins

- **Track fit (25%)**: automates a real internal finance workflow (closing the books)
  including exceptions and human review, which is literally the track brief.
- **Real-world value**: mirrors the sponsor's own thesis (~98% autonomous, ~2% escalated,
  then the system learns). Judges from Maximor will recognise it instantly.
- **Measurable**: synthetic company with planted anomalies and known ground truth, so we
  report precision/recall, escalation rate, auto-match rate, cost, and latency for
  Month 1 → Month 2 → Month 3. The curve going down is the demo.
- **Novelty (10%)**: the learn-from-review loop with rule promotion gated by regression
  tests, plus evidence-first design. Most entries stop at "LLM categorises transactions".
- **Reliability (25%)**: deterministic matching engine does the heavy lifting; the LLM
  only handles ambiguity and explanation. Hallucinated plug entries are structurally
  impossible (every JE must cite source rows).

## Workflow the agent owns

Inputs per month (synthetic, SQLite "ERP"):
- General ledger journal + trial balance (prior month + current)
- Bank statement lines
- AP invoices, vendor master
- Purchase orders + goods receipts (for received-not-invoiced accruals)
- Recurring vendor calendar (expected monthly bills)
- Dodo Payments payouts (sponsor tie-in: reconcile payouts to bank deposits + revenue)

Close tasks:
1. **Bank reconciliation**: match bank lines ↔ GL cash entries (exact, fuzzy amount/date,
   one-to-many). Propose JEs for bank fees, interest, timing differences. Unmatched → exception.
2. **Accruals**: detect received-not-invoiced POs and missing recurring bills; propose
   accrual JEs with reversal next month.
3. **Flux analysis**: explain every account movement beyond threshold vs prior month using
   underlying transactions; unexplained → exception.
4. **Anomaly checks**: duplicate invoices, vendor name variance, round-number/split
   invoices near approval limits, out-of-period postings.

Outputs:
- Proposed JE batch, each with `evidence[]` (source table + row ids), rule/reason,
  confidence, and status (auto-approved / needs review).
- Exception queue in a web UI: approve / reject / edit, with mandatory reason.
- Close package export (CSV + markdown/PDF): rec summary, JE listing, exception log,
  full audit trail.
- Metrics dashboard.

## The learning loop (the novel part)

1. Human resolves an exception with a reason.
2. Agent distils a candidate **policy rule** (structured: scope, condition, action, limits).
   Example: "Vendor 'AWS' bank descriptor 'AMZN WEB SERV*' → match to AWS invoices, tolerance ±1%, cap $50k".
3. Candidate rule is replayed against all prior months (regression). Promote only if it
   does not flip any previously-correct decision.
4. Promoted rules apply next month with lower confidence threshold; the agent cites the
   rule id in evidence. Rules can be viewed, disabled, and expire.
5. Report: escalation rate and human minutes per close, month over month.

## Evaluation harness

- Generator plants N anomalies per month across categories with known labels.
- `eval/` runs the close headless for months 1-3 and reports:
  precision, recall, F1 per category; escalation rate; auto-match rate; LLM cost; wall time.
- Results committed as `eval/results.json` + chart for the README and demo.

## Architecture

- Python 3.13, FastAPI, SQLite (SQLAlchemy), pydantic models
- Deterministic engines: `recon/` (matching), `accrual/`, `flux/`, `anomaly/`
- LLM layer (Anthropic SDK): ambiguity resolution, flux narrative, rule distillation.
  Default model claude-sonnet-5; judge/validation with claude-opus-5 if budget allows.
- Tracing: Neatlogs SDK on every agent run (sponsor/venue partner).
- UI: Vite + React (or HTMX) review queue, JE viewer with evidence drill-down, metrics.
- Tests: pytest, GitHub Actions CI (so AO's PR/CI feedback loop is visible in the demo).

## AO build plan (25% of score)

Orchestrator session plans and spawns workers, each in its own worktree/branch/PR:
1. `data-gen`: synthetic company + anomaly planting + ground truth
2. `recon-engine`: matching + JE proposal + evidence model
3. `accrual-flux`: accrual detection + flux analysis
4. `agent-core`: LLM layer, exception handling, rule distillation + regression gate
5. `review-ui`: FastAPI routes + frontend
6. `evals`: harness, metrics, charts
7. `demo-docs`: README, architecture diagram, demo script, Devpost write-up

Every PR goes through AO review → CI → merge. Record the AO board for the demo video.

## Submission checklist (Devpost)

- Track + problem statement
- GitHub repo (public), README with architecture + eval results
- 3-5 min demo video showing AO sessions/board + the product
- Explanation of agent architecture and evaluation
- How AO was used
- Team member names
