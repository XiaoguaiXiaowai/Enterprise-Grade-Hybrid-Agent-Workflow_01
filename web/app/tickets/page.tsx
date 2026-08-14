"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Nav from "@/components/Nav";
import { createTicket, getConversation, getTicket, listTickets, runTicket, subscribeTicketWS } from "@/lib/api";

type Msg = {
  role: "user" | "assistant" | "system";
  type?: string;
  text: string;
  payload?: any;
  at?: string;
};

const STATUS_LABEL: Record<string, string> = {
  created: "已创建", triaged: "已分诊", gathering: "信息收集中", agent_running: "处理中",
  running: "处理中", awaiting_approval: "待审批", done: "已完成", failed: "失败",
  cancelled: "已取消", archived: "已归档",
};
const INTENT_LABEL: Record<string, string> = {
  knowledge: "知识", data_query: "数据", change: "变更", troubleshoot: "故障",
};
const IN_PROGRESS = ["created", "triaged", "gathering", "agent_running", "running"];

// 解析为北京时间并显示。后端以 UTC 存储（形如 "2026-08-12 09:12:45"，无时区后缀），
// 这里统一按 UTC 解析再换算成 Asia/Shanghai（北京时间）展示。
const BEIJING_TZ = "Asia/Shanghai";

function toBeijingMs(iso?: string): number | null {
  if (!iso) return null;
  // 数据库里是 "YYYY-MM-DD HH:MM:SS" 无时区 → 视为 UTC
  let s = iso.trim();
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(s)) s = s.replace(" ", "T") + "Z";
  const t = new Date(s).getTime();
  return Number.isNaN(t) ? null : t;
}

function fmtTime(iso?: string): string {
  const t = toBeijingMs(iso);
  if (t === null) return "";
  const d = Date.now() - t;
  if (d >= 0 && d < 60_000) return "刚刚";
  if (d >= 0 && d < 3_600_000) return `${Math.floor(d / 60_000)} 分钟前`;
  if (d >= 0 && d < 86_400_000) return `${Math.floor(d / 3_600_000)} 小时前`;
  return new Date(t).toLocaleString("zh-CN", { timeZone: BEIJING_TZ, month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function safeParse(s: string): any {
  try { return JSON.parse(s); } catch { return {}; }
}

function dump(v: any): string {
  try { return typeof v === "string" ? v : JSON.stringify(v, null, 2); } catch { return String(v); }
}

function eventToMsg(e: any): Msg[] {
  const type = e.type || e.event_type;
  const at = e.at || e.created_at;
  const raw = e.payload ?? e.payload_json ?? {};
  const payload = typeof raw === "string" ? safeParse(raw) : raw;
  switch (type) {
    case "created":
      return [{ role: "system", type, text: "工单已创建", at }];
    case "model_call":
      return [{ role: "assistant", type, text: payload?.content || dump(payload), payload, at }];
    case "tool_call":
      return [{ role: "assistant", type, text: "[工具调用] " + (payload?.tool || ""), payload, at }];
    case "hitl":
      return [{ role: "system", type, text: `等待审批：${payload?.tool || ""}`, payload, at }];
    case "deliver":
      return [{ role: "assistant", type, text: payload?.summary || "工单已完成", payload, at }];
    case "state_transition":
      return [{ role: "system", type, text: `状态：${payload?.from} → ${payload?.to}`, at }];
    default:
      return [{ role: "system", type, text: `[${type}] ${dump(payload)}`, at }];
  }
}

function buildMsgs(msgs: Msg[], evts: any[]): Msg[] {
  evts.forEach((e) => msgs.push(...eventToMsg(e)));
  return msgs;
}

function MsgView({ m }: { m: Msg }) {
  if (m.type === "tool_call") {
    const p = m.payload || {};
    const params = dump(p.params);
    const result = p.result !== undefined ? dump(p.result) : "";
    return (
      <details className="tool-card">
        <summary><span>🔧</span><strong>{p.tool || "工具调用"}</strong><span className="muted">点击展开参数与结果</span></summary>
        <div className="tc-body">
          <div className="tc-kv"><div className="tc-label">参数</div><pre>{params}</pre></div>
          {result !== "" && <div className="tc-kv"><div className="tc-label">结果</div><pre>{result}</pre></div>}
        </div>
      </details>
    );
  }
  if (m.type === "hitl") {
    const p = m.payload || {};
    return (
      <div className="hitl-card">
        <div>⏳ 等待审批 · {p.tool || "工具"}</div>
        <div className="muted">参数：{dump(p.params)}</div>
      </div>
    );
  }
  if (m.type === "deliver") {
    return <div className="deliver-card">✅ {m.text}</div>;
  }
  return (
    <div className={`chat-msg ${m.role}`}>
      <div className="chat-bubble">{m.text.split("\n").map((line, j) => <div key={j}>{line}</div>)}</div>
      {m.at && <div className="chat-at muted">{fmtTime(m.at)}</div>}
    </div>
  );
}

export default function Tickets() {
  const [tickets, setTickets] = useState<any[]>([]);
  const [form, setForm] = useState({ title: "", description: "" });
  const [modalOpen, setModalOpen] = useState(false);
  const [formErr, setFormErr] = useState("");
  const [active, setActive] = useState<number | null>(null);
  const [activeTicket, setActiveTicket] = useState<any>(null);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [filter, setFilter] = useState("all");
  const [q, setQ] = useState("");
  const chatRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const stopStreamRef = useRef<(() => void) | null>(null);
  const stopPollRef = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try { setTickets(await listTickets()); } catch { /* */ }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  useEffect(() => {
    const el = chatRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [msgs]);

  useEffect(() => {
    const el = listRef.current?.querySelector(`[data-id="${active}"]`);
    if (el) el.scrollIntoView({ block: "nearest" });
  }, [active, filter, q]);

  useEffect(() => () => {
    if (stopPollRef.current !== null) window.clearInterval(stopPollRef.current);
    stopStreamRef.current?.();
  }, []);

  function openNew() { setFormErr(""); setModalOpen(true); }

  function closeNew() {
    stopStreamRef.current?.();
    if (stopPollRef.current !== null) { window.clearInterval(stopPollRef.current); stopPollRef.current = null; }
    setModalOpen(false);
    setMsg("");
    setFormErr("");
  }

  function stopLive() {
    if (stopPollRef.current !== null) { window.clearInterval(stopPollRef.current); stopPollRef.current = null; }
    stopStreamRef.current?.();
    stopStreamRef.current = null;
  }

  async function view(id: number) {
    stopLive();
    setMsg(""); setBusy(true);
    try {
      const ticket = await getTicket(id);
      const evts = await getConversation(id);
      setMsgs(buildMsgs(
        [{ role: "user", text: `${ticket.title}\n\n${ticket.description}`, at: ticket.created_at }],
        evts,
      ));
      setActive(id);
      setActiveTicket(ticket);
    } catch (ex: any) { setMsg(ex.message); }
    finally { setBusy(false); }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault(); setMsg(""); setFormErr(""); setBusy(true);
    const userText = `${form.title}\n\n${form.description}`;
    try {
      const t = await createTicket(form);
      setFormErr("");
      setModalOpen(false);
      setActive(t.id);
      setActiveTicket(t);
      setMsgs([
        { role: "user", text: userText, at: t.created_at },
        { role: "system", type: "created", text: "工单已创建", at: new Date().toISOString() },
      ]);
      await refresh();

      // 实时流（WebSocket，尽力而为；依赖 nginx 已开启 WS 升级）
      stopStreamRef.current = subscribeTicketWS(t.id, (ev) => {
        setMsgs((prev) => [...prev, ...eventToMsg(ev)]);
      });

      // 兜底实时刷新：轮询完整对话，直到 run 结束。
      stopPollRef.current = window.setInterval(async () => {
        try {
          const evts = await getConversation(t.id);
          setMsgs(buildMsgs([{ role: "user", text: userText, at: t.created_at }], evts));
        } catch { /* ignore */ }
      }, 1000);

      // 后台触发 Agent 工作流，不阻塞 UI；结束后收敛同步并刷新列表状态。
      runTicket(t.id)
        .then(async () => {
          const evts = await getConversation(t.id);
          setMsgs(buildMsgs([{ role: "user", text: userText, at: t.created_at }], evts));
          const t2 = await getTicket(t.id).catch(() => null);
          if (t2) setActiveTicket(t2);
          setTickets(await listTickets());
        })
        .catch((ex: any) => setMsg(ex.message))
        .finally(() => { stopLive(); setBusy(false); });
    } catch (ex: any) {
      // 建单前的相关性校验失败（不相关）→ 在对话框内展示原因，不关闭弹窗、不建单
      setFormErr(ex.message || "创建失败");
      setBusy(false);
    }
  }

  const filtered = useMemo(() => {
    return tickets.filter((t) => {
      if (filter === "pending") return t.status === "awaiting_approval";
      if (filter === "running") return IN_PROGRESS.includes(t.status);
      if (filter === "done") return t.status === "done";
      if (filter === "failed") return t.status === "failed";
      return true;
    }).filter((t) => {
      if (!q) return true;
      const s = q.toLowerCase();
      return String(t.id).includes(s) || (t.title || "").toLowerCase().includes(s);
    });
  }, [tickets, filter, q]);

  const statusClass = (s: string) => {
    if (s === "done") return "done";
    if (s === "failed") return "failed";
    if (s === "awaiting_approval") return "awaiting_approval";
    return "processing";
  };

  const tab = (key: string, label: string) => (
    <button type="button" className={filter === key ? "on" : ""} onClick={() => setFilter(key)}>{label}</button>
  );

  return (
    <div className="container">
      <Nav />
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>工单台</h2>
        <button onClick={openNew}>新建工单</button>
      </div>
      {msg && <div className="card muted">{msg}</div>}

      <div className="ticket-layout">
        {/* 左栏：搜索 + 筛选 + 卡片列表 */}
        <div className="card ticket-list-panel">
          <input className="ticket-search" placeholder="搜索工单 ID / 标题…" value={q} onChange={(e) => setQ(e.target.value)} />
          <div className="filter-tabs">
            {tab("all", "全部")}
            {tab("pending", "待审批")}
            {tab("running", "处理中")}
            {tab("done", "已完成")}
            {tab("failed", "失败")}
          </div>
          <div className="ticket-list" ref={listRef}>
            {filtered.map((t) => (
              <div key={t.id} data-id={t.id} className={`ticket-card ${active === t.id ? "active" : ""}`} onClick={() => view(t.id)}>
                <div className="tc-title">#{t.id} {t.title}</div>
                <div className="tc-meta">
                  <span className={`badge ${statusClass(t.status)}`}>{STATUS_LABEL[t.status] || t.status}</span>
                  <span className="muted">{INTENT_LABEL[t.intent_type] || t.intent_type} · {fmtTime(t.updated_at || t.created_at)}</span>
                </div>
              </div>
            ))}
            {filtered.length === 0 && <p className="muted" style={{ margin: "8px 0", textAlign: "center" }}>暂无匹配工单。</p>}
          </div>
        </div>

        {/* 右栏：详情头 + 对话 + 底部状态条 */}
        <div className="card chat-panel">
          {active === null ? (
            <div className="chat-empty">
              <p className="muted">请从左侧选择一条工单，或点击右上角「新建工单」。</p>
            </div>
          ) : (
            <>
              <div className="detail-head">
                <h3 className="dh-title">#{activeTicket?.id ?? active} {activeTicket?.title || ""}</h3>
                <div className="dh-meta">
                  <span className={`badge ${activeTicket ? statusClass(activeTicket.status) : "processing"}`}>
                    {activeTicket ? STATUS_LABEL[activeTicket.status] || activeTicket.status : ""}
                  </span>
                  {activeTicket && <span className={`badge ${activeTicket.risk_level || "low"}`}>{activeTicket.risk_level || "low"}</span>}
                  {activeTicket && <span>{INTENT_LABEL[activeTicket.intent_type] || activeTicket.intent_type}</span>}
                  <span>创建于 {fmtTime(activeTicket?.created_at)}</span>
                  {busy && <span className="muted">加载中…</span>}
                </div>
              </div>
              <div className="chat-area" ref={chatRef}>
                {msgs.map((m, i) => <MsgView key={i} m={m} />)}
                {msgs.length === 0 && <p className="muted">该工单暂无对话记录。</p>}
                {busy && (
                  <div className="thinking-row">
                    <span className="thinking-dot" /><span className="thinking-dot" /><span className="thinking-dot" />
                    <span className="thinking-text">大模型思考中</span>
                  </div>
                )}
              </div>
              <div className="chat-foot">
                <span>🔒</span>
                <span className="muted">该工单对话为只读；如需新的任务，请点击右上角「新建工单」。</span>
              </div>
            </>
          )}
        </div>
      </div>

      {/* 新建工单 Modal */}
      {modalOpen && (
        <div className="modal-overlay" onClick={closeNew}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>新建工单</h3>
            <form onSubmit={submit}>
              <div>
                <label>工单标题 *</label>
                <input
                  style={{ width: "100%" }}
                  placeholder="例如：开通数据库只读权限"
                  value={form.title}
                  onChange={(e) => setForm({ ...form, title: e.target.value })}
                  required
                />
              </div>
              <div>
                <label>工单内容 *</label>
                <textarea
                  style={{ width: "100%" }}
                  rows={4}
                  placeholder="描述你的诉求，Agent 将自动处理…"
                  value={form.description}
                  onChange={(e) => setForm({ ...form, description: e.target.value })}
                  required
                />
              </div>
              <p className="muted" style={{ marginTop: 4 }}>意图类型与风险等级将由后端 Agent 自动判定。</p>
              {formErr && <div style={{ color: "var(--err)", marginTop: 8 }}>{formErr}</div>}
              <div className="m-actions">
                <button type="button" className="secondary" onClick={closeNew}>取消</button>
                <button type="submit" disabled={busy}>{busy ? "校验中…" : "创建并运行"}</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
