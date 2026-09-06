# Demo script (3 to 5 minutes)

## Setup before recording

Run these ahead of time so the demo opens on a working app with a fresh database:

```bash
uv sync
uv run python -m sentinel.datagen
SENTINEL_DB=data/sentinel.db uv run uvicorn sentinel.api:app
```

In a second terminal:

```bash
cd web
npm install
npm run dev -- --host 127.0.0.1
```

Open http://127.0.0.1:5173. Keep a third terminal ready for the curl commands below. The rule distillation step calls the model, so have `ANTHROPIC_API_KEY` exported in the uvicorn terminal (or the local `claude` CLI logged in); without either, the distill button returns "LLM unavailable" and the rest of the demo still works.

## 0:00 - The problem

Say: "Month-end close is days of reconciliation, accrual hunting, and anomaly chasing. Ledger Sentinel closes the books autonomously, escalates only true judgment calls, and learns a policy rule from every human decision so it escalates less next month. Its core guarantee: no journal entry exists without evidence pointing at source rows."

## 0:30 - Run the close live

In the terminal:

```bash
curl -X POST http://127.0.0.1:8000/close/run -H "Content-Type: application/json" -d '{"period": "2026-01"}'
```

Point at the response: 4 journal entries proposed, 3 auto-approved, 1 needs review, 8 exceptions, and a 100 percent bank auto-match rate, in under a second.

## 1:00 - A journal entry and its evidence chain

In the browser, click the **Journal entries** tab. Type `2026-01` in the Period filter.

- Click the `bank_fee` row. It expands to show the balanced debit and credit lines and an **Evidence** section.
- Click the evidence chip (`bank_lines #...`). The actual source row appears inline: the bank descriptor "MONTHLY SERVICE FEE", the date, the amount.

Say: "Every entry carries evidence like this. The schema rejects an entry with no evidence, so a hallucinated plug entry is structurally impossible."

## 1:45 - Resolve an exception with a reason

Click the **Exceptions** tab. Filter Period to `2026-01`, Status to `open`. Pick a `vendor_name_variance` exception: the description names both suspicious vendor spellings, and clicking the evidence chip pulls up the duplicate vendor's master record.

- The approve, reject, and edit buttons are disabled until a reason is entered.
- Type a reason such as `Same vendor, descriptor variant of the master record` and click **approve**.

Say: "A human touches only the judgment calls, and every resolution requires a written reason. That reason is not just an audit trail, it is training signal."

## 2:15 - Distill a rule and pass the regression gate

The resolved exception card now shows a **Distill rule** button. Click it.

- The candidate rule appears: scope, condition, action, with limits.
- Below it, the regression gate result: the rule was replayed against prior periods and promoted only because it flipped no previously correct decision. The notice shows "passed, promoted as rule #1".

Say: "The rule was replayed against every prior month before promotion. If it had flipped a single previously correct decision, it would have been rejected. That gate is what makes learning safe."

Click the **Rules** tab to show the learned policy rule listed with its scope and condition, and the Disable button for human override.

## 2:45 - Close the next month and show the metrics

In the terminal:

```bash
curl -X POST http://127.0.0.1:8000/close/run -H "Content-Type: application/json" -d '{"period": "2026-02", "prior_period": "2026-01"}'
```

Click the **Metrics** tab and type `2026-02` in the Period field. Walk the month-over-month table: journal entries proposed, auto-approved count, needs review, exceptions, and the escalation rate bars for 2026-02 next to 2026-01. Point out the active learned rule count at the bottom.

## 3:30 - The eval curve

Show `eval/escalation.png` (or the Evaluation results section of the README).

Say: "The committed eval closes all three months headless with a simulated reviewer and scores against planted ground truth: precision 0.97, recall 1.0, F1 0.985 over 33 planted anomalies, and the escalation rate falls month over month, 75 percent to 69 to 64, because promoted rules keep taking work off the reviewer's desk. Reproduce it with one command:"

```bash
uv run python -m eval.run --db data/eval.db --seed 42 --out eval/results.json
```

## 4:15 - Close

Say: "Deterministic engines do the accounting, the LLM only resolves ambiguity and distills rules, every entry ships with evidence, and the system provably gets more autonomous every month. Built end to end by Agent Orchestrator worker sessions, one PR per component through review and CI." Show the AO board if recording it.
