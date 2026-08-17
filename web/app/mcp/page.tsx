"use client";
import { useCallback, useEffect, useState } from "react";
import Nav from "@/components/Nav";
import {
  listMcpServers, getMcpServerTools,
  mcpConnect, mcpDisconnect, mcpRefresh, getToken,
} from "@/lib/api";
import { useRouter } from "next/navigation";

const RISK_LABEL: Record<string, string> = { low: "低", medium: "中", high: "高" };
const ROLE_LABEL: Record<string, string> = { employee: "员工", operator: "运维", admin: "管理员" };

export default function McpPanel() {
  const router = useRouter();
  const [servers, setServers] = useState<any[]>([]);
  const [tools, setTools] = useState<Record<string, any[]>>({});
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState<string>("");

  const refresh = useCallback(async () => {
    try {
      const list = await listMcpServers();
      setServers(list);
      const m: Record<string, any[]> = {};
      for (const s of list) {
        try { m[s.name] = (await getMcpServerTools(s.name))?.tools ?? []; }
        catch { m[s.name] = []; }
      }
      setTools(m);
    } catch { /* 未登录等 */ }
  }, []);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    refresh();
    const iv = setInterval(refresh, 3000);
    return () => clearInterval(iv);
  }, [refresh, router]);

  async function act(fn: (n: string) => Promise<any>, name: string, label: string) {
    setMsg(""); setBusy(name);
    try {
      const r = await fn(name);
      setMsg(`${label} ${name} 成功：healthy=${r.healthy ?? "-"} tools=[${(r.tools || []).join(", ")}]`);
    } catch (ex: any) { setMsg(ex.message); }
    setBusy("");
    await refresh();
  }

  return (
    <div className="container">
      <Nav />
      <h2>MCP 工具面（外部 Server 一屏可见）</h2>
      <p className="muted">
        工具实现可以外置到外部系统（MCP），但能否被调 / 谁调 / 什么风险 / 要不要审批，
        都由治理层的映射表白名单说了算。可对 server 执行 连接 / 断开（故障演练）／刷新（热刷新工具清单）。
      </p>
      {msg && <div className="card muted">{msg}</div>}
      {servers.length === 0 && <div className="card"><p className="muted">未启用 MCP 或未配置 server（见 .env 的 MCP_ENABLED 与 mcp_servers.json）。</p></div>}
      {servers.map((s) => (
        <div key={s.name} className="card" style={{ marginBottom: 14 }}>
          <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
            <div>
              <strong>{s.name}</strong>
              <span className="muted" style={{ fontSize: 12 }}> · {s.transport}</span>
              <span style={{ marginLeft: 8, color: s.healthy ? "var(--ok, green)" : "var(--err, red)" }}>
                {s.healthy ? "● 已连接" : "○ 未连接"}
              </span>
              {s.error && s.error !== "not_connected" && (
                <div className="muted" style={{ fontSize: 12, color: "red" }}>error: {s.error}</div>
              )}
            </div>
            <div className="row" style={{ gap: 6 }}>
              <button className="secondary" disabled={busy === s.name}
                      onClick={() => act(mcpConnect, s.name, "连接")}>连接</button>
              <button className="secondary" disabled={busy === s.name}
                      onClick={() => act(mcpDisconnect, s.name, "断开")}>断开</button>
              <button className="secondary" disabled={busy === s.name}
                      onClick={() => act(mcpRefresh, s.name, "刷新")}>刷新</button>
            </div>
          </div>

          <table style={{ width: "100%", borderCollapse: "collapse", marginTop: 10 }}>
            <thead>
              <tr className="muted" style={{ textAlign: "left", fontSize: 12 }}>
                <th style={{ padding: "6px 8px" }}>工具</th>
                <th>风险</th>
                <th>允许角色</th>
                <th>参数约束 (param_schema)</th>
                <th>描述</th>
              </tr>
            </thead>
            <tbody>
              {(tools[s.name] || []).map((t) => (
                <tr key={t.name} style={{ borderTop: "1px solid var(--border)", fontSize: 13 }}>
                  <td style={{ padding: "6px 8px" }}><code>{s.name}__{t.name}</code></td>
                  <td style={{ color: t.risk === "high" ? "var(--err, red)" : "inherit" }}>
                    {RISK_LABEL[t.risk] || t.risk}
                  </td>
                  <td>{(t.roles || []).map((r: string) => ROLE_LABEL[r] || r).join(" / ")}</td>
                  <td style={{ fontFamily: "monospace", fontSize: 11 }}>
                    {t.param_schema
                      ? `required=[${(t.param_schema.required || []).join(",")}]`
                      : "—"}
                  </td>
                  <td className="muted">{t.description || "—"}</td>
                </tr>
              ))}
              {(tools[s.name] || []).length === 0 && (
                <tr><td colSpan={5} className="muted" style={{ padding: 6 }}>
                  工具清单需连接后获取（点“连接”）。
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}
