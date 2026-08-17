"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import Nav from "@/components/Nav";
import { listApprovals, approve, getToken } from "@/lib/api";
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

// 审批状态徽标样式（复用工单台配色）
const statusClass = (s: string) => {
  if (s === "approved") return "done";
  if (s === "rejected") return "failed";
  return "awaiting_approval";
};

const PAGE_SIZE = 10;

export default function Approve() {
  const router = useRouter();
  const [tab, setTab] = useState<"pending" | "history">("pending");
  const [page, setPage] = useState(1);
  const [pages, setPages] = useState(0);
  const [total, setTotal] = useState(0);
  const [items, setItems] = useState<any[]>([]);
  const [msg, setMsg] = useState("");
  const seqRef = useRef(0); // 请求序号：防止轮询/翻页竞态乱序

  const refresh = useCallback(async (targetTab: string, targetPage: number) => {
    const seq = ++seqRef.current;
    try {
      const r = await listApprovals({
        page: targetPage, page_size: PAGE_SIZE,
        status: targetTab === "history" ? "history" : "pending",
      });
      if (seq !== seqRef.current) return; // 已有更新的请求，丢弃过期响应
      setItems(r.items || []);
      setPages(r.pages || 0);
      setTotal(r.total || 0);
      // 当前页被删空（如审批被处理）时回退一页
      if (r.items.length === 0 && targetPage > 1) {
        setPage(targetPage - 1);
        refresh(targetTab, targetPage - 1);
        return;
      }
    } catch (ex: any) {
      if (seq === seqRef.current) setMsg(ex.message);
    }
  }, []);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    refresh(tab, page);
  }, [tab, page, refresh, router]);

  // 待审批 Tab：3 秒轮询保持最新（切到历史 Tab 时暂停）
  useEffect(() => {
    if (tab !== "pending") return;
    const iv = setInterval(() => refresh("pending", page), 3000);
    return () => clearInterval(iv);
  }, [tab, page, refresh]);

  const switchTab = (t: "pending" | "history") => {
    setTab(t);
    setPage(1);
  };

  async function decide(a: any, decision: string) {
    setMsg("");
    try {
      const r = await approve(a.ticket_id, a.id, decision, decision === "approve" ? "同意" : "驳回");
      const ok = r?.decision === "approved" || r?.status === "rejected";
      setMsg(ok
        ? `已${decision === "approve" ? "通过" : "驳回"}工单 #${a.ticket_id} 的审批，Agent 已${decision === "approve" ? "恢复执行" : "收到驳回并调整"}。`
        : `审批结果：${JSON.stringify(r)}`);
      await refresh("pending", page);
    } catch (ex: any) { setMsg(ex.message); }
  }

  const fmtTime = (s?: string) => (s ? String(s).replace("T", " ").slice(0, 19) : "—");

  return (
    <div className="container">
      <Nav />
      <h2>审批台（HITL）</h2>
      {msg && <div className="card muted">{msg}</div>}
      <div className="card">
        <div className="filter-tabs" style={{ marginBottom: 8 }}>
          <button type="button" className={tab === "pending" ? "on" : ""} onClick={() => switchTab("pending")}>
            待审批{tab === "pending" ? `（${total}）` : ""}
          </button>
          <button type="button" className={tab === "history" ? "on" : ""} onClick={() => switchTab("history")}>
            历史记录{tab === "history" ? `（${total}）` : ""}
          </button>
        </div>

        {items.length === 0 && (
          <p className="muted">
            {tab === "pending"
              ? "暂无待审批工单。可在工单台创建一张\u201C开通数据库只读权限\u201D的高风险工单来触发。"
              : "暂无历史审批记录。"}
          </p>
        )}

        {items.map((a) => (
          <div key={a.id} style={{ borderBottom: "1px solid var(--border)", padding: "10px 0" }}>
            <div className="row" style={{ justifyContent: "space-between", gap: 8 }}>
              <div>
                <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                  <strong>#{a.ticket_id} {a.ticket_title}</strong>
                  <span className={`badge ${statusClass(a.status)}`}>{APPROVAL_STATUS_LABEL[a.status] || a.status}</span>
                </div>
                <div className="muted">待执行操作: <code>{TOOL_LABEL[a.tool_name] || a.tool_name}</code> · 参数: <code>{a.params_json}</code></div>
                <div className="muted">
                  申请人: {a.requested_by} · 申请于 {fmtTime(a.created_at)}
                  {a.status !== "pending" && <> · 审批人: {a.decided_by || "—"} · 审批于 {fmtTime(a.decided_at)}</>}
                  {a.status !== "pending" && a.reason ? <> · 意见: {a.reason}</> : null}
                </div>
              </div>
              {a.status === "pending" && (
                <div className="row" style={{ flexShrink: 0 }}>
                  <button className="ok" onClick={() => decide(a, "approve")}>通过</button>
                  <button className="err" onClick={() => decide(a, "reject")}>驳回</button>
                </div>
              )}
            </div>
          </div>
        ))}

        {pages > 1 && (
          <div className="pager">
            <button type="button" className="pager-btn" disabled={page <= 1}
                    onClick={() => setPage(page - 1)}>上一页</button>
            <span className="pager-info">第 {page} / {pages} 页 · 共 {total} 条</span>
            <button type="button" className="pager-btn" disabled={page >= pages}
                    onClick={() => setPage(page + 1)}>下一页</button>
          </div>
        )}
      </div>
    </div>
  );
}
