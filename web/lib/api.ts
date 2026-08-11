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
export function getTicket(id: number) {
  return req<any>(`/api/tickets/${id}`);
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

// SSE：工单事件流（EventSource 无法带自定义 header，token 经 query 传递）
export function subscribeEvents(ticketId: number, onEvent: (e: any) => void): () => void {
  const token = getToken() || "";
  const es = new EventSource(`${API}/api/tickets/${ticketId}/events?token=${encodeURIComponent(token)}`);
  es.onmessage = (msg) => {
    try { onEvent(JSON.parse(msg.data)); } catch { /* ignore */ }
  };
  return () => es.close();
}
