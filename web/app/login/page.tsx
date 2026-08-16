"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { getToken, login, seedInfo } from "@/lib/api";

const NETWORK_ERR = /failed to fetch|networkerror|load failed|fetch failed/i;

export default function Login() {
  const router = useRouter();
  const [u, setU] = useState("admin");
  const [p, setP] = useState("");
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  // undefined=尚未返回；null=无可用演示密码（生产/未播种/请求失败）
  const [demoPwd, setDemoPwd] = useState<string | null | undefined>(undefined);

  // 已登录用户直接进入工单台（token 无效时后续 401 会自动清 token 跳回，不会死循环）
  useEffect(() => {
    if (getToken()) router.replace("/tickets");
  }, [router]);

  // 尽力获取演示播种信息（后端仅非生产环境返回密码）；失败静默降级为通用文案
  useEffect(() => {
    seedInfo()
      .then((info) => setDemoPwd(info?.seeded ? info.password ?? null : null))
      .catch(() => setDemoPwd(null));
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const username = u.trim();
    if (!username) { setErr("请输入用户名"); return; }
    if (!p) { setErr("请输入密码"); return; }
    setErr(""); setLoading(true);
    try {
      await login(username, p);
      router.push("/tickets");
    } catch (ex: any) {
      const m = typeof ex?.message === "string" ? ex.message : "登录失败";
      setErr(NETWORK_ERR.test(m) ? "无法连接服务器，请检查网络后重试" : m);
    } finally { setLoading(false); }
  }

  return (
    <div className="container" style={{ maxWidth: 400, marginTop: 80 }}>
      <div className="card">
        <h2>企业 IT 工单处理 · 登录</h2>
        {demoPwd ? (
          <p className="muted">
            演示账户：admin / operator / employee，密码：<code>{demoPwd}</code>
          </p>
        ) : (
          <p className="muted">请使用已分配的账号登录（生产环境不提供演示账户）</p>
        )}
        <form onSubmit={submit} noValidate style={{ display: "grid", gap: 12 }}>
          <input
            name="username"
            autoComplete="username"
            value={u}
            onChange={(e) => setU(e.target.value)}
            placeholder="用户名"
            aria-label="用户名"
            disabled={loading}
          />
          <input
            type="password"
            name="password"
            autoComplete="current-password"
            value={p}
            onChange={(e) => setP(e.target.value)}
            placeholder="密码"
            aria-label="密码"
            disabled={loading}
          />
          {err && <div role="alert" style={{ color: "var(--err)" }}>{err}</div>}
          <button type="submit" disabled={loading} aria-busy={loading}>
            {loading ? "登录中…" : "登录"}
          </button>
        </form>
      </div>
    </div>
  );
}
