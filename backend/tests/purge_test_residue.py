r"""用新的删除接口清理测试残留模板（顺带真实数据冒烟验证 B31）。

只清理名字以「验证用-B5-generic-」开头的模板 —— 这些是 verify_b5_b6 反复跑出来的纯测试垃圾。
不动「测试模板-M5」（产品界面里的演示场景），也不动任何真实生成图。
"""
import live_guard
live_guard.require_live_ok()

import json
import sys
import urllib.error
import urllib.request

from test_env import API_BASE_URL

BASE = API_BASE_URL
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


def main() -> int:
    st, login = req(
        "/auth/login",
        "POST",
        body={"username": "admin", "password": "admin123"},
    )
    if st != 200 or not isinstance(login, dict) or not login.get("access_token"):
        print(f"登录失败：HTTP {st} {login}")
        return 1
    token = login["access_token"]

    st, templates = req("/templates?include_inactive=true", token=token)
    if st != 200 or not isinstance(templates, list):
        print(f"拉模板列表失败：HTTP {st} {templates}")
        return 1
    targets = [item for item in templates if item["name"].startswith(TARGET_PREFIX)]
    print(f"待清理测试模板 {len(targets)} 个: {[item['name'] for item in targets]}")

    st, tasks = req("/tasks?limit=500", token=token)
    if st != 200 or not isinstance(tasks, list):
        print(f"拉任务列表失败：HTTP {st} {tasks}")
        return 1
    by_template: dict[str, list] = {}
    for task in tasks:
        by_template.setdefault(task["template_id"], []).append(task)

    removed_tasks = 0
    removed_templates = 0
    failed = 0
    for template in targets:
        for task in by_template.get(template["id"], []):
            st, body = req(f"/tasks/{task['id']}", "DELETE", token=token)
            if st == 200:
                removed_tasks += 1
            else:
                failed += 1
                print(f"   删任务失败 {st} {body}")
        st, body = req(f"/templates/{template['id']}", "DELETE", token=token)
        if st == 200:
            removed_templates += 1
        else:
            failed += 1
            print(f"   删模板失败 {st} {body}")

    print(f"清理完成: 任务 {removed_tasks} 个, 模板 {removed_templates} 个")

    st, left = req("/templates?include_inactive=true", token=token)
    if st != 200 or not isinstance(left, list):
        print(f"复查模板失败：HTTP {st} {left}")
        return 1
    leftovers = [item["name"] for item in left if item["name"].startswith(TARGET_PREFIX)]
    print(f"剩余测试模板: {leftovers}")
    failed += len(leftovers)

    st, assets = req("/assets?limit=200", token=token)
    if st != 200:
        print(f"复查素材失败：HTTP {st} {assets}")
        return 1
    print(f"剩余素材: {len(assets) if isinstance(assets, list) else assets} 条")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
