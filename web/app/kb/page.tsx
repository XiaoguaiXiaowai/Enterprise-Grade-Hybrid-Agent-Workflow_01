"use client";
import { useEffect, useRef, useState } from "react";
import Nav from "@/components/Nav";
import { listDocuments, uploadDocument, deleteDocument } from "@/lib/api";

export default function KbPage() {
  const [docs, setDocs] = useState<any[]>([]);
  const [err, setErr] = useState("");
  const [status, setStatus] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = () =>
    listDocuments().then(setDocs).catch((e) => setErr(String(e)));

  useEffect(() => { refresh(); }, []);

  async function onUpload(files: FileList | null) {
    if (!files || files.length === 0) return;
    const file = files[0];
    setStatus(`上传中: ${file.name} ...`);
    setErr("");
    try {
      await uploadDocument(file);
      setStatus(`已上传: ${file.name}`);
      if (fileRef.current) fileRef.current.value = "";
      refresh();
    } catch (e) {
      setErr(String(e));
      setStatus("");
    }
  }

  async function onDelete(id: number, name: string) {
    if (!confirm(`确认删除文档「${name}」？」`)) return;
    setErr("");
    try {
      await deleteDocument(id);
      setStatus(`已删除: ${name}`);
      refresh();
    } catch (e) {
      setErr(String(e));
    }
  }

  return (
    <div className="container">
      <Nav />
      <h2>知识库管理（RAG）</h2>

      <div className="card">
        <div className="row" style={{ gap: 12 }}>
          <input ref={fileRef} type="file"
                 accept=".txt,.md,.pdf,.docx"
                 onChange={(e) => onUpload(e.target.files)}
                 style={{ flex: 1 }} />
          <button onClick={() => fileRef.current?.click()}>上传文档</button>
        </div>
        <p className="muted" style={{ marginBottom: 0 }}>
          支持 .txt / .md / .pdf / .docx 文件（≤10MB）。PDF 提取文本层、Word 提取段落与表格；上传后自动重建检索索引。
        </p>
        {status && <p style={{ color: "var(--ok)" }}>{status}</p>}
        {err && <p style={{ color: "var(--err)" }}>操作失败: {err}</p>}
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>已上传文档（{docs.length}）</h3>
        {docs.length === 0 ? (
          <p className="muted">暂无文档，请上传或先用 seed 脚本初始化演示数据。</p>
        ) : (
          <table>
            <thead>
              <tr><th>ID</th><th>标题</th><th>标签</th><th>文件名</th><th>上传时间</th><th></th></tr>
            </thead>
            <tbody>
              {docs.map((d) => (
                <tr key={d.id}>
                  <td>{d.id}</td>
                  <td>{d.title}</td>
                  <td><span className="badge low">{d.tag}</span></td>
                  <td className="muted">{d.filename}</td>
                  <td className="muted">{d.created_at}</td>
                  <td>
                    <button className="err" style={{ padding: "2px 10px", fontSize: 12 }}
                            onClick={() => onDelete(d.id, d.title)}>删除</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <p className="muted">本页对「operator / admin」开放；上传/删除会同步重建 SQLite FTS 与 LanceDB 向量索引。</p>
    </div>
  );
}
