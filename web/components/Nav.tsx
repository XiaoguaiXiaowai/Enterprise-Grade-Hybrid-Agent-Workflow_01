"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { clearToken, getUser } from "@/lib/api";

const ROLE_LABEL: Record<string, string> = {
  employee: "员工",
  operator: "运维",
  admin: "管理员",
};

export default function Nav() {
  const router = useRouter();
  const [user, setUser] = useState<any | null>(null);

  useEffect(() => {
    const load = () => setUser(getUser());
    load();
    // 多标签页间登录/退出同步
    window.addEventListener("storage", load);
    return () => window.removeEventListener("storage", load);
  }, []);

  const role: string | null = user?.role ?? null;
  const isOps = role === "operator" || role === "admin";

  return (
    <nav className="nav">
      <Link href="/tickets">工单台</Link>
      {isOps && <Link href="/approve">审批台</Link>}
      {isOps && <Link href="/metrics">指标</Link>}
      {isOps && <Link href="/kb">知识库</Link>}
      <span className="muted" style={{ marginLeft: "auto", fontSize: 13 }}>
        {user?.username && <>👤 {user.username}（{ROLE_LABEL[role || ""] || role}）</>}
      </span>
      <button
        className="secondary"
        onClick={() => { clearToken(); router.push("/login"); }}
      >
        退出
      </button>
    </nav>
  );
}
