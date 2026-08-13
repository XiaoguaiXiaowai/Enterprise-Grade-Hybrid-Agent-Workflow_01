"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { clearToken } from "@/lib/api";

export default function Nav() {
  const router = useRouter();
  return (
    <nav className="nav">
      <Link href="/tickets">工单台</Link>
      <Link href="/approve">审批台</Link>
      <Link href="/metrics">指标</Link>
      <Link href="/kb">知识库</Link>
      <button
        className="secondary"
        style={{ marginLeft: "auto" }}
        onClick={() => { clearToken(); router.push("/login"); }}
      >
        退出
      </button>
    </nav>
  );
}
