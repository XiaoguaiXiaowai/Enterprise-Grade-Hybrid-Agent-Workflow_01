"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { login } from "@/lib/api";

export default function Login() {
  const router = useRouter();
  const [u, setU] = useState("admin");
  const [p, setP] = useState("");
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault(); setErr(""); setLoading(true);
    try {
      await login(u, p);
      router.push("/tickets");
    } catch (ex: any) {
      setErr(ex.message || "登录失败");
    } finally { setLoading(false); }
  }

  return (
    <div className="container" style={{ maxWidth: 400, marginTop: 80 }}>
      <div className="card">
        <h2>企业 IT 工单处理 · 登录</h2>
        <p className="muted">演示环境账户：admin / operator / employee（密码由 SEED_USER_PASSWORD 配置，生产环境不自动播种）</p>
        <form onSubmit={submit} style={{ display: "grid", gap: 12 }}>
          <input value={u} onChange={(e) => setU(e.target.value)} placeholder="用户名" />
          <input type="password" value={p} onChange={(e) => setP(e.target.value)} placeholder="密码" />
          {err && <div style={{ color: "var(--err)" }}>{err}</div>}
          <button disabled={loading}>{loading ? "登录中…" : "登录"}</button>
        </form>
      </div>
    </div>
  );
}
