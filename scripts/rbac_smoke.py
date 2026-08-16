"""RBAC 冒烟测试：验证角色可见范围与删除权限（使用临时数据库，不影响真实数据）。"""
import os
import sys
import tempfile

# 隔离数据库与向量库；强制离线 reader 后端（必须在导入 app 模块前设置）
tmpdir = tempfile.mkdtemp(prefix="rbac_test_")
os.environ["APP_DB_PATH"] = os.path.join(tmpdir, "test.db")
os.environ["VECTOR_DB_PATH"] = os.path.join(tmpdir, "vector-kb")
os.environ["APP_ENV"] = "development"
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9/v1"  # 不可达端口，确保走离线 reader

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402
from app.db import init_db, session  # noqa: E402
from app.main import app  # noqa: E402
from app.security import hash_password  # noqa: E402

init_db()
# 播种测试用户：employee / operator / admin（同一租户）
with session() as conn:
    conn.execute("INSERT OR IGNORE INTO tenants (id, name) VALUES (1, '默认租户')")
    for role in ("employee", "operator", "admin"):
        conn.execute(
            "INSERT OR IGNORE INTO users (tenant_id, username, password_hash, role) VALUES (1,?,?,?)",
            (role, hash_password("pass123"), role),
        )

client = TestClient(app)


def login(u):
    r = client.post("/api/auth/login", json={"username": u, "password": "pass123"})
    assert r.status_code == 200, r.text
    return {"X-Auth-Token": r.json()["token"]}


h_emp = login("employee")
h_op = login("operator")
h_adm = login("admin")

# 1) 各角色建单（员工建 2 张、运维建 1 张）
def create(h, title):
    r = client.post("/api/tickets", json={"title": title, "description": "开通数据库只读权限 demo"}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["id"]


t_emp1 = create(h_emp, "员工的工单A")
t_emp2 = create(h_emp, "员工的工单B")
t_op1 = create(h_op, "运维的工单C")

# 2) 列表可见范围
lst_emp = client.get("/api/tickets", headers=h_emp).json()
lst_op = client.get("/api/tickets", headers=h_op).json()
lst_adm = client.get("/api/tickets", headers=h_adm).json()
assert {t["id"] for t in lst_emp} == {t_emp1, t_emp2}, f"employee 应只见自己的工单: {lst_emp}"
assert {t["id"] for t in lst_op} == {t_emp1, t_emp2, t_op1}, f"operator 应见全部工单: {lst_op}"
assert {t["id"] for t in lst_adm} == {t_emp1, t_emp2, t_op1}, f"admin 应见全部工单: {lst_adm}"
print("✓ 列表可见范围: employee=自己 / operator/admin=全部")

# 3) 详情/对话/运行 访问控制：employee 访问他人工单 → 403；operator/admin 访问他人工单 → 200
r = client.get(f"/api/tickets/{t_op1}", headers=h_emp)
assert r.status_code == 403, f"employee 访问他人工单应 403: {r.status_code} {r.text}"
r = client.get(f"/api/tickets/{t_emp1}", headers=h_op)
assert r.status_code == 200, f"operator 访问员工工单应 200: {r.status_code} {r.text}"
r = client.get(f"/api/tickets/{t_emp1}", headers=h_adm)
assert r.status_code == 200, f"admin 访问员工工单应 200: {r.status_code} {r.text}"
r = client.get(f"/api/tickets/{t_op1}/conversation", headers=h_emp)
assert r.status_code == 403, f"employee 读他人对话应 403: {r.status_code} {r.text}"
r = client.post(f"/api/tickets/{t_op1}/run", headers=h_emp)
assert r.status_code == 403, f"employee 运行他人工单应 403: {r.status_code} {r.text}"
print("✓ 详情/对话/运行访问控制: employee 越权 403 / operator、admin 放行")

# 4) 列表带创建人姓名
names = {t["id"]: t.get("requester_name") for t in lst_op}
assert names[t_emp1] == "employee" and names[t_op1] == "operator", f"requester_name 缺失: {names}"
print("✓ 列表联表带出 requester_name")

# 5) 删除权限：employee/operator 删除 → 403；admin 删除 → 200 且数据级联清除
r = client.delete(f"/api/tickets/{t_emp1}", headers=h_emp)
assert r.status_code == 403, f"employee 删除应 403: {r.status_code} {r.text}"
r = client.delete(f"/api/tickets/{t_emp1}", headers=h_op)
assert r.status_code == 403, f"operator 删除应 403: {r.status_code} {r.text}"
r = client.delete(f"/api/tickets/{t_emp1}", headers=h_adm)
assert r.status_code == 200, f"admin 删除应 200: {r.status_code} {r.text}"
r = client.get(f"/api/tickets/{t_emp1}", headers=h_adm)
assert r.status_code == 404, f"删除后应 404: {r.status_code} {r.text}"
with session() as conn:
    ev = conn.execute("SELECT COUNT(*) c FROM ticket_events WHERE ticket_id=?", (t_emp1,)).fetchone()["c"]
    ap = conn.execute("SELECT COUNT(*) c FROM approvals WHERE ticket_id=?", (t_emp1,)).fetchone()["c"]
assert ev == 0 and ap == 0, f"级联删除不完整: events={ev} approvals={ap}"
r = client.delete(f"/api/tickets/99999", headers=h_adm)
assert r.status_code == 404, "删除不存在的工单应 404"
print("✓ 删除权限: employee/operator 403 / admin 可删且级联清除 / 不存在 404")

# 6) 指标接口：employee 403；operator/admin 200
r = client.get("/api/metrics", headers=h_emp)
assert r.status_code == 403, f"employee 访问指标应 403: {r.status_code} {r.text}"
r = client.get("/api/metrics", headers=h_op)
assert r.status_code == 200, f"operator 访问指标应 200: {r.status_code} {r.text}"
r = client.get("/api/metrics", headers=h_adm)
assert r.status_code == 200, f"admin 访问指标应 200: {r.status_code} {r.text}"
print("✓ 指标接口: employee 403 / operator、admin 200")

print("\n全部 RBAC 冒烟断言通过 ✅")
