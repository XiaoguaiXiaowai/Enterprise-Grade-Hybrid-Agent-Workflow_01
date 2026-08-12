// 前端 API 客户端：封装 fetch、token 持久化、SSE。
const API = process.env.NEXT_PUBLIC_API_URL || "";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem("token");
}
export function setToken(t: string) {
  window.localStorage.setItem("token", t);
}
export function clearToken() {
  window.localStorage.removeItem("token");
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
  const data = await req<{ token: string; user: any }>("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  setToken(data.token);
  return data.user;
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
export function listAllTickets() {
  return req<any[]>("/api/tickets/all");
}
export function getTicket(id: number) {
  return req<any>(`/api/tickets/${id}`);
}
export function getConversation(id: number) {
  return req<any[]>(`/api/tickets/${id}/conversation`);
}
export function runTicket(id: number) {
  return req<any>(`/api/tickets/${id}/run`, { method: "POST" });
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
export function getTraces(ticketId: number) {
  return req<any[]>(`/api/traces/${ticketId}`);
}
export function getMetrics() {
  return req<any>("/api/metrics");
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

