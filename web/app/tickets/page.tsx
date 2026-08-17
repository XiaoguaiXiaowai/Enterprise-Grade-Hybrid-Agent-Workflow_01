"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import Nav from "@/components/Nav";
import { createTicket, deleteTicket, getConversation, getTicket, getUser, listTicketsPaginated, runTicket, sendMessage, confirmTicket, cancelTicket, subscribeTicketWS } from "@/lib/api";

type Msg = {
  role: "user" | "assistant" | "system";
  type?: string;
  text: string;
  payload?: any;
  at?: string;
};

const STATUS_LABEL: Record<string, string> = {
  created: "已创建", triaged: "已分诊", gathering: "信息收集中", agent_running: "处理中",
  running: "处理中", awaiting_approval: "等待管理者确认", pending_confirm: "待确认完成",
  done: "已完成", failed: "失败",
  cancelled: "已取消", archived: "已关闭",
};
const INTENT_LABEL: Record<string, string> = {
  knowledge: "知识", data_query: "数据", change: "变更", troubleshoot: "故障",
};
const RISK_LABEL: Record<string, string> = { low: "低", medium: "中", high: "高" };
const TOOL_LABEL: Record<string, string> = {
  search_kb: "检索企业知识库",
  search_kb_direct: "检索企业知识库",
  query_user_dir: "查询用户信息与权限",
  grant_db_readonly: "开通数据库只读权限",
  revoke_db_readonly: "回收数据库只读权限",
};
// 可取消状态（agent_running 执行中不可取消，同步执行无法中断线程）
const CANCELABLE = ["created", "triaged", "gathering", "awaiting_approval", "pending_confirm", "failed"];

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

function eventToMsg(e: any): Msg[] {
  const type = e.type || e.event_type;
  const at = e.at || e.created_at;
  const raw = e.payload ?? e.payload_json ?? {};
  const payload = typeof raw === "string" ? safeParse(raw) : raw;
  switch (type) {
    case "created":
      return [{ role: "system", type, text: "工单已创建", at }];
    case "user_message":
      return [{ role: "user", type, text: payload?.content || "", at }];
    case "system_message":
      return [{ role: "system", type, text: payload?.content || "", at }];
    case "model_call":
      // 只展示最终结论（kind=final 为自然语言答复）；计划/JSON 类内容不输出
      if (payload?.kind === "final" && payload?.content) {
        return [{ role: "assistant", type, text: payload.content, payload, at }];
      }
      return [];
    case "tool_call":
      // 工具调用参数/结果（JSON）不输出
      return [];
    case "hitl":
      return [{
        role: "system", type,
        text: `等待管理者确认：${TOOL_LABEL[payload?.tool] || payload?.tool || "执行高风险操作"}`,
        at,
      }];
    case "deliver":
      return [{ role: "assistant", type, text: payload?.summary || "工单已完成", payload, at }];
    case "state_transition":
      return [{
        role: "system", type,
        text: `状态：${STATUS_LABEL[payload?.from] || payload?.from} → ${STATUS_LABEL[payload?.to] || payload?.to}`,
        at,
      }];
    case "closed":
      return [{ role: "system", type, text: "工单已关闭", at }];
    case "cancelled":
      return [{ role: "system", type, text: "工单已取消", at }];
    case "confirmed":
      return [{ role: "system", type, text: "客户已确认完成，工单已完成", at }];
    case "stage": {
      // 快速落单首轮流程提示：相关性→重写→分类（实时流式展示）
      const st = payload?.stage || "";
      const state = payload?.state || "";
      const label = payload?.label || "";
      if (state === "running") return [{ role: "system", type, text: `⏳ ${label}…`, at }];
      if (state === "fail") return [{ role: "system", type, text: `❌ ${label}`, at }];
      if (state === "ok") return [{ role: "system", type, text: `✅ ${label}`, at }];
      return [];
    }
    default:
      // log / rewrite / reject_followup 等内部事件不展示
      return [];
  }
}

function buildMsgs(msgs: Msg[], evts: any[]): Msg[] {
  evts.forEach((e) => msgs.push(...eventToMsg(e)));
  return msgs;
}

function MsgView({ m }: { m: Msg }) {
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
  const [total, setTotal] = useState(0);
  const [pages, setPages] = useState(0);
  const [page, setPage] = useState(1);
  const pageSize = 20;
  const [form, setForm] = useState({ title: "", description: "" });
  const [modalOpen, setModalOpen] = useState(false);
  const [formErr, setFormErr] = useState("");
  const [active, setActive] = useState<number | null>(null);
  const [activeTicket, setActiveTicket] = useState<any>(null);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [input, setInput] = useState("");
  const [filter, setFilter] = useState("all");
  const [q, setQ] = useState("");
  const chatRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const stopStreamRef = useRef<(() => void) | null>(null);
  const searchRef = useRef(""); // 已执行的搜索关键词（防抖去重）

  // 当前用户角色（登录时写入 localStorage）：admin 可删除工单；operator/admin 列表含全部工单
  const role = getUser()?.role ?? null;
  const isAdmin = role === "admin";
  const isOps = role === "operator" || role === "admin";

  // 需求②：筛选 Tab → 后端 status 集合映射
  const filterStatus = (f: string): string => {
    if (f === "pending") return "awaiting_approval";
    if (f === "running") return "created,triaged,gathering,agent_running,running";
    if (f === "done") return "done,archived";
    if (f === "failed") return "failed";
    return "";
  };

  // 需求②：分页加载（搜索/筛选/页码作为后端参数，联动刷新）
  const refresh = useCallback(async () => {
    try {
      const data = await listTicketsPaginated({
        page, page_size: pageSize, status: filterStatus(filter), q: q || undefined,
      });
      setTickets(data.items ?? []);
      setTotal(data.total ?? 0);
      setPages(data.pages ?? 0);
      if (page > 1 && (data.pages ?? 0) < page) setPage(Math.max(1, data.pages));
    } catch { /* */ }
  }, [filter, q, page]);
  // 始终引用最新 refresh（避免轮询 interval 闭包捕获旧参数，导致切页后被旧查询覆盖）
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;

  useEffect(() => { refresh(); }, [refresh]);

  // 需求②：搜索输入防抖 → 重置到第 1 页并刷新
  useEffect(() => {
    const t = window.setTimeout(() => {
      if (q === searchRef.current) return;
      searchRef.current = q;
      setPage(1);
    }, 300);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q]);

  // 活跃工单实时刷新：对话/状态每秒轮询（实时性所需）；列表仅在状态变化或 10s 兜底时刷新，
  // 避免每轮询都打 count_tickets + list_tickets 造成无谓的接口压力。
  useEffect(() => {
    if (active === null) return;
    let lastStatus: string | null = null;
    let tick = 0;
    const iv = window.setInterval(async () => {
      try {
        const [t2, evts] = await Promise.all([getTicket(active), getConversation(active)]);
        setActiveTicket(t2);
        setMsgs(buildMsgs(
          [{ role: "user", text: `${t2.title}\n\n${t2.description}`, at: t2.created_at }],
          evts,
        ));
        tick++;
        if (tick % 10 === 0 || (lastStatus !== null && lastStatus !== t2.status)) {
          refreshRef.current(); // 状态变化立即同步列表徽章；否则每 10 轮询兜底一次
        }
        lastStatus = t2.status;
      } catch { /* ignore */ }
    }, 1000);
    return () => window.clearInterval(iv);
  }, [active]);

  // 滚动跟随：仅当用户停留在底部附近时才自动滚动，避免查看历史时被拉回。
  // 注意 chat-area 是条件渲染的（未选工单时不存在），因此监听绑在 document
  // 的捕获阶段，运行时动态取 chatRef.current，避免绑定时机与挂载时机耦合。
  const stickToBottomRef = useRef(true);

  useEffect(() => {
    const onScroll = (e: Event) => {
      const el = chatRef.current;
      if (!el || e.target !== el) return;
      stickToBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    };
    document.addEventListener("scroll", onScroll, { capture: true, passive: true });
    return () => document.removeEventListener("scroll", onScroll, { capture: true });
  }, []);

  useEffect(() => {
    const el = chatRef.current;
    if (el && stickToBottomRef.current) el.scrollTop = el.scrollHeight;
  }, [msgs, busy]);

  useEffect(() => {
    const el = listRef.current?.querySelector(`[data-id="${active}"]`);
    if (el) el.scrollIntoView({ block: "nearest" });
  }, [active, filter, q]);

  useEffect(() => () => {
    stopStreamRef.current?.();
  }, []);

  function openNew() { setFormErr(""); setModalOpen(true); }

  function closeNew() {
    stopStreamRef.current?.();
    setModalOpen(false);
    setMsg("");
    setFormErr("");
  }

  function stopLive() {
    stopStreamRef.current?.();
    stopStreamRef.current = null;
  }

  async function view(id: number) {
    stopLive();
    setMsg(""); setBusy(true);
    stickToBottomRef.current = true; // 切换工单后回到底部
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
    stickToBottomRef.current = true; // 新建工单后回到底部
    const userText = `${form.title}\n\n${form.description}`;
    try {
      const t = await createTicket(form);
      setFormErr("");
      setModalOpen(false);
      setActive(t.id);
      setActiveTicket(t);
      setMsgs([{ role: "user", text: userText, at: t.created_at }]);
      await refresh();

      // 实时流（WebSocket，尽力而为；依赖 nginx 已开启 WS 升级）
      stopStreamRef.current = subscribeTicketWS(t.id, (ev) => {
        setMsgs((prev) => [...prev, ...eventToMsg(ev)]);
      });

      // 后台触发 Agent 工作流，不阻塞 UI；活跃工单轮询会自动同步对话与状态。
      runTicket(t.id)
        .then(async () => {
          const evts = await getConversation(t.id);
          setMsgs(buildMsgs([{ role: "user", text: userText, at: t.created_at }], evts));
          const t2 = await getTicket(t.id).catch(() => null);
          if (t2) setActiveTicket(t2);
          refresh();
        })
        .catch((ex: any) => setMsg(ex.message))
        .finally(() => { stopLive(); setBusy(false); });
    } catch (ex: any) {
      // 建单失败（字段校验等）→ 在对话框内展示原因，不关闭弹窗、不建单；
      // （相关性校验已移至 run 阶段，在聊天内流式展示，不再走此分支）
      setFormErr(ex.message || "创建失败");
      setBusy(false);
    }
  }

  // 多轮对话：追加提问 / 补充信息（待确认完成 / failed 状态可发）
  async function send() {
    const content = input.trim();
    if (!content || active === null) return;
    setMsg(""); setInput(""); setBusy(true);
    const tid = active;
    try {
      await sendMessage(tid, content);
    } catch (ex: any) {
      setMsg(ex.message);
      setInput(content); // 发送失败时保留输入内容，避免误丢
      const t2 = await getTicket(tid).catch(() => null);
      if (t2) setActiveTicket(t2);
    } finally {
      // 回合结束（含拒绝）→ 用最新对话重建
      try {
        const [t2, evts] = await Promise.all([getTicket(tid), getConversation(tid)]);
        setActiveTicket(t2);
        setMsgs(buildMsgs(
          [{ role: "user", text: `${t2.title}\n\n${t2.description}`, at: t2.created_at }],
          evts,
        ));
        refresh();
      } catch { /* ignore */ }
      setBusy(false);
    }
  }

  // 客户确认完成（pending_confirm → 已完成，工单走到最后）
  async function doConfirm() {
    if (active === null) return;
    setMsg(""); setBusy(true);
    try {
      const t = await confirmTicket(active);
      setActiveTicket(t);
      refresh();
    } catch (ex: any) { setMsg(ex.message); }
    finally { setBusy(false); }
  }

  // 取消工单（未完成状态；未决审批一并作废）
  async function doCancel() {
    if (active === null) return;
    if (!confirm(`确认取消工单 #${active}「${activeTicket?.title || ""}」？取消后未决审批将一并作废。`)) return;
    setMsg(""); setBusy(true);
    try {
      const t = await cancelTicket(active);
      setActiveTicket(t);
      refresh();
      setMsg(`已取消工单 #${active}`);
    } catch (ex: any) { setMsg(ex.message); }
    finally { setBusy(false); }
  }

  // 管理员删除工单（级联清除全部关联数据）
  async function doDelete(id: number) {
    const t = tickets.find((x) => x.id === id);
    if (!confirm(`确认删除工单 #${id}「${t?.title || ""}」？删除后不可恢复，将同时清除其对话、审批与执行记录。`)) return;
    setMsg(""); setBusy(true);
    try {
      await deleteTicket(id);
      setMsg(`已删除工单 #${id}`);
      stopLive();
      setActive(null); setActiveTicket(null); setMsgs([]);
      await refresh();
    } catch (ex: any) { setMsg(ex.message); }
    finally { setBusy(false); }
  }

  const status = activeTicket?.status;
  const canChat = active !== null && (status === "pending_confirm" || status === "failed") && !busy;
  // 大模型思考态：提交/运行请求进行中，或工单处于处理中集合
  // （created=首轮流水线阶段 / triaged / gathering / agent_running / running）。
  // 注意：切页后组件重挂载 busy 会重置，必须依赖工单状态判断，否则切回时思考态丢失。
  const thinking = busy || status === "created" || status === "triaged" || status === "gathering" || status === "agent_running" || status === "running";

  const inputPlaceholder = (() => {
    if (!active) return "";
    if (busy) return "正在处理中，请稍候…";
    if (status === "awaiting_approval") return "工单正在等待管理者确认，暂不能追加提问";
    if (status === "archived") return "工单已关闭，无法追加提问";
    if (status === "done") return "工单已完成，无法追加提问";
    if (status === "pending_confirm") return "本轮处理已完成，可确认完成或继续追问…";
    if (status === "failed") return "工单处理失败，可补充信息后重试…";
    return "工单正在处理中，请稍候…";
  })();

  const statusClass = (s: string) => {
    if (s === "done" || s === "archived") return "done";
    if (s === "failed") return "failed";
    if (s === "awaiting_approval" || s === "pending_confirm") return "awaiting_approval";
    return "processing";
  };

  const tab = (key: string, label: string) => (
    <button type="button" className={filter === key ? "on" : ""} onClick={() => { setFilter(key); setPage(1); }}>{label}</button>
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
            {tickets.map((t) => (
              <div key={t.id} data-id={t.id} className={`ticket-card ${active === t.id ? "active" : ""}`} onClick={() => view(t.id)}>
                <div className="tc-title">#{t.id} {t.title}</div>
                <div className="tc-meta">
                  <span className={`badge ${statusClass(t.status)}`}>{STATUS_LABEL[t.status] || t.status}</span>
                  <span className="muted">{INTENT_LABEL[t.intent_type] || t.intent_type} · {fmtTime(t.updated_at || t.created_at)}</span>
                  {isOps && <span className="muted">· {t.requester_name || `用户#${t.requester_id}`}</span>}
                </div>
                {isAdmin && (
                  <button
                    className="err"
                    style={{ position: "absolute", top: 10, right: 10, padding: "2px 10px", fontSize: 12 }}
                    onClick={(e) => { e.stopPropagation(); doDelete(t.id); }}
                    title="删除工单（仅管理员）"
                  >删除</button>
                )}
              </div>
            ))}
            {tickets.length === 0 && <p className="muted" style={{ margin: "8px 0", textAlign: "center" }}>暂无匹配工单。</p>}
          </div>
          {/* 需求②：分页 UI */}
          {pages > 1 && (
            <div className="pager">
              <button type="button" className="pager-btn" disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</button>
              <span className="pager-info">第 {page} / {pages} 页 · 共 {total} 条</span>
              <button type="button" className="pager-btn" disabled={page >= pages} onClick={() => setPage(page + 1)}>下一页</button>
            </div>
          )}
        </div>

        {/* 右栏：详情头 + 对话 + 底部输入区 */}
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
                  {activeTicket && <span className={`badge ${activeTicket.risk_level || "low"}`}>{RISK_LABEL[activeTicket.risk_level] || activeTicket.risk_level || "低"}</span>}
                  {activeTicket && <span>{INTENT_LABEL[activeTicket.intent_type] || activeTicket.intent_type}</span>}
                  <span>创建于 {fmtTime(activeTicket?.created_at)}</span>
                  <a href={`/traces/${active}`} className="trace-link" title="查看该工单全链路执行 Trace（治理审计）">🔍 Trace</a>
                  {activeTicket && CANCELABLE.includes(activeTicket.status) && (
                    <button className="secondary cancel-btn" onClick={doCancel} disabled={busy} title="取消工单（未决审批一并作废）">取消工单</button>
                  )}
                  {busy && <span className="muted">加载中…</span>}
                </div>
              </div>
              <div className="chat-area" ref={chatRef}>
                {msgs.map((m, i) => <MsgView key={i} m={m} />)}
                {msgs.length === 0 && <p className="muted">该工单暂无对话记录。</p>}
                {thinking && (
                  <div className="thinking-row">
                    <span className="thinking-dot" /><span className="thinking-dot" /><span className="thinking-dot" />
                    <span className="thinking-text">大模型思考中…</span>
                  </div>
                )}
              </div>
              <div className="chat-foot">
                {/* 处理中（发送追问/确认中）隐藏确认条，回合结束回到待确认再出现 */}
                {status === "pending_confirm" && !busy && (
                  <div className="close-bar">
                    <span>✅ 本轮处理已完成，是否确认完成工单？</span>
                    <div className="cb-actions">
                      <button className="ok" onClick={doConfirm} disabled={busy}>确认完成</button>
                      <button className="secondary" onClick={() => inputRef.current?.focus()} disabled={busy}>继续追问</button>
                    </div>
                  </div>
                )}
                <div className="chat-input-row">
                  <input
                    ref={inputRef}
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    placeholder={inputPlaceholder}
                    disabled={!canChat}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && canChat && input.trim()) send();
                    }}
                  />
                  <button onClick={send} disabled={!canChat || !input.trim()}>发送</button>
                </div>
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
                <button type="submit" disabled={busy}>{busy ? "创建中…" : "创建并运行"}</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
