"use client";
import { useCallback, useEffect, useState } from "react";
import Nav from "@/components/Nav";
import { createTicket, listTickets, runTicket, subscribeEvents } from "@/lib/api";

export default function Tickets() {
  const [tickets, setTickets] = useState<any[]>([]);
  const [form, setForm] = useState({ title: "", description: "", risk_level: "low", intent_type: "knowledge" });
  const [active, setActive] = useState<number | null>(null);
  const [events, setEvents] = useState<any[]>([]);
  const [msg, setMsg] = useState("");

  const refresh = useCallback(async () => {
    try { setTickets(await listTickets()); } catch { /* */ }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  async function add(e: React.FormEvent) {
    e.preventDefault(); setMsg("");
    try { await createTicket(form); setForm({ title: "", description: "", risk_level: "low", intent_type: "knowledge" }); await refresh(); } catch (ex: any) { setMsg(ex.message); }
  }

  async function run(id: number) {
    setActive(id); setEvents([]); setMsg("");
    try {
      const unsub = subscribeEvents(id, (ev) => setEvents((p) => [...p, ev]));
      const r = await runTicket(id);
      setMsg(JSON.stringify(r));
      setTimeout(unsub, 4000);
    } catch (ex: any) { setMsg(ex.message); }
  }

  const riskBadge = (r: string) => <span className={`badge ${r}`}>{r}</span>;
  const statusBadge = (s: string) => <span className={`badge ${s}`}>{s}</span>;

  return (
    <div className="container">
      <Nav />
      <h2>工单台</h2>
      {msg && <div className="card muted">{msg}</div>}
      <div className="card">
        <h3>新建工单</h3>
        <form onSubmit={add} style={{ display: "grid", gap: 10 }}>
          <input placeholder="标题" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} required />
          <textarea placeholder="描述" rows={3} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} required />
          <div className="row">
            <select value={form.risk_level} onChange={(e) => setForm({ ...form, risk_level: e.target.value })}>
              <option value="low">低风险</option><option value="medium">中风险</option><option value="high">高风险</option>
            </select>
            <select value={form.intent_type} onChange={(e) => setForm({ ...form, intent_type: e.target.value })}>
              <option value="knowledge">知识</option><option value="data_query">数据</option>
              <option value="change">变更</option><option value="troubleshoot">故障</option>
            </select>
            <button type="submit">创建</button>
          </div>
        </form>
      </div>
      <div className="card">
        <h3>工单列表</h3>
        <table>
          <thead><tr><th>#</th><th>标题</th><th>风险</th><th>意图</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>
            {tickets.map((t) => (
              <tr key={t.id}>
                <td>{t.id}</td><td>{t.title}</td>
                <td>{riskBadge(t.risk_level)}</td><td>{t.intent_type}</td>
                <td>{statusBadge(t.status)}</td>
                <td>
                  <button className="secondary" onClick={() => run(t.id)} disabled={active === t.id}>运行</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {active && (
        <div className="card">
          <h3>工单 #{active} 事件流（实时 SSE）</h3>
          <div style={{ maxHeight: 200, overflow: "auto", fontFamily: "monospace", fontSize: 12 }}>
            {events.map((e, i) => <div key={i} className="muted">[{e.at}] {e.type}: {JSON.stringify(e.payload)}</div>)}
          </div>
        </div>
      )}
    </div>
  );
}
