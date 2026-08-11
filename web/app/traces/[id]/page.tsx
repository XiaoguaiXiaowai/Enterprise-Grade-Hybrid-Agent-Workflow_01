"use client";
import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Nav from "@/components/Nav";
import { getTraces } from "@/lib/api";

export default function TracePage() {
  const { id } = useParams<{ id: string }>();
  const [traces, setTraces] = useState<any[]>([]);
  const [err, setErr] = useState("");

  useEffect(() => {
    getTraces(Number(id)).then(setTraces).catch((e) => setErr(String(e)));
  }, [id]);

  return (
    <div className="container">
      <Nav />
      <h2>Trace 回放 · 工单 #{id}</h2>
      {err && <div className="card muted">加载失败: {err}</div>}
      <div className="card">
        <table>
          <thead><tr><th>#</th><th>模型/Provider</th><th>工具</th><th>状态</th><th>延迟</th><th>IO</th></tr></thead>
          <tbody>
            {traces.map((t, i) => (
              <tr key={t.id}>
                <td>{i + 1}</td>
                <td>{t.provider}:{t.model}</td>
                <td>{t.tool_name || "—"}</td>
                <td>{t.state_before} → {t.state_after}</td>
                <td>{t.latency_ms}ms</td>
                <td className="muted" style={{ maxWidth: 260, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{JSON.stringify(t.io_json)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {traces.length === 0 && <p className="muted">该工单暂无 trace 记录。</p>}
      </div>
    </div>
  );
}
