"use client";
import { useEffect, useState } from "react";
import Nav from "@/components/Nav";
import { getMetrics } from "@/lib/api";

export default function Metrics() {
  const [m, setM] = useState<any>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    getMetrics().then(setM).catch((e) => setErr(String(e)));
  }, []);

  const pct = (v: number) => `${(v * 100).toFixed(1)}%`;
  const rows = m ? [
    ["工单总数", m.tickets],
    ["成功率", pct(m.success_rate)],
    ["正确失败率", pct(m.correct_failure_rate)],
    ["平均延迟(ms)", m.avg_latency_ms],
    ["总成本", m.total_cost],
    ["人工接管率", pct(m.human_takeover_rate)],
  ] : [];

  return (
    <div className="container">
      <Nav />
      <h2>系统监控评估</h2>
      {err && <div className="card muted">加载失败: {err}</div>}
      <div className="card" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(160px,1fr))", gap: 12 }}>
        {rows.map(([k, v]) => (
          <div key={k as string} style={{ background: "#0b1222", borderRadius: 8, padding: 14, textAlign: "center" }}>
            <div style={{ fontSize: 22, fontWeight: 700 }}>{v}</div>
            <div className="muted">{k}</div>
          </div>
        ))}
      </div>
      <p className="muted">这些指标由 M2 的 metrics 模块在工作流结束时自动聚合（成功率 / 正确失败率 / 人工接管率等）。</p>
    </div>
  );
}
