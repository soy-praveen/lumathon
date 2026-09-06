# Ledger Sentinel: Devpost submission

## Track and problem statement

**Track 2: Autonomous Office of the CFO.**

Month-end close is one of the most repetitive, deadline-driven workflows in finance: reconcile the bank statement against the ledger, hunt for accruals that belong in the period, explain every material account movement, and catch anomalies like duplicate invoices or out-of-period postings. Most of it is mechanical; a small fraction genuinely needs human judgment. Ledger Sentinel automates the mechanical part end to end, escalates only the judgment calls, and, the part existing tools miss, turns each human decision into a guardrailed policy rule so the same call never needs a human twice.

## What it does

Each period the agent runs four close tasks over a synthetic ERP in SQLite:

1. **Bank reconciliation**: exact, fuzzy, and one-to-many matching of bank lines to GL cash entries, including Dodo Payments payout reconciliation. It proposes journal entries for bank fees, interest, and timing differences; unmatched lines become exceptions.
2. **Accruals**: received-not-invoiced purchase orders and missing recurring vendor bills, proposed as accrual entries.
3. **Flux analysis**: every account movement beyond threshold versus the prior month, explained from underlying transactions or escalated.
4. **Anomaly checks**: duplicate invoices, vendor name variance, round-number split invoices near approval limits, out-of-period postings.

Output is a proposed journal entry batch, an exception queue in a React review UI (approve, reject, or edit, with a mandatory written reason), a metrics dashboard, and a full audit log.

**The core guarantee**: every proposed journal entry carries an `evidence[]` list referencing specific source rows. The pydantic schema rejects any entry with empty evidence or unbalanced lines, so an entry without evidence cannot exist anywhere in the system. A hallucinated plug entry is structurally impossible, not just discouraged.

## Agent architecture

- **Deterministic engines do the heavy lifting.** Matching, accrual detection, flux, and anomaly logic are plain Python over SQL rows (`sentinel/recon`, `sentinel/accrual`, `sentinel/flux`, `sentinel/anomaly`), each unit tested against fixtures.
- **Agent core** (`sentinel/agent`): `run_close` orchestrates the engines per period, triages proposed entries into auto-approved and needs-review confidence bands, opens exceptions, and audits every action.
- **LLM layer** (`sentinel/llm.py`): the only module allowed to call a model (Anthropic SDK when a key is set, otherwise the local claude CLI headless). It is used solely for ambiguity resolution, narrative, and rule distillation. It never creates entries, and every model call degrades gracefully: if the LLM is unavailable the item escalates to a human instead.
- **The learning loop** (`sentinel/agent/rules.py`): when a human resolves an exception with a reason, the agent distills a candidate policy rule (scope, condition, action, limits). The candidate is replayed against all prior months as a deterministic regression gate and promoted only if it flips no previously correct decision. Promoted rules apply on the next close and can be viewed and disabled in the UI.
- **Review UI**: FastAPI plus Vite/React with tabs for exceptions, journal entries with evidence drill-down (each evidence chip fetches the actual source row through a whitelisted evidence endpoint), close metrics month over month, and learned rules.
- **Tracing**: Neatlogs instrumentation over every run: one workflow trace per close, a span per engine, a span per LLM call. Without a key it is a silent no-op.

## Evaluation approach and results

The data generator plants 33 labeled anomalies across 9 categories over three months (seed 42, committed ground truth). The eval harness (`eval/run.py`) closes all three months headless, simulates the human review loop between months (resolving every needs-review entry and open exception against ground truth, then distilling and promoting rules through the regression gate), and scores every detection.

From the committed `eval/results.json` (LLM disabled, fully deterministic run):

- **Precision 0.9706, recall 1.0, F1 0.9851** overall: 33 true positives, 1 false positive, 0 misses. The single false positive is one duplicate-invoice flag in 2026-02; recall is 1.0 in every category.
- **Bank auto-match rate 1.0** in all three months.
- **Escalation rate falls month over month: 0.75 to 0.6923 to 0.6364**, because each month's reviewed decisions become promoted rules (1 per month, 0 rejected by the gate) that handle the same situations automatically.
- Each close completes in under half a second of wall time.

Charts are committed as `eval/escalation.png` and `eval/f1_by_category.png`, and the whole eval reproduces with `uv run python -m eval.run --db data/eval.db --seed 42 --out eval/results.json`.

## How AO was used

An orchestrator session planned the build and spawned worker sessions, each in its own git worktree and branch: data generator, bank reconciliation engine, accrual and flux engines, agent core with the learning loop, review UI, eval harness, and documentation. Every component shipped as one focused PR through review and CI (ruff and pytest on every PR) before merging to main. The orchestrator coordinated the interface contracts between parallel workers (database schema, engine signatures, rule grammar) and dispatched follow-up fix sessions when the eval surfaced engine bugs.

## Novelty

Two things most close-automation entries stop short of:

1. **Evidence-first design.** Evidence is not an explanation bolted on afterwards; it is a validation constraint. No journal entry object can be constructed without citing source rows, and the review UI resolves every citation back to the actual row.
2. **A learning loop gated by regression tests.** Human review is not a dead end; each resolution is distilled into a narrow policy rule, but a rule is promoted only after being replayed against all prior months without flipping a previously correct decision. The measured, committed result is the escalation curve declining month over month with recall held at 1.0.

## Submission checklist

- Track and problem statement: this document, above.
- Public GitHub repo with README covering architecture and eval results: https://github.com/soy-praveen/lumathon (see `README.md`).
- 3 to 5 minute demo video: script in `docs/DEMO.md`, showing the AO board and the product.
- Explanation of agent architecture and evaluation: above.
- How AO was used: above.
- Team member names: listed in the Team section of `README.md`.
