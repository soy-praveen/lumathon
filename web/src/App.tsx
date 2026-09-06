import { useState } from "react";
import { ExceptionsPage } from "./pages/ExceptionsPage";
import { JEsPage } from "./pages/JEsPage";
import { MetricsPage } from "./pages/MetricsPage";
import { RulesPage } from "./pages/RulesPage";

const TABS = [
  { key: "exceptions", label: "Exceptions" },
  { key: "jes", label: "Journal entries" },
  { key: "metrics", label: "Metrics" },
  { key: "rules", label: "Rules" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

export function App() {
  const [tab, setTab] = useState<TabKey>("exceptions");
  return (
    <div>
      <header className="app-header">
        <h1>Ledger Sentinel</h1>
        <nav>
          {TABS.map((item) => (
            <button
              key={item.key}
              type="button"
              className={`tab ${tab === item.key ? "active" : ""}`}
              onClick={() => setTab(item.key)}
            >
              {item.label}
            </button>
          ))}
        </nav>
      </header>
      <main>
        {tab === "exceptions" && <ExceptionsPage />}
        {tab === "jes" && <JEsPage />}
        {tab === "metrics" && <MetricsPage />}
        {tab === "rules" && <RulesPage />}
      </main>
    </div>
  );
}
