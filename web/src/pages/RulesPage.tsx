import { useCallback, useEffect, useState } from "react";
import { ApiError, PolicyRule, disableRule, fetchRules } from "../api";

export function RulesPage() {
  const [rules, setRules] = useState<PolicyRule[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setRules(await fetchRules());
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "failed to load rules");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function disable(ruleId: number) {
    setError(null);
    try {
      await disableRule(ruleId);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "failed to disable rule");
    }
  }

  return (
    <section>
      <h2>Learned policy rules</h2>
      {error && <div className="notice bad">{error}</div>}
      {rules.length === 0 ? (
        <p className="muted">
          No rules yet. Resolve an exception and distill it to teach the agent.
        </p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Rule</th>
              <th>Scope</th>
              <th>Condition</th>
              <th>Action</th>
              <th>Limits</th>
              <th>Expires</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rules.map((rule) => (
              <tr key={rule.id}>
                <td>#{rule.id}</td>
                <td className="mono">{rule.scope}</td>
                <td>{rule.condition}</td>
                <td>{rule.action}</td>
                <td className="mono">{rule.limits ? JSON.stringify(rule.limits) : ""}</td>
                <td>{rule.expires ?? "never"}</td>
                <td>
                  <span className={`badge ${rule.active ? "ok" : "neutral"}`}>
                    {rule.active ? "active" : "disabled"}
                  </span>
                </td>
                <td>
                  {rule.active && (
                    <button type="button" className="danger" onClick={() => void disable(rule.id)}>
                      Disable
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
