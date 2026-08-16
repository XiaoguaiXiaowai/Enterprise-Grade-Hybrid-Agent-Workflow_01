"use client";
// 工单执行 Trace 回放：治理层审计时间线（模型调用 / 工具调用 / 状态变迁）。
// 数据来自 GET /api/traces/{ticket_id}（traces 表，全链路可回放）。
import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import Nav from "@/components/Nav";
import { getTraces } from "@/lib/api";

const SOURCE_LABEL: Record<string, string> = {
  assemble: "上下文装配",
  tool: "工具调用",
  agents_sdk: "Agent 编排",
  agents_sdk_final: "Agent 收敛",
};
const SOURCE_CLASS: Record<string, string> = {
  assemble: "trace-source-assemble",
  tool: "trace-source-tool",
  agents_sdk: "trace-source-sdk",
  agents_sdk_final: "trace-source-final",
};

function fmtTime(iso?: string): string {
  if (!iso) return "";
  let s = iso.trim();
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(s)) s = s.replace(" ", "T") + "Z";
  const t = new Date(s).getTime();
  if (Number.isNaN(t)) return "";
  return new Date(t).toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai", month: "numeric", day: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

function safeParse(s: string): any {
  try { return JSON.parse(s); } catch { return null; }
}

function TraceRow({ t }: { t: any }) {
  const [open, setOpen] = useState(false);
  const io = safeParse(t.io_json) || {};
  const usage = safeParse(t.token_usage_json) || {};
  const totalTokens = usage.total_tokens
    ?? ((usage.prompt_tokens || 0) + (usage.completion_tokens || 0));
  const source = SOURCE_LABEL[t.context_source] || t.context_source || "-";
  const cls = SOURCE_CLASS[t.context_source] || "trace-source-assemble";

  // io 展示文案：按 context_source 摘要
  const ioPreview = (() => {
    if (t.context_source === "tool") {
      return `工具 ${t.tool_name}：参数 ${JSON.stringify(io.params || {})} → 结果 ${JSON.stringify(io.result || {})}`;
    }
    if (t.context_source === "agents_sdk_final") {
      return String(io.final_output || "").slice(0, 200);
    }
    if (t.context_source === "agents_sdk") {
      const spans = io.spans || [];
      return `span 数 ${spans.length}，${String(io.final_output || io.workflow || "").slice(0, 120)}`;
    }
    return JSON.stringify(io).slice(0, 200);
  })();

  return (
    <div className="trace-row">
      <div className="trace-head" onClick={() => setOpen(!open)}>
        <span className={`badge ${cls}`}>{source}</span>
        {t.tool_name && <span className="trace-tool">🔧 {t.tool_name}</span>}
        {!t.tool_name && t.model && <span className="trace-tool">🤖 {t.model}</span>}
        <span className="muted trace-time">{fmtTime(t.created_at)}</span>
        <span className="muted">req {t.req_id}</span>
        {t.state_before && <span className="muted">状态 {t.state_before} → {t.state_after || "?"}</span>}
        {t.latency_ms ? <span className="muted">{t.latency_ms}ms</span> : null}
        {totalTokens ? <span className="muted">{totalTokens} tok</span> : null}
        <span className="trace-caret">{open ? "▾" : "▸"}</span>
      </div>
      {open && (
        <div className="trace-body">
          <div className="trace-meta">
            {t.model && <span>模型：{t.model}</span>}
            {t.provider && <span>提供方：{t.provider}</span>}
            <span>版本：{t.prompt_version || "-"}</span>
            {t.retry_count ? <span>重试：{t.retry_count}</span> : null}
          </div>
          <div className="trace-io">
            <div className="trace-io-preview">{ioPreview}</div>
            <pre className="trace-json">{JSON.stringify(io, null, 2)}</pre>
          </div>
        </div>
      )}
    </div>
  );
}

export default function Traces({ params }: { params: { id: string } }) {
  const ticketId = Number(params.id);
  const [traces, setTraces] = useState<any[] | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    getTraces(ticketId)
      .then((rows) => setTraces(rows || []))
      .catch((e) => setErr(String(e)));
  }, [ticketId]);

  const stats = useMemo(() => {
    if (!traces) return null;
    const latency = traces.reduce((s, t) => s + (t.latency_ms || 0), 0);
    const tokens = traces.reduce((s, t) => {
      const u = safeParse(t.token_usage_json) || {};
      return s + (u.total_tokens ?? ((u.prompt_tokens || 0) + (u.completion_tokens || 0)));
    }, 0);
    const models = [...new Set(traces.map((t) => t.model).filter(Boolean))];
    const tools = traces.filter((t) => t.tool_name).map((t) => t.tool_name);
    return { n: traces.length, latency, tokens, models, tools: [...new Set(tools)] };
  }, [traces]);

  return (
    <div className="container">
      <Nav />
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>Trace 回放 · 工单 #{ticketId}</h2>
        <Link href="/tickets" className="secondary" style={{ textDecoration: "none", padding: "6px 14px", borderRadius: 6, fontSize: 14 }}>← 返回工单台</Link>
      </div>
      {err && <div className="card muted">{err}</div>}
      {!traces && !err && <div className="card muted">加载中…</div>}
      {traces && (
        <>
          {stats && (
            <div className="card" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(140px,1fr))", gap: 10, marginBottom: 12 }}>
              <div className="trace-stat"><b>{stats.n}</b><span className="muted">调用次数</span></div>
              <div className="trace-stat"><b>{stats.latency}ms</b><span className="muted">总延迟</span></div>
              <div className="trace-stat"><b>{stats.tokens}</b><span className="muted">总 Token</span></div>
              <div className="trace-stat"><b>{stats.models.join(", ") || "-"}</b><span className="muted">模型</span></div>
              <div className="trace-stat"><b>{stats.tools.join(", ") || "无"}</b><span className="muted">工具</span></div>
            </div>
          )}
          <div className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
            <p className="muted" style={{ margin: 0 }}>治理层全链路审计：每次模型调用 / 工具调用 / 状态变迁一行，点击展开详情。</p>
          </div>
          {traces.length === 0
            ? <div className="card muted">该工单暂无 Trace 记录（尚未运行或数据已被清理）。</div>
            : traces.map((t, i) => (
              <div key={i} className="trace-item">
                <TraceRow t={t} />
              </div>
            ))}
        </>
      )}
    </div>
  );
}
