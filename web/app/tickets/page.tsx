"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import Nav from "@/components/Nav";
import { createTicket, getConversation, getTicket, listTickets, runTicket, subscribeTicketWS } from "@/lib/api";

type Msg = { role: "user" | "assistant" | "system"; text: string; at?: string };

function safeParse(s: string): any {
  try { return JSON.parse(s); } catch { return {}; }
}

function eventToMsgs(e: any): Msg[] {
  const type = e.type || e.event_type;
  const at = e.at || e.created_at;
  const raw = e.payload ?? e.payload_json ?? {};
  const payload = typeof raw === "string" ? safeParse(raw) : raw;
  switch (type) {
    case "created":
      return [{ role: "system", text: "工单已创建", at }];
    case "model_call":
      return [{ role: "assistant", text: payload?.content || JSON.stringify(payload), at }];
    case "tool_call":
      return [{
        role: "assistant",
        text: `[工具调用] ${payload?.tool} ${JSON.stringify(payload?.params || {})} → ${JSON.stringify(payload?.result || "")}`,
        at,
      }];
    case "hitl":
      return [{ role: "system", text: `⏳ 等待审批：${payload?.tool} ${JSON.stringify(payload?.params || {})}`, at }];
    case "deliver":
      return [{ role: "assistant", text: `✅ ${payload?.summary || "工单已完成"}`, at }];
    case "state_transition":
      return [{ role: "system", text: `状态：${payload?.from} → ${payload?.to}`, at }];
    default:
      return [{ role: "system", text: `[${type}] ${JSON.stringify(payload)}`, at }];
  }
}

function buildMsgs(ticket: any, evts: any[], userText: string): Msg[] {
  const list: Msg[] = [{ role: "user", text: userText, at: ticket.created_at }];
  evts.forEach((e) => list.push(...eventToMsgs(e)));
  return list;
}

export default function Tickets() {
  const [tickets, setTickets] = useState<any[]>([]);
  const [form, setForm] = useState({ title: "", description: "", risk_level: "low", intent_type: "knowledge" });
  const [active, setActive] = useState<number | null>(null);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [inputEnabled, setInputEnabled] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const chatRef = useRef<HTMLDivElement>(null);
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

  async function startNew() {
    stopStreamRef.current?.();
    if (stopPollRef.current !== null) { window.clearInterval(stopPollRef.current); stopPollRef.current = null; }
    setMsg("");
    setActive(null);
    setMsgs([]);
    setForm({ title: "", description: "", risk_level: "low", intent_type: "knowledge" });
    setInputEnabled(true);
  }

  async function view(id: number) {
    stopStreamRef.current?.();
    if (stopPollRef.current !== null) { window.clearInterval(stopPollRef.current); stopPollRef.current = null; }
    setMsg(""); setBusy(true);
    try {
      const ticket = await getTicket(id);
      const evts = await getConversation(id);
      const list: Msg[] = [
        { role: "user", text: `${ticket.title}\n\n${ticket.description}`, at: ticket.created_at },
      ];
      evts.forEach((e) => list.push(...eventToMsgs(e)));
      setMsgs(list);
      setActive(id);
      setInputEnabled(false);
    } catch (ex: any) { setMsg(ex.message); }
    finally { setBusy(false); }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault(); setMsg(""); setBusy(true);
    let tid: number | null = null;
    const userText = `${form.title}\n\n${form.description}`;
    try {
      const t = await createTicket(form);
      tid = t.id;
      setActive(t.id);
      setInputEnabled(false);
      setMsgs([
        { role: "user", text: userText, at: t.created_at },
        { role: "system", text: "工单已创建", at: new Date().toISOString() },
      ]);
      await refresh();

      // 实时流（WebSocket，尽力而为；依赖 nginx 已开启 WS 升级）
      const unsub = subscribeTicketWS(t.id, (ev) => {
        setMsgs((prev) => [...prev, ...eventToMsgs(ev)]);
      });
      stopStreamRef.current = unsub;

      // 兜底实时刷新：轮询完整对话，直到 run 结束。保证无论 WS 是否可达都能“一点一点”输出。
      stopPollRef.current = window.setInterval(async () => {
        try {
          const evts = await getConversation(t.id);
          setMsgs(buildMsgs(t, evts, userText));
        } catch { /* ignore */ }
      }, 1000);

      // 后台触发 Agent 工作流，不阻塞 UI。run 完成后其 HTTP 响应返回，
      // 此时工作流最后一个事件（最终答复/收敛/交付）已落库，再做一次收敛同步，
      // 保证结尾内容不用刷新也能出现；随后再清理轮询与 WS，避免漏掉收尾批次。
      runTicket(t.id)
        .then(() => getConversation(t.id))
        .then((evts) => setMsgs(buildMsgs(t, evts, userText)))
        .catch((ex: any) => setMsg(ex.message))
        .finally(() => {
          if (stopPollRef.current !== null) { window.clearInterval(stopPollRef.current); stopPollRef.current = null; }
          stopStreamRef.current?.();
          stopStreamRef.current = null;
          setBusy(false);
        });
    } catch (ex: any) { setMsg(ex.message); setBusy(false); }
  }

  const riskBadge = (r: string) => <span className={`badge ${r}`}>{r}</span>;
  const statusBadge = (s: string) => <span className={`badge ${s}`}>{s}</span>;

  return (
    <div className="container">
      <Nav />
      <h2>工单台</h2>
      {msg && <div className="card muted">{msg}</div>}
      <div className="ticket-layout">
        <div className="card ticket-list-panel">
          <div className="row" style={{ justifyContent: "space-between", marginBottom: 12 }}>
            <h3 style={{ margin: 0 }}>工单列表</h3>
            <button onClick={startNew}>新建工单</button>
          </div>
          <table>
            <thead><tr><th>#</th><th>标题</th><th>风险</th><th>意图</th><th>状态</th><th>操作</th></tr></thead>
            <tbody>
              {tickets.map((t) => (
                <tr key={t.id} className={active === t.id ? "active" : ""}>
                  <td>{t.id}</td><td>{t.title}</td>
                  <td>{riskBadge(t.risk_level)}</td><td>{t.intent_type}</td>
                  <td>{statusBadge(t.status)}</td>
                  <td>
                    <button className="secondary" onClick={() => view(t.id)} disabled={busy}>查看</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {tickets.length === 0 && <p className="muted" style={{ marginTop: 12 }}>暂无工单，点击右上角「新建工单」发起。</p>}
        </div>

        <div className="card chat-panel">
          <h3>工单内容</h3>
          {active === null && !inputEnabled ? (
            <div className="chat-empty">
              <p className="muted">请从列表选择一条工单「查看」，或点击「新建工单」开始新的对话。</p>
            </div>
          ) : (
            <>
              <div className="chat-area" ref={chatRef}>
                {msgs.map((m, i) => (
                  <div key={i} className={`chat-msg ${m.role}`}>
                    <div className="chat-bubble">{m.text.split("\n").map((line, j) => <div key={j}>{line}</div>)}</div>
                    {m.at && <div className="chat-at muted">{m.at}</div>}
                  </div>
                ))}
                {active !== null && msgs.length === 0 && <p className="muted">该工单暂无对话记录。</p>}
              </div>
              <form onSubmit={submit} className="chat-form">
                <input
                  placeholder="工单标题"
                  value={form.title}
                  onChange={(e) => setForm({ ...form, title: e.target.value })}
                  disabled={!inputEnabled}
                  required
                />
                <textarea
                  placeholder="工单内容"
                  rows={3}
                  value={form.description}
                  onChange={(e) => setForm({ ...form, description: e.target.value })}
                  disabled={!inputEnabled}
                  required
                />
                <div className="row">
                  {inputEnabled && (
                    <>
                      <select value={form.risk_level} onChange={(e) => setForm({ ...form, risk_level: e.target.value })}>
                        <option value="low">低风险</option><option value="medium">中风险</option><option value="high">高风险</option>
                      </select>
                      <select value={form.intent_type} onChange={(e) => setForm({ ...form, intent_type: e.target.value })}>
                        <option value="knowledge">知识</option><option value="data_query">数据</option>
                        <option value="change">变更</option><option value="troubleshoot">故障</option>
                      </select>
                      <button type="submit" disabled={busy}>发送</button>
                    </>
                  )}
                  {!inputEnabled && <span className="muted">正在查看历史工单，输入已禁用；「新建工单」后可发起新的对话。</span>}
                </div>
              </form>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
