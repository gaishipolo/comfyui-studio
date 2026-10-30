"""维护脚本：把模板里「字符串形态的布尔下拉」修成真布尔选项

背景：早期能力字段带入时，bool 字段生成了 options: ["true", "false"]（字符串），
而 default 是布尔 true/false。问题有三：
  1. 界面显示英文 true/false，和中文表单不搭；
  2. 用户选中后表单值是字符串 "false"，插件侧 bool("false") == True —— 意思反了；
  3. 与现在的生成规则（options: [true, false]）不一致，同一个能力带出来的模板长得不一样。

本脚本只改「type=select 且 options 全是 true/false 字符串」的字段，把选项转成布尔值，
其他字段一个字节都不动。默认 dry-run，加 --apply 才写库（走 PUT 接口，过一遍归一化校验）。

用法：
  python tests/fix_bool_select_options.py            # 只看会改什么
  python tests/fix_bool_select_options.py --apply    # 真改
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000/api"
APPLY = "--apply" in sys.argv

BOOL_WORDS = {"true": True, "false": False}


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


def needs_fix(field: dict) -> bool:
    if field.get("type") != "select":
        return False
    opts = field.get("options") or []
    if not opts or len(opts) != 2:
        return False
    return all(isinstance(o, str) and o.lower() in BOOL_WORDS for o in opts)


def fix_field(field: dict) -> dict:
    out = dict(field)
    out["options"] = [BOOL_WORDS[str(o).lower()] for o in field["options"]]
    # 默认值也统一成布尔：字符串 "true" / "false" 或 1/0 都转过来
    d = out.get("default")
    if isinstance(d, str) and d.lower() in BOOL_WORDS:
        out["default"] = BOOL_WORDS[d.lower()]
    elif isinstance(d, (int, float)) and not isinstance(d, bool):
        out["default"] = bool(d)
    return out


T = req("/auth/login", "POST", {"username": "admin", "password": "admin123"})[1]["access_token"]
st, templates = req("/templates", token=T)
if st != 200:
    print(f"拉模板列表失败：HTTP {st}")
    sys.exit(1)
items = templates if isinstance(templates, list) else templates.get("items", [])

print(f"{'模板':38s} {'字段':16s} 改动")
print("-" * 96)
changed = 0
for t in items:
    schema = t.get("input_schema") or {}
    fields = schema.get("fields") or []
    targets = [f for f in fields if needs_fix(f)]
    if not targets:
        continue
    names = ", ".join(f["name"] for f in targets)
    print(f"{t['name'][:36]:38s} {names:16s} options 字符串 → 布尔")

    new_fields = [fix_field(f) if needs_fix(f) else f for f in fields]
    for f in targets:
        fixed = next(x for x in new_fields if x["name"] == f["name"])
        print(f"{'':38s} {'':16s}   {f['name']}: {f.get('options')} → {fixed['options']}"
              f"  default {f.get('default')!r} → {fixed.get('default')!r}")
    changed += 1

    if APPLY:
        st, _ = req(f"/templates/{t['id']}", "PUT",
                    {"input_schema": {**schema, "fields": new_fields}}, token=T)
        print(f"{'':38s} {'':16s}   写库: HTTP {st}")

print("-" * 96)
if changed == 0:
    print("没有需要修的模板")
elif APPLY:
    print(f"已修 {changed} 个模板")
else:
    print(f"共 {changed} 个模板待修 —— 确认无误后加 --apply 执行")
