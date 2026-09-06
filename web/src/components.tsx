import { useState } from "react";
import { ApiError, Evidence, fetchEvidence } from "./api";

export function StatusBadge({ status }: { status: string }) {
  const tone =
    status === "auto_approved" || status === "approved" || status === "resolved"
      ? "ok"
      : status === "needs_review" || status === "open"
        ? "warn"
        : status === "rejected"
          ? "bad"
          : "neutral";
  return <span className={`badge ${tone}`}>{status.replace("_", " ")}</span>;
}

export function ConfidenceBadge({ confidence }: { confidence: number }) {
  const tone = confidence >= 0.9 ? "ok" : confidence >= 0.7 ? "warn" : "bad";
  return <span className={`badge ${tone}`}>{(confidence * 100).toFixed(0)}%</span>;
}

interface EvidenceListProps {
  evidence: Evidence[];
}

export function EvidenceList({ evidence }: EvidenceListProps) {
  const [selected, setSelected] = useState<string | null>(null);
  const [row, setRow] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function show(item: Evidence) {
    const key = `${item.source_table}:${item.row_id}`;
    if (selected === key) {
      setSelected(null);
      setRow(null);
      return;
    }
    setSelected(key);
    setRow(null);
    setError(null);
    try {
      setRow(await fetchEvidence(item.source_table, item.row_id));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "failed to load evidence row");
    }
  }

  if (evidence.length === 0) {
    return <span className="muted">no evidence</span>;
  }
  return (
    <div>
      {evidence.map((item, index) => (
        <button
          key={index}
          type="button"
          className="evidence-chip"
          onClick={() => void show(item)}
          title={item.note ?? undefined}
        >
          {item.source_table} #{item.row_id}
        </button>
      ))}
      {selected && (
        <div className="evidence-detail">
          <div className="muted mono">{selected}</div>
          {error && <div className="notice bad">{error}</div>}
          {row && (
            <table>
              <tbody>
                {Object.entries(row).map(([key, value]) => (
                  <tr key={key}>
                    <th>{key}</th>
                    <td className="mono">{value === null ? "" : String(value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}
