# Ledger Sentinel — conventions for every agent session

Read `docs/SPEC.md` first. It is the source of truth for scope and architecture.

## Stack
- Python 3.12+, managed with `uv` (`uv sync`, `uv run pytest`). Keep `pyproject.toml` the single dependency list.
- FastAPI + SQLite (SQLAlchemy 2.x) + pydantic v2. Frontend: Vite + React + TypeScript in `web/`.
- Lint with `ruff`, test with `pytest`. Run both before opening a PR. CI runs them on every PR.
- LLM access goes through `sentinel/llm.py` only. It uses the Anthropic SDK when `ANTHROPIC_API_KEY` is set, otherwise shells out to the local `claude -p` CLI (headless, `--output-format json`). Never call a model anywhere else.
- Tracing: wrap agent runs with the Neatlogs SDK when `NEATLOGS_API_KEY` is set; no-op otherwise.

## Rules
- Work only inside your own worktree. Never touch files outside the repo, never change global git or system config.
- Commits: short imperative subject, plain body. NO `Co-Authored-By` trailers, no mention of Claude, Anthropic, or AI assistance anywhere in commits, PR titles, PR bodies, code, comments, or docs. Use the repository's configured git identity as is.
- No `--no-verify`, no force pushes, no history rewrites.
- One PR per task, focused. Include a short "How to test" section in the PR body.
- Every proposed journal entry must carry `evidence[]` referencing source rows. No entry may be created without evidence. This is the product's core guarantee.
- Deterministic engines (matching, accrual, flux, anomaly) must be unit tested with fixtures. The LLM layer is only for ambiguity resolution, narrative, and rule distillation.
- Keep synthetic data realistic: real-looking vendor names, bank descriptors, amounts with cents, dates within the period.
- Prefer small, readable modules over clever abstractions. No emojis in code, docs, or UI.
