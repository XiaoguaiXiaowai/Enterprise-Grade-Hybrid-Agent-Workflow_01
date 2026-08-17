"use client";
import { useCallback, useEffect, useState } from "react";
import Nav from "@/components/Nav";
import { getMetrics } from "@/lib/api";

type Daily = {
  day: string;
  tickets: number;
  success_rate: number;
  correct_failure_rate: number;
  avg_latency_ms: number;
  total_cost: number;
  human_takeover_rate: number;
};
type MetricsResp = {
  range: { min: string | null; max: string | null };
  summary: Daily & { start?: string | null; end?: string | null };
  daily: Daily[];
};

const pad = (n: number) => `${n}`.padStart(2, "0");
const toStr = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const todayStr = () => toStr(new Date());
const monthStartStr = () => {
  const d = new Date();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-01`;
};
const daysAgoStr = (n: number) => {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return toStr(d);
};
const yearStartStr = () => `${new Date().getFullYear()}-01-01`;

const PRESETS: { key: string; label: string; apply: () => { start: string; end: string; all: boolean } }[] = [
  { key: "month", label: "本月", apply: () => ({ start: monthStartStr(), end: todayStr(), all: false }) },
  { key: "7d", label: "近7天", apply: () => ({ start: daysAgoStr(6), end: todayStr(), all: false }) },
  { key: "30d", label: "近30天", apply: () => ({ start: daysAgoStr(29), end: todayStr(), all: false }) },
  { key: "year", label: "今年", apply: () => ({ start: yearStartStr(), end: todayStr(), all: false }) },
  { key: "all", label: "全部历史", apply: () => ({ start: "", end: "", all: true }) },
];

const pct = (v: number) => `${(v * 100).toFixed(1)}%`;

export default function Metrics() {
  const [data, setData] = useState<MetricsResp | null>(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);
  // 默认展示当月（1 日 → 今天）
  const [preset, setPreset] = useState("month");
  const [start, setStart] = useState(monthStartStr());
  const [end, setEnd] = useState(todayStr());
  const [allHistory, setAllHistory] = useState(false);

  const load = useCallback(async (q: { start?: string; end?: string; all?: boolean }) => {
    setLoading(true);
    setErr("");
    try {
      setData(await getMetrics(q));
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load({ start: monthStartStr(), end: todayStr() });
  }, [load]);

  const applyPreset = (p: (typeof PRESETS)[number]) => {
    const q = p.apply();
    setPreset(p.key);
    setAllHistory(q.all);
    setStart(q.start);
    setEnd(q.end);
    load(q.all ? { all: true } : { start: q.start, end: q.end });
  };

  const applyCustom = () => {
    setPreset("");
    setAllHistory(false);
    if (!start || !end) { setErr("请选择开始与结束日期"); return; }
    if (start > end) { setErr("开始日期不能晚于结束日期"); return; }
    load({ start, end });
  };

  const summary = data?.summary;
  const cards = summary ? [
    ["工单总数", summary.tickets],
    ["成功率", pct(summary.success_rate)],
    ["正确失败率", pct(summary.correct_failure_rate)],
    ["平均延迟(ms)", summary.avg_latency_ms],
    ["总成本", summary.total_cost],
    ["人工接管率", pct(summary.human_takeover_rate)],
  ] : [];

  const rangeLabel = allHistory
    ? "全部历史"
    : `${start} ~ ${end}`;
  const dataSpan = data && data.range.min && data.range.max
    ? `表内数据范围: ${data.range.min} ~ ${data.range.max}`
    : "";

  return (
    <div className="container">
      <Nav />
      <h2>系统监控评估</h2>

      {/* 时间筛选：快捷区间 + 自定义日期范围 */}
      <div className="card" style={{ padding: 14 }}>
        <div className="row" style={{ marginBottom: 10 }}>
          {PRESETS.map((p) => (
            <button
              key={p.key}
              className={preset === p.key ? "" : "secondary"}
              style={{ padding: "5px 14px", fontSize: 13 }}
              onClick={() => applyPreset(p)}
            >
              {p.label}
            </button>
          ))}
        </div>
        <div className="row">
          <span className="muted">自定义区间</span>
          <input type="date" value={start} onChange={(e) => setStart(e.target.value)} />
          <span className="muted">至</span>
          <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
          <button className="secondary" style={{ padding: "5px 14px", fontSize: 13 }} onClick={applyCustom}>
            查询
          </button>
          <span className="muted" style={{ marginLeft: "auto" }}>{rangeLabel}</span>
        </div>
      </div>

      {err && <div className="card muted">加载失败: {err}</div>}
      {loading && !data && <div className="card muted">加载中…</div>}

      {/* 汇总卡片 */}
      <div className="card" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(160px,1fr))", gap: 12 }}>
        {cards.map(([k, v]) => (
          <div key={k as string} style={{ background: "#0b1222", borderRadius: 8, padding: 14, textAlign: "center" }}>
            <div style={{ fontSize: 22, fontWeight: 700 }}>{v}</div>
            <div className="muted">{k}</div>
          </div>
        ))}
      </div>

      {/* 趋势图：工单数（柱）+ 成功率（线） */}
      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
          <strong>每日趋势</strong>
          <span className="muted">
            <span style={{ color: "var(--accent)" }}>■</span> 工单数
            <span style={{ color: "var(--ok)", marginLeft: 12 }}>■</span> 成功率
          </span>
        </div>
        {data && data.daily.length > 0 ? (
          <TrendChart daily={data.daily} />
        ) : (
          <div className="muted" style={{ textAlign: "center", padding: "40px 0" }}>
            {data ? "该时间段暂无监控数据" : "…"}
          </div>
        )}
      </div>

      {/* 每日明细 */}
      <div className="card" style={{ padding: 0, overflow: "hidden" }}>
        <div style={{ padding: "14px 20px 6px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <strong>每日明细</strong>
          {dataSpan && <span className="muted">{dataSpan}</span>}
        </div>
        <div style={{ overflowX: "auto" }}>
          <table style={{ minWidth: 760 }}>
            <thead>
              <tr>
                <th>日期</th><th>工单数</th><th>成功率</th><th>正确失败率</th>
                <th>平均延迟(ms)</th><th>成本</th><th>人工接管率</th>
              </tr>
            </thead>
            <tbody>
              {(data?.daily || []).map((d) => (
                <tr key={d.day}>
                  <td>{d.day}</td>
                  <td>{d.tickets}</td>
                  <td>{pct(d.success_rate)}</td>
                  <td>{pct(d.correct_failure_rate)}</td>
                  <td>{d.avg_latency_ms}</td>
                  <td>{d.total_cost}</td>
                  <td>{pct(d.human_takeover_rate)}</td>
                </tr>
              ))}
              {data && data.daily.length === 0 && (
                <tr><td colSpan={7} className="muted" style={{ textAlign: "center", padding: 20 }}>无数据</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <p className="muted">这些指标由 M2 的 metrics 模块在工作流结束时自动聚合（成功率 / 正确失败率 / 人工接管率等），支持按时间段与全部历史筛选。</p>
    </div>
  );
}

/* ---- 纯 SVG 趋势图：柱 = 每日工单数（左轴），折线 = 成功率（右轴 0~100%） ---- */
function TrendChart({ daily }: { daily: Daily[] }) {
  const W = 920, H = 250, padL = 44, padR = 40, padT = 16, padB = 30;
  const iw = W - padL - padR, ih = H - padT - padB;
  const n = daily.length;
  const maxT = Math.max(1, ...daily.map((d) => d.tickets));
  const bw = iw / n;
  const x = (i: number) => padL + bw * i + bw / 2;
  const yT = (v: number) => padT + ih - (v / maxT) * ih;       // 工单数轴
  const yR = (v: number) => padT + ih - v * ih;                 // 成功率轴（0~1）

  const points = daily.map((d, i) => `${x(i).toFixed(1)},${yR(d.success_rate).toFixed(1)}`).join(" ");
  const labelEvery = Math.max(1, Math.ceil(n / 10));

  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto", display: "block" }}>
      {/* 成功率网格 + 右轴刻度 */}
      {[0, 0.25, 0.5, 0.75, 1].map((r) => (
        <g key={r}>
          <line x1={padL} x2={W - padR} y1={yR(r)} y2={yR(r)} stroke="#334155" strokeWidth={1} strokeDasharray={r === 0 ? "" : "3 4"} />
          <text x={W - padR + 6} y={yR(r) + 4} fontSize={10} fill="#94a3b8">{Math.round(r * 100)}%</text>
        </g>
      ))}
      {/* 工单数刻度（左轴） */}
      {[0, 0.5, 1].map((r) => (
        <text key={`t${r}`} x={padL - 6} y={yT(maxT * r) + 4} textAnchor="end" fontSize={10} fill="#38bdf8">
          {Math.round(maxT * r)}
        </text>
      ))}

      {/* 每日工单数柱 */}
      {daily.map((d, i) => (
        <g key={d.day}>
          <rect x={x(i) - bw * 0.32} y={yT(d.tickets)} width={bw * 0.64} height={Math.max(0, padT + ih - yT(d.tickets))}
            rx={2} fill="rgba(56,189,248,0.55)">
            <title>{`${d.day}: ${d.tickets} 个工单`}</title>
          </rect>
          {i % labelEvery === 0 && (
            <text x={x(i)} y={H - 10} textAnchor="middle" fontSize={10} fill="#94a3b8">{d.day.slice(5)}</text>
          )}
        </g>
      ))}

      {/* 成功率折线 */}
      <polyline points={points} fill="none" stroke="var(--ok)" strokeWidth={2} strokeLinejoin="round" />
      {daily.map((d, i) => (
        <circle key={`c${d.day}`} cx={x(i)} cy={yR(d.success_rate)} r={3} fill="var(--ok)">
          <title>{`${d.day}: 成功率 ${pct(d.success_rate)}`}</title>
        </circle>
      ))}
    </svg>
  );
}
