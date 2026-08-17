// 前端 API 客户端：封装 fetch、token 持久化、SSE。
const API = process.env.NEXT_PUBLIC_API_URL || "";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem("token");
}
export function setToken(t: string) {
  window.localStorage.setItem("token", t);
}
// 当前登录用户信息（含 role），登录成功后写入；供导航/页面按角色渲染
export function setUser(u: any) {
  window.localStorage.setItem("user", JSON.stringify(u));
}
export function getUser(): any | null {
  if (typeof window === "undefined") return null;
  try { return JSON.parse(window.localStorage.getItem("user") || "null"); } catch { return null; }
}
export function clearToken() {
  window.localStorage.removeItem("token");
  window.localStorage.removeItem("user");
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { "X-Auth-Token": token } : {}),
      ...(init?.headers || {}),
    },
  });
  if (res.status === 401) {
    clearToken();
    if (typeof window !== "undefined") window.location.href = "/login";
    throw new Error("未登录");
  }
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(body || res.statusText);
  }
  return res.json() as Promise<T>;
}

export async function login(username: string, password: string) {
  // 注意：登录失败的后端响应是 401，但那是"凭据错误"语义，绝不能走通用 req() 的
  // 401 处理（清 token + 跳转 /login + 抛"未登录"），否则错误提示会被吞掉。
  const res = await fetch(`${API}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    // 解析 FastAPI 错误体 {"detail": "..."}（422 时 detail 为校验错误数组）
    const text = await res.text().catch(() => "");
    let detail = res.statusText;
    try {
      const d = JSON.parse(text)?.detail;
      if (typeof d === "string") detail = d;
      else if (Array.isArray(d) && d[0]?.msg) detail = d[0].msg;
    } catch { /* 保留 fallback */ }
    throw new Error(detail || `登录失败（HTTP ${res.status}）`);
  }
  const data = await res.json();
  setToken(data.token);
  setUser(data.user);
  return data.user;
}

// 演示播种信息（尽力而为，失败返回 null 静默降级）：仅非生产环境返回密码
export async function seedInfo(): Promise<{ seeded: boolean; password?: string | null; roles?: string[] } | null> {
  try {
    const res = await fetch(`${API}/api/auth/seed-info`);
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

export function me() {
  return req<any>("/api/auth/me");
}

export function createTicket(body: any) {
  return req<any>("/api/tickets", { method: "POST", body: JSON.stringify(body) });
}
export function listTickets() {
  return req<any[]>("/api/tickets");
}
// 需求②：分页列表。返回 {items, total, page, page_size, pages}
export function listTicketsPaginated(params: { page?: number; page_size?: number; status?: string; q?: string } = {}) {
  const sp = new URLSearchParams();
  if (params.page) sp.set("page", String(params.page));
  if (params.page_size) sp.set("page_size", String(params.page_size));
  if (params.status) sp.set("status", params.status);
  if (params.q) sp.set("q", params.q);
  const qs = sp.toString();
  return req<any>(`/api/tickets${qs ? `?${qs}` : ""}`);
}
export function listAllTickets() {
  return req<any[]>("/api/tickets/all");
}
export function getTicket(id: number) {
  return req<any>(`/api/tickets/${id}`);
}
export function deleteTicket(id: number) {
  return req<any>(`/api/tickets/${id}`, { method: "DELETE" });
}
export function getConversation(id: number) {
  return req<any[]>(`/api/tickets/${id}/conversation`);
}
export function runTicket(id: number) {
  return req<any>(`/api/tickets/${id}/run`, { method: "POST" });
}
export function sendMessage(id: number, content: string) {
  return req<any>(`/api/tickets/${id}/messages`, {
    method: "POST",
    body: JSON.stringify({ content }),
  });
}
export function closeTicket(id: number) {
  return req<any>(`/api/tickets/${id}/close`, { method: "POST" });
}
export function confirmTicket(id: number) {
  return req<any>(`/api/tickets/${id}/confirm`, { method: "POST" });
}
export function cancelTicket(id: number) {
  return req<any>(`/api/tickets/${id}/cancel`, { method: "POST" });
}
export function getApprovals(ticketId: number) {
  return req<any[]>(`/api/tickets/${ticketId}/approvals`);
}
export function approve(ticketId: number, approvalId: number, decision: string, reason = "") {
  return req<any>(`/api/tickets/${ticketId}/approve`, {
    method: "POST",
    body: JSON.stringify({ approval_id: approvalId, decision, reason }),
  });
}
export function getMetrics() {
  return req<any>("/api/metrics");
}

// MCP 工具面（M5-P2）：外部工具server状态 / 工具映射表 / 连接控制 / 热刷新
export function listMcpServers() {
  return req<any[]>("/api/mcp/servers");
}
export function getMcpServerTools(name: string) {
  return req<any>(`/api/mcp/servers/${name}/tools`);
}
export function mcpConnect(name: string) {
  return req<any>(`/api/mcp/servers/${name}/connect`, { method: "POST" });
}
export function mcpDisconnect(name: string) {
  return req<any>(`/api/mcp/servers/${name}/disconnect`, { method: "POST" });
}
export function mcpRefresh(name: string) {
  return req<any>(`/api/mcp/servers/${name}/refresh`, { method: "POST" });
}

// 工单执行 Trace 回放（治理层审计：模型/工具调用/状态变迁全链路）
export function getTraces(ticketId: number) {
  return req<any[]>(`/api/traces/${ticketId}`);
}

// 基于 WebSocket 实时订阅工单事件流（先回放历史事件，再增量推送；token 经 query 传递）
export function subscribeTicketWS(ticketId: number, onEvent: (e: any) => void): () => void {
  const token = getToken() || "";
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  // 若配置了 API 基址（跨源部署），则把 WS 指向该源；否则同源反代
  const host = API ? API.replace(/^https?:\/\//, "").replace(/\/$/, "")
                   : window.location.host;
  const url = `${proto}//${host}/api/tickets/${ticketId}/ws?token=${encodeURIComponent(token)}`;
  const ws = new WebSocket(url);
  ws.onmessage = (msg) => {
    try { onEvent(JSON.parse(msg.data)); } catch { /* ignore */ }
  };
  return () => { try { ws.close(); } catch { /* ignore */ } };
}


// 知识库管理
export function listDocuments() {
  return req<any[]>("/api/kb");
}
export async function uploadDocument(file: File): Promise<any> {
  const token = getToken();
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch(`${API}/api/kb/upload`, {
    method: "POST",
    headers: token ? { "X-Auth-Token": token } : {},
    body: fd,
  });
  if (res.status === 401) {
    clearToken();
    if (typeof window !== "undefined") window.location.href = "/login";
    throw new Error("未登录");
  }
  if (!res.ok) {
    throw new Error((await res.text().catch(() => "")) || res.statusText);
  }
  return res.json();
}
export function deleteDocument(id: number) {
  return req<any>(`/api/kb/${id}`, { method: "DELETE" });
}
