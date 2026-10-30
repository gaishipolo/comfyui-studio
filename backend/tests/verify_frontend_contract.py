"""前后端契约测试：拉真实响应，逐字段比对 frontend/src/types/index.ts 的 TS 接口

tsc 只能保证前端内部类型一致，无法发现「后端返回的字段名/结构与 TS 声明不符」。
这里按 TS 接口逐字段核对真实响应，缺失必报，多余仅提示。
"""
import json, sys, urllib.parse, urllib.request

BASE = "http://127.0.0.1:8000/api"

# 逐字摘自 frontend/src/types/index.ts（optional 用 * 标注）
TS = {
    "Template": {
        "req": ["id", "name", "description", "category", "icon", "input_schema",
                "engine", "capability", "workflow_file", "param_mapping", "output_config",
                "is_active", "sort_order", "created_at", "updated_at"],
        "opt": [],
    },
    "Task": {
        "req": ["id", "template_id", "batch_id", "status", "progress", "params",
                "provider_task_id", "result", "error_msg", "created_at",
                "started_at", "finished_at"],
        "opt": ["created_by", "creator_name"],
    },
    "Asset": {
        "req": ["id", "task_id", "file_path", "file_name", "file_size",
                "mime_type", "tags", "meta", "created_at"],
        "opt": ["is_shared", "created_by", "creator_name"],
    },
    "User": {
        "req": ["id", "username", "display_name", "role", "is_active", "created_at"],
        "opt": [],
    },
    "LoginResponse": {
        "req": ["access_token", "token_type", "user"],
        "opt": [],
    },
    "AssetTag": {"req": ["name", "count"], "opt": []},
    "EngineInfo": {"req": ["name", "status"], "opt": ["detail"]},
    # 引擎插件（多引擎）：FieldSpec/CapabilitySpec 与后端 asdict 同构，全字段必在
    "FieldSpec": {
        "req": ["name", "label", "type", "required", "default", "options", "description", "multiple"],
        "opt": [],
    },
    "CapabilitySpec": {"req": ["kind", "label", "fields"], "opt": []},
    "EnginePluginInfo": {
        "req": ["name", "label", "capabilities", "config_schema", "builtin", "enabled",
                "last_error", "filename", "uploaded_by", "config", "has_config"],
        "opt": [],
    },
    "TemplateField": {
        "req": ["name", "label", "type", "required"],
        "opt": ["placeholder", "options", "default", "advanced", "multiple", "accept"],
    },
    # avg_duration_seconds 必须存在（没完成任务时是 null，不是缺字段）→ 归 req
    "UsageByUser": {
        "req": ["user_id", "username", "display_name", "role",
                "task_total", "task_done", "task_error", "task_cancelled",
                "avg_duration_seconds", "storage_bytes", "asset_count"],
        "opt": [],
    },
    "UsageResponse": {
        "req": ["date_from", "date_to", "by_user", "totals"],
        "opt": [],
    },
}

ok_all = True


def check(label: str, iface: str, obj: dict):
    global ok_all
    spec = TS[iface]
    missing = [k for k in spec["req"] if k not in obj]
    extra = [k for k in obj if k not in spec["req"] + spec["opt"]]
    if missing:
        ok_all = False
        print(f"  [FAIL] {label} ({iface}) 缺字段: {missing}")
    else:
        print(f"  [OK] {label} ({iface}) 必填字段齐全 ({len(spec['req'])} 项)")
    if extra:
        print(f"         [WARN] 后端多出字段（TS 未声明）: {extra}")


def req(path, method="GET", data=None, token=None):
    r = urllib.request.Request(BASE + path, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    body = json.dumps(data).encode() if data is not None else None
    with urllib.request.urlopen(r, body) as resp:
        raw = resp.read() or b"{}"
    return json.loads(raw)


print("=== 前后端契约比对（真实响应 vs TS 接口）===\n")

login = req("/auth/login", "POST", {"username": "admin", "password": "admin123"})
check("LoginResponse", "LoginResponse", login)
check("login.user", "User", login["user"])
T = login["access_token"]

check("GET /auth/me", "User", req("/auth/me", token=T))

tpls = req("/templates?include_inactive=true", token=T)
print(f"\n[templates] 共 {len(tpls)} 个")
for t in tpls:
    check(f"  模板 {t.get('name', '?')[:18]}", "Template", t)
    for f in (t.get("input_schema") or {}).get("fields", []):
        check(f"    字段 {f.get('name')}", "TemplateField", f)

tasks = req("/tasks", token=T)
print(f"\n[tasks] 共 {len(tasks)} 个")
for t in tasks[:3]:
    check(f"  任务 {t['id'][:10]}", "Task", t)

assets = req("/assets?limit=3", token=T)
print(f"\n[assets] 抽查 {len(assets)} 个")
for a in assets:
    check(f"  素材 {a.get('file_name', '?')[:22]}", "Asset", a)

tags = req("/assets/tags", token=T)
print(f"\n[asset tags] 共 {len(tags)} 个")
for tg in tags:
    check(f"  标签 {tg.get('name')}", "AssetTag", tg)

engines = req("/engines", token=T)
print(f"\n[engines] 共 {len(engines)} 个")
for e in engines:
    check(f"  引擎 {e.get('name')}", "EngineInfo", e)

plugins = req("/plugins", token=T)
print(f"\n[plugins] 共 {len(plugins)} 个（内置 + 插件）")
for p in plugins:
    check(f"  引擎 {p.get('name')}", "EnginePluginInfo", p)
    for kind, cap in (p.get("capabilities") or {}).items():
        check(f"    能力 {kind}", "CapabilitySpec", cap)
        for f in cap.get("fields", []):
            check(f"      入参 {f.get('name')}", "FieldSpec", f)
    for f in p.get("config_schema", []):
        check(f"    配置项 {f.get('name')}", "FieldSpec", f)

usage = req("/stats/usage", token=T)
print(f"\n[stats/usage] by_user {len(usage.get('by_user', []))} 行")
check("GET /stats/usage", "UsageResponse", usage)
check("  totals", "UsageByUser", usage.get("totals", {}))
for u in usage.get("by_user", [])[:5]:
    check(f"  用户 {u.get('username', '?')}", "UsageByUser", u)

# 附加：Task.result.outputs 结构（TS 声明了 outputs[].filename 等）
print()
for t in tasks:
    r = t.get("result") or {}
    outs = r.get("outputs")
    if outs:
        # node_id 只有 ComfyUI 系引擎的产物才有；插件 provider 返回的产物没有节点概念
        need = ["filename", "subfolder", "type"]
        miss = [k for k in need if k not in outs[0]]
        if miss:
            ok_all = False
            print(f"  [FAIL] result.outputs[0] 缺字段: {miss} (实际 {list(outs[0])})")
        else:
            extra = "含 node_id（ComfyUI）" if "node_id" in outs[0] else "插件 provider，无 node_id"
            print(f"  [OK] result.outputs[0] 字段齐全 (filename/subfolder/type)；{extra}")
        break

print(f"\n结果: {'[OK] 契约一致' if ok_all else '[FAIL] 存在契约不一致'}")
sys.exit(0 if ok_all else 1)
