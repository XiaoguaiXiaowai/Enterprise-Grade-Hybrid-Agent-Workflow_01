"use client";
import { useCallback, useEffect, useState } from "react";
import Nav from "@/components/Nav";
import { listAllTickets, getApprovals, approve, getToken } from "@/lib/api";
import { useRouter } from "next/navigation";

const APPROVAL_STATUS_LABEL: Record<string, string> = {
  pending: "待处理", approved: "已通过", rejected: "已驳回",
};
const TOOL_LABEL: Record<string, string> = {
  search_kb: "检索企业知识库",
  search_kb_direct: "检索企业知识库",
  query_user_dir: "查询用户信息与权限",
  grant_db_readonly: "开通数据库只读权限",
  revoke_db_readonly: "回收数据库只读权限",
};

export default function Approve() {
  const router = useRouter();
  const [pending, setPending] = useState<any[]>([]);
  const [msg, setMsg] = useState("");

  const refresh = useCallback(async () => {
    try {
      const tickets = await listAllTickets();
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
      const ok = r?.decision === "approved" || r?.status === "rejected";
      setMsg(ok
        ? `已${decision === "approve" ? "通过" : "驳回"}工单 #${a.ticket_id} 的审批，Agent 已${decision === "approve" ? "恢复执行" : "收到驳回并调整"}。`
        : `审批结果：${JSON.stringify(r)}`);
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
                <div className="muted">待执行操作: <code>{TOOL_LABEL[a.tool_name] || a.tool_name}</code> · 参数: <code>{a.params_json}</code></div>
                <div className="muted">申请人: {a.requested_by} · 状态: {APPROVAL_STATUS_LABEL[a.status] || a.status}</div>
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
