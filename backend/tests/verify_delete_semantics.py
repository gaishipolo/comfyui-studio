"""删除 vs 停用 语义验证 —— 两个不同按钮不能是同一个行为

停用 = is_active=False，保留记录、名称仍占用
删除 = 物理删除，名称可重新使用
有任务记录的模板不允许删除（保护历史）
"""
import json, sys, urllib.error, urllib.request, uuid

BASE = "http://127.0.0.1:8000/api"
ok_all = True


def req(path, method="GET", data=None, token=None):
    r = urllib.request.Request(BASE + path, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    body = json.dumps(data).encode() if data is not None else None
    try:
        with urllib.request.urlopen(r, body) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def report(label, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


_, tok = req("/auth/login", "POST", {"username": "admin", "password": "admin123"})
T = tok["access_token"]

N = f"删除语义-{uuid.uuid4().hex[:8]}"

print("=== 1. 创建 → 删除 → 名称应可复用 ===")
st, tpl = req("/templates", "POST", {"name": N, "is_active": True}, token=T)
report("创建模板", st == 200, f"status={st}")
tid = tpl["id"]

st, _ = req(f"/templates/{tid}", "DELETE", token=T)
report("删除模板", st == 200, f"status={st}")

# 删除后不应出现在任何列表里
st, alls = req("/templates?include_inactive=true", token=T)
names = [t["name"] for t in alls]
report("删除后从列表消失", N not in names, f"剩 {len(names)} 个模板")

# 关键：名称可复用
st, tpl2 = req("/templates", "POST", {"name": N, "is_active": True}, token=T)
report("名称可重新使用", st == 200, f"status={st}")
if st == 200:
    tid2 = tpl2["id"]
else:
    tid2 = None

print("\n=== 2. 停用 ≠ 删除：停用后名称仍被占用 ===")
M = f"停用语义-{uuid.uuid4().hex[:8]}"
st, tplm = req("/templates", "POST", {"name": M, "is_active": True}, token=T)
report("创建模板 M", st == 200)
tidm = tplm["id"]

st, _ = req(f"/templates/{tidm}", "PUT", {"is_active": False}, token=T)
report("停用模板 M", st == 200)

st, alls = req("/templates?include_inactive=true", token=T)
report("停用后仍出现在管理列表", M in [t["name"] for t in alls])

st, _ = req("/templates", "POST", {"name": M}, token=T)
report("停用后名称仍被占用(应 400)", st == 400, f"status={st}")

print("\n=== 3. 有任务记录的模板不允许删除 ===")
# 找一个真正有任务的模板
st, tasks = req("/tasks?limit=50", token=T)
used_tid = tasks[0]["template_id"] if tasks else None
if used_tid:
    st, body = req(f"/templates/{used_tid}", "DELETE", token=T)
    report("带任务的模板删除被拒(应 400)", st == 400,
           f"status={st} detail={body.get('detail', '')[:40]}")
    # 仍应存在
    st, _ = req(f"/templates/{used_tid}", token=T)
    report("被拒后模板仍在", st == 200)
else:
    print("  [SKIP] 无任务数据，跳过")

print("\n=== 4. 清理 ===")
for tid in [tid2, tidm]:
    if tid:
        st, _ = req(f"/templates/{tid}", "DELETE", token=T)
        report(f"清理 {tid[:8]}", st == 200, f"status={st}")

print(f"\n结果: {'[OK] 删除/停用语义正确' if ok_all else '[FAIL] 语义有问题'}")
sys.exit(0 if ok_all else 1)
