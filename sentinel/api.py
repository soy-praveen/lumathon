"""Minimal FastAPI app. The review UI adds real routes on top of this."""

from fastapi import FastAPI

app = FastAPI(title="Ledger Sentinel")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
