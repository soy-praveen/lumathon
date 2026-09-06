import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  CloseRunResult,
  Metrics,
  fetchMetrics,
  formatPercent,
  priorPeriod,
  runClose,
} from "../api";

interface MetricRow {
  label: string;
  current: number;
  prior: number | null;
  percent?: boolean;
}

export function MetricsPage() {
  const [period, setPeriod] = useState("2026-01");
  const [current, setCurrent] = useState<Metrics | null>(null);
  const [prior, setPrior] = useState<Metrics | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [lastRun, setLastRun] = useState<CloseRunResult | null>(null);

  const load = useCallback(async () => {
    setError(null);
    const prev = priorPeriod(period);
    if (!prev) {
      setError("enter a period as YYYY-MM");
      return;
    }
    try {
      const [now, before] = await Promise.all([fetchMetrics(period), fetchMetrics(prev)]);
      setCurrent(now);
      setPrior(before);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "failed to load metrics");
    }
  }, [period]);

  useEffect(() => {
    void load();
  }, [load]);

  const close = useCallback(async () => {
    setError(null);
    setRunning(true);
    try {
      const result = await runClose(period, priorPeriod(period));
      setLastRun(result);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "close run failed");
    } finally {
      setRunning(false);
    }
  }, [period, load]);

  const rows: MetricRow[] = current
    ? [
        { label: "Journal entries proposed", current: current.je_count, prior: prior?.je_count ?? null },
        {
          label: "Auto approved",
          current: current.auto_approved_count,
          prior: prior?.auto_approved_count ?? null,
        },
        {
          label: "Needs review",
          current: current.needs_review_count,
          prior: prior?.needs_review_count ?? null,
        },
        {
          label: "Exceptions raised",
          current: current.exception_count,
          prior: prior?.exception_count ?? null,
        },
        {
          label: "Exceptions still open",
          current: current.open_exception_count,
          prior: prior?.open_exception_count ?? null,
        },
        {
          label: "Escalation rate",
          current: current.escalation_rate,
          prior: prior?.escalation_rate ?? null,
          percent: true,
        },
      ]
    : [];

  const categories = new Set<string>([
    ...Object.keys(current?.exceptions_by_category ?? {}),
    ...Object.keys(prior?.exceptions_by_category ?? {}),
  ]);

  return (
    <section>
      <h2>Close metrics</h2>
      <div className="filters">
        <label>
          Period
          <input
            value={period}
            onChange={(event) => setPeriod(event.target.value)}
            placeholder="YYYY-MM"
          />
        </label>
        <button type="button" onClick={() => void load()}>
          Refresh
        </button>
        <button type="button" className="primary" disabled={running} onClick={() => void close()}>
          {running ? "Closing " + period + "..." : "Run close for " + period}
        </button>
      </div>
      {error && <div className="notice bad">{error}</div>}
      {lastRun && (
        <div className="notice">
          Closed {lastRun.period}: {lastRun.je_count} journal entries proposed,{" "}
          {lastRun.auto_approved_count} auto approved, {lastRun.needs_review_count} for review,{" "}
          {lastRun.exception_count} exceptions raised.
        </div>
      )}
      {current && (
        <div>
          <table>
            <thead>
              <tr>
                <th>Metric</th>
                <th className="num">{current.period}</th>
                <th className="num">{prior ? prior.period : "prior"}</th>
                <th>Month over month</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.label}>
                  <td>{row.label}</td>
                  <td className="num">{row.percent ? formatPercent(row.current) : row.current}</td>
                  <td className="num">
                    {row.prior === null ? "" : row.percent ? formatPercent(row.prior) : row.prior}
                  </td>
                  <td>
                    <Bars current={row.current} prior={row.prior} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>Exceptions by category</h3>
          {categories.size === 0 ? (
            <p className="muted">No exceptions in either period.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Category</th>
                  <th className="num">{current.period}</th>
                  <th className="num">{prior ? prior.period : "prior"}</th>
                </tr>
              </thead>
              <tbody>
                {[...categories].sort().map((category) => (
                  <tr key={category}>
                    <td>{category}</td>
                    <td className="num">{current.exceptions_by_category[category] ?? 0}</td>
                    <td className="num">{prior?.exceptions_by_category[category] ?? 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <h3>Rules</h3>
          <p>
            {current.active_rule_count} active of {current.rule_count} learned policy rules.
          </p>
        </div>
      )}
    </section>
  );
}

function Bars({ current, prior }: { current: number; prior: number | null }) {
  const max = Math.max(current, prior ?? 0);
  const width = (value: number) => (max > 0 ? `${(value / max) * 100}%` : "0%");
  return (
    <div>
      <span className="bar-track">
        <span className="bar-fill" style={{ width: width(current) }} />
      </span>
      {prior !== null && (
        <div>
          <span className="bar-track">
            <span className="bar-fill prior" style={{ width: width(prior) }} />
          </span>
        </div>
      )}
    </div>
  );
}
