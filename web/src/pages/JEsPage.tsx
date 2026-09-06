import { Fragment, useCallback, useEffect, useState } from "react";
import { ApiError, JE, fetchJEs, formatMoney } from "../api";
import { ConfidenceBadge, EvidenceList, StatusBadge } from "../components";

export function JEsPage() {
  const [period, setPeriod] = useState("");
  const [status, setStatus] = useState("");
  const [jes, setJes] = useState<JE[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [openId, setOpenId] = useState<number | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setJes(await fetchJEs(period, status));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "failed to load journal entries");
    }
  }, [period, status]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section>
      <h2>Proposed journal entries</h2>
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
            <option value="auto_approved">auto approved</option>
            <option value="needs_review">needs review</option>
            <option value="approved">approved</option>
            <option value="rejected">rejected</option>
          </select>
        </label>
        <button type="button" onClick={() => void load()}>
          Refresh
        </button>
      </div>
      {error && <div className="notice bad">{error}</div>}
      {jes.length === 0 ? (
        <p className="muted">No journal entries match these filters.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>JE</th>
              <th>Period</th>
              <th>Rule</th>
              <th>Reason</th>
              <th>Confidence</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {jes.map((je) => (
              <Fragment key={je.id}>
                <tr
                  className="clickable"
                  onClick={() => setOpenId(openId === je.id ? null : je.id)}
                >
                  <td>#{je.id}</td>
                  <td>{je.period}</td>
                  <td className="mono">{je.rule}</td>
                  <td>{je.reason}</td>
                  <td>
                    <ConfidenceBadge confidence={je.confidence} />
                  </td>
                  <td>
                    <StatusBadge status={je.status} />
                  </td>
                </tr>
                {openId === je.id && (
                  <tr className="je-detail">
                    <td colSpan={6}>
                      <h3>Lines</h3>
                      <table>
                        <thead>
                          <tr>
                            <th>Account</th>
                            <th className="num">Debit</th>
                            <th className="num">Credit</th>
                          </tr>
                        </thead>
                        <tbody>
                          {je.lines.map((line, index) => (
                            <tr key={index}>
                              <td className="mono">{line.account_code}</td>
                              <td className="num">{formatMoney(line.debit)}</td>
                              <td className="num">{formatMoney(line.credit)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                      <h3>Evidence</h3>
                      <EvidenceList evidence={je.evidence} />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
