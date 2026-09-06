import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  DistillResult,
  ExceptionItem,
  distillException,
  fetchExceptions,
  resolveException,
} from "../api";
import { EvidenceList, StatusBadge } from "../components";

interface DistillState {
  loading: boolean;
  result?: DistillResult;
  error?: string;
}

export function ExceptionsPage() {
  const [period, setPeriod] = useState("");
  const [status, setStatus] = useState("");
  const [actor, setActor] = useState("reviewer");
  const [items, setItems] = useState<ExceptionItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [reasons, setReasons] = useState<Record<number, string>>({});
  const [distills, setDistills] = useState<Record<number, DistillState>>({});

  const load = useCallback(async () => {
    setError(null);
    try {
      setItems(await fetchExceptions(period, status));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "failed to load exceptions");
    }
  }, [period, status]);

  useEffect(() => {
    void load();
  }, [load]);

  async function resolve(id: number, resolution: string) {
    const reason = (reasons[id] ?? "").trim();
    if (!reason) return;
    setError(null);
    try {
      await resolveException(id, resolution, reason, actor.trim() || "reviewer");
      setReasons((prev) => ({ ...prev, [id]: "" }));
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "failed to resolve exception");
    }
  }

  async function distill(id: number) {
    setDistills((prev) => ({ ...prev, [id]: { loading: true } }));
    try {
      const result = await distillException(id);
      setDistills((prev) => ({ ...prev, [id]: { loading: false, result } }));
    } catch (err) {
      const message =
        err instanceof ApiError && err.status === 503
          ? `LLM unavailable, try again later (${err.message})`
          : err instanceof ApiError
            ? err.message
            : "distillation failed";
      setDistills((prev) => ({ ...prev, [id]: { loading: false, error: message } }));
    }
  }

  return (
    <section>
      <h2>Exception queue</h2>
      <div className="filters">
        <label>
          Period
          <input
            value={period}
            onChange={(event) => setPeriod(event.target.value)}
            placeholder="YYYY-MM (all)"
          />
        </label>
        <label>
          Status
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">all</option>
            <option value="open">open</option>
            <option value="resolved">resolved</option>
          </select>
        </label>
        <label>
          Acting as
          <input value={actor} onChange={(event) => setActor(event.target.value)} />
        </label>
        <button type="button" onClick={() => void load()}>
          Refresh
        </button>
      </div>
      {error && <div className="notice bad">{error}</div>}
      {items.length === 0 && <p className="muted">No exceptions match these filters.</p>}
      {items.map((item) => (
        <div key={item.id} className="card">
          <div>
            <strong>#{item.id}</strong> <StatusBadge status={item.status} />{" "}
            <span className="badge neutral">{item.category}</span>{" "}
            <span className="badge neutral">{item.period}</span>
          </div>
          <p>{item.description}</p>
          {item.source_table && item.row_id !== null && (
            <EvidenceList evidence={[{ source_table: item.source_table, row_id: item.row_id }]} />
          )}
          {item.status === "open" ? (
            <div>
              <div className="row-actions">
                <textarea
                  rows={2}
                  placeholder="Reason (required)"
                  value={reasons[item.id] ?? ""}
                  onChange={(event) =>
                    setReasons((prev) => ({ ...prev, [item.id]: event.target.value }))
                  }
                />
              </div>
              <div className="row-actions">
                {["approve", "reject", "edit"].map((resolution) => (
                  <button
                    key={resolution}
                    type="button"
                    className={resolution === "approve" ? "primary" : undefined}
                    disabled={!(reasons[item.id] ?? "").trim()}
                    onClick={() => void resolve(item.id, resolution)}
                  >
                    {resolution}
                  </button>
                ))}
                {!(reasons[item.id] ?? "").trim() && (
                  <span className="muted">enter a reason to enable actions</span>
                )}
              </div>
            </div>
          ) : (
            <div>
              <div className="meta">
                Resolved as <strong>{item.resolution}</strong>: {item.resolution_reason}
              </div>
              <div className="row-actions">
                <button
                  type="button"
                  disabled={distills[item.id]?.loading}
                  onClick={() => void distill(item.id)}
                >
                  {distills[item.id]?.loading ? "Distilling..." : "Distill rule"}
                </button>
              </div>
              <DistillOutcome state={distills[item.id]} />
            </div>
          )}
        </div>
      ))}
    </section>
  );
}

function DistillOutcome({ state }: { state?: DistillState }) {
  if (!state || state.loading) return null;
  if (state.error) return <div className="notice bad">{state.error}</div>;
  if (!state.result) return null;
  const { candidate, promoted, rule_id, flips, replayed_periods } = state.result;
  return (
    <div className={`notice ${promoted ? "ok" : "warn"}`}>
      <div>
        Candidate rule: <span className="mono">{candidate.scope}</span> when {candidate.condition},
        then {candidate.action}
      </div>
      <div>
        Regression gate over {replayed_periods.length} prior period(s):{" "}
        {promoted
          ? `passed, promoted as rule #${rule_id}`
          : "failed, the rule was not promoted"}
      </div>
      {flips.length > 0 && (
        <ul className="flips">
          {flips.map((flip, index) => (
            <li key={index}>{flip}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
