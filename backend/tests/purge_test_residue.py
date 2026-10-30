r"""用新的删除接口清理测试残留模板（顺带真实数据冒烟验证 B31）。

只清理名字以「验证用-B5-generic-」开头的模板 —— 这些是 verify_b5_b6 反复跑出来的纯测试垃圾。
不动「测试模板-M5」（产品界面里的演示场景），也不动任何真实生成图。
"""
import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000/api"
TARGET_PREFIX = "验证用-B5-generic-"


def req(path, method="GET", token=None, body=None):
    r = urllib.request.Request(BASE + path, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    data = json.dumps(body).encode() if body is not None else (b"" if method != "GET" else None)
    try:
        with urllib.request.urlopen(r, data) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


_, tok = req("/auth/login", "POST", body={"username": "admin", "password": "admin123"})
T = tok["access_token"]

_, tpls = req("/templates?include_inactive=true", token=T)
targets = [t for t in tpls if t["name"].startswith(TARGET_PREFIX)]
print(f"待清理测试模板 {len(targets)} 个: {[t['name'] for t in targets]}")

_, tasks = req("/tasks?limit=500", token=T)
by_tpl: dict[str, list] = {}
for k in tasks:
    by_tpl.setdefault(k["template_id"], []).append(k)

removed_tasks = removed_tpl = 0
for t in targets:
    for k in by_tpl.get(t["id"], []):
        st, body = req(f"/tasks/{k['id']}", "DELETE", token=T)
        if st == 200:
            removed_tasks += 1
        else:
            print(f"   删任务失败 {st} {body}")
    st, body = req(f"/templates/{t['id']}", "DELETE", token=T)
    if st == 200:
        removed_tpl += 1
    else:
        print(f"   删模板失败 {st} {body}")

print(f"清理完成: 任务 {removed_tasks} 个, 模板 {removed_tpl} 个")

_, left = req("/templates?include_inactive=true", token=T)
print(f"剩余模板: {[t['name'] for t in left]}")
_, assets = req("/assets?limit=200", token=T)
print(f"剩余素材: {len(assets) if isinstance(assets, list) else assets} 条")
