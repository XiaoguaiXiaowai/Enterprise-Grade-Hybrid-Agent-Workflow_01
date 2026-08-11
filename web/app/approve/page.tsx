"use client";
import { useCallback, useEffect, useState } from "react";
import Nav from "@/components/Nav";
import { listTickets, getApprovals, approve, getToken } from "@/lib/api";
import { useRouter } from "next/navigation";

export default function Approve() {
  const router = useRouter();
  const [pending, setPending] = useState<any[]>([]);
  const [msg, setMsg] = useState("");

  const refresh = useCallback(async () => {
    try {
      const tickets = await listTickets();
      const list: any[] = [];
      for (const t of tickets) {
        if (t.status !== "awaiting_approval") continue;
        const ap = await getApprovals(t.id);
        ap.forEach((a) => list.push({ ...a, ticket_title: t.title, ticket_id: t.id }));
      }
      setPending(list);
    } catch { /* */ }
  }, []);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    refresh();
    const iv = setInterval(refresh, 3000);
    return () => clearInterval(iv);
  }, [refresh, router]);

  async function decide(a: any, decision: string) {
    setMsg("");
    try {
      const r = await approve(a.ticket_id, a.id, decision, decision === "approve" ? "同意" : "驳回");
      setMsg(JSON.stringify(r));
      await refresh();
    } catch (ex: any) { setMsg(ex.message); }
  }

  return (
    <div className="container">
      <Nav />
      <h2>审批台（HITL）</h2>
      {msg && <div className="card muted">{msg}</div>}
      <div className="card">
        {pending.length === 0 && <p className="muted">暂无待审批工单。可在工单台创建一张"开通数据库只读权限"的高风险工单来触发。</p>}
        {pending.map((a) => (
          <div key={a.id} style={{ borderBottom: "1px solid var(--border)", padding: "10px 0" }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <div>
                <strong>#{a.ticket_id} {a.ticket_title}</strong>
                <div className="muted">工具: <code>{a.tool_name}</code> · 参数: <code>{a.params_json}</code></div>
                <div className="muted">申请人: {a.requested_by} · 状态: {a.status}</div>
              </div>
              <div className="row">
                <button className="ok" onClick={() => decide(a, "approve")}>通过</button>
                <button className="err" onClick={() => decide(a, "reject")}>驳回</button>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
