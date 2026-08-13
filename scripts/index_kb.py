"""向量知识库索引脚本：对知识库文档切块 + embedding 写入 LanceDB。

用法：
    .venv/bin/python scripts/index_kb.py            # 用当前 SQLite 知识库内容建索引
    VECTOR_DB_PATH=/tmp/vk .venv/bin/python scripts/index_kb.py  # 指定向量库位置
前置：Ollama 已启动且已拉取 bge-m3（ollama pull bge-m3）。
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.db import init_db, session
from app.kb import init_kb
from app import vector_kb


def load_docs() -> list[dict]:
    """从 SQLite kb_fts 读取全部文档作为索引入口。"""
    with session() as conn:
        rows = conn.execute("SELECT DISTINCT title, tag, content FROM kb_fts").fetchall()
    return [{"title": r["title"], "tag": r["tag"], "content": r["content"]} for r in rows]


def main():
    init_db()
    init_kb()
    if not vector_kb._VECTOR_OK:
        print("依赖缺失：请先安装 lancedb / numpy / pyarrow（见 requirements.txt）")
        sys.exit(1)
    docs = load_docs()
    if not docs:
        print("SQLite 知识库为空，先 run scripts/seed_demo.py 播种。")
        sys.exit(0)
    print(f"待索引文档数: {len(docs)}")
    try:
        n = vector_kb.index_docs(docs)
        print(f"向量索引完成，写入 chunk 数: {n}")
        print(f"向量库位置: {vector_kb._connect().uri if hasattr(vector_kb,'_connect') else settings.vector_db_path}")
    except Exception as e:
        print(f"索引失败（请确认 Ollama 已启动且 pull bge-m3）: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
