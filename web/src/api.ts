// Typed client for the Ledger Sentinel API. The Vite dev server proxies these
// paths to the FastAPI app, so all URLs are relative.

export interface Evidence {
  source_table: string;
  row_id: number;
  note?: string | null;
}

export interface JELine {
  account_code: string;
  debit: number;
  credit: number;
}

export interface JE {
  id: number;
  period: string;
  rule: string;
  reason: string;
  confidence: number;
  status: string;
  created_at: string;
  evidence: Evidence[];
  lines: JELine[];
}

export interface ExceptionItem {
  id: number;
  period: string;
  category: string;
  description: string;
  source_table: string | null;
  row_id: number | null;
  status: string;
  resolution: string | null;
  resolution_reason: string | null;
  created_at: string;
}

export interface PolicyRule {
  id: number;
  scope: string;
  condition: string;
  action: string;
  limits: Record<string, unknown> | null;
  active: boolean;
  expires: string | null;
  source_exception_id: number | null;
  created_at: string;
}

export interface Metrics {
  period: string;
  je_count: number;
  auto_approved_count: number;
  needs_review_count: number;
  exception_count: number;
  open_exception_count: number;
  exceptions_by_category: Record<string, number>;
  exceptions_by_status: Record<string, number>;
  escalation_rate: number;
  rule_count: number;
  active_rule_count: number;
}

export interface DistillResult {
  candidate: {
    scope: string;
    condition: string;
    action: string;
    limits: Record<string, unknown> | null;
    active: boolean;
    expires: string | null;
  };
  promoted: boolean;
  rule_id: number | null;
  flips: string[];
  replayed_periods: string[];
}

export class ApiError extends Error {
  status: number;

  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") detail = body.detail;
      else if (body.detail) detail = JSON.stringify(body.detail);
    } catch {
      // keep the status text
    }
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

function withParams(path: string, params: Record<string, string>): string {
  const query = new URLSearchParams(
    Object.entries(params).filter(([, value]) => value !== ""),
  ).toString();
  return query ? `${path}?${query}` : path;
}

export function fetchJEs(period: string, status: string): Promise<JE[]> {
  return request(withParams("/jes", { period, status }));
}

export function fetchExceptions(period: string, status: string): Promise<ExceptionItem[]> {
  return request(withParams("/exceptions", { period, status }));
}

export function fetchEvidence(sourceTable: string, rowId: number): Promise<Record<string, unknown>> {
  return request(
    withParams("/evidence", { source_table: sourceTable, row_id: String(rowId) }),
  );
}

export function fetchRules(): Promise<PolicyRule[]> {
  return request("/rules");
}

export function disableRule(ruleId: number): Promise<PolicyRule> {
  return request(`/rules/${ruleId}/disable`, { method: "POST" });
}

export interface CloseRunResult {
  period: string;
  je_count: number;
  auto_approved_count: number;
  needs_review_count: number;
  exception_count: number;
  stats: Record<string, unknown>;
}

export function runClose(period: string, priorPeriodValue: string | null): Promise<CloseRunResult> {
  return request("/close/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(
      priorPeriodValue ? { period, prior_period: priorPeriodValue } : { period },
    ),
  });
}

export function fetchMetrics(period: string): Promise<Metrics> {
  return request(withParams("/metrics", { period }));
}

export function resolveException(
  exceptionId: number,
  resolution: string,
  reason: string,
  actor: string,
): Promise<{ id: number; status: string; resolution: string; resolution_reason: string }> {
  return request(`/exceptions/${exceptionId}/resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ resolution, reason, actor }),
  });
}

export function distillException(exceptionId: number): Promise<DistillResult> {
  return request(`/exceptions/${exceptionId}/distill`, { method: "POST" });
}

export function formatMoney(value: number): string {
  return value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function formatPercent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

// "2025-08" to "2025-07"; wraps January back to December of the prior year.
export function priorPeriod(period: string): string | null {
  const match = /^(\d{4})-(\d{2})$/.exec(period);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  if (month === 1) return `${year - 1}-12`;
  return `${year}-${String(month - 1).padStart(2, "0")}`;
}
