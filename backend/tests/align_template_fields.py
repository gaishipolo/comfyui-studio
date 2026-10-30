"""把存量模板的 input_schema 对齐到插件当前的「能力声明」

背景：模板的 input_schema 是创建那一刻从能力声明拍下来的**快照**。插件升级后
（比如 minimax_h3 的 audio/audio2/audio3 三个槽位合成 audios 列表），老模板还是
旧字段，界面上就继续显示「音频1/音频2/音频3」——能力改了但模板没跟上。

这个脚本用与前端 TemplateManager.capabilitiesToFields 完全相同的规则重算字段，
只对**不一致**的模板发 PUT。默认 dry-run，加 --apply 才写。

用法：
    python tests/align_template_fields.py           # 只看差异
    python tests/align_template_fields.py --apply   # 真正写回
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000/api"
USER = "admin"
PASS = "admin123"

# 元素类型 → 上传控件放行的文件种类
FILE_KINDS = {"image", "audio", "video"}


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
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except Exception:
            return e.code, {"detail": raw.decode(errors="replace")[:300]}


def caps_of(plugin):
    """插件的 capabilities 可能是 dict 或 list，统一成 {kind: cap}"""
    caps = plugin.get("capabilities") or {}
    if isinstance(caps, dict):
        return caps
    return {c.get("kind"): c for c in caps}


def fields_from_capability(cap):
    """与前端 capabilitiesToFields 同规则：元素类型 → 控件类型"""
    fields = []
    for spec in cap.get("fields") or []:
        spec_type = spec.get("type") or "string"
        name = spec.get("name")
        desc = spec.get("description") or ""
        options = list(spec.get("options") or [])
        if not options and spec_type == "bool":
            options = [True, False]

        file_kind = spec_type if spec_type in FILE_KINDS else None
        if file_kind:
            ctype = "file_upload"
        elif options:
            ctype = "select"
        elif spec_type in ("int", "float"):
            ctype = "number"
        elif name == "prompt" or "\n" in desc or "\r" in desc:
            ctype = "textarea"
        else:
            ctype = "text"

        f = {"name": name, "label": spec.get("label") or name, "type": ctype}
        f["required"] = bool(spec.get("required"))
        if file_kind:
            f["accept"] = file_kind
        if options:
            f["options"] = options
        default = spec.get("default")
        if default is not None:
            f["default"] = default
        if desc:
            f["placeholder"] = desc
        if spec.get("advanced"):
            f["advanced"] = True
        if spec.get("multiple"):
            f["multiple"] = True
        fields.append(f)
    return fields


def norm(field):
    """只比对这些键——顺序无关，缺省与显式 false 等价"""
    opts = field.get("options")
    return (
        field.get("name"),
        field.get("label"),
        field.get("type"),
        bool(field.get("required")),
        field.get("accept") or ("image" if field.get("type") in ("file_upload", "image_upload") else None),
        bool(field.get("multiple")),
        json.dumps(opts, sort_keys=True, ensure_ascii=False) if opts else None,
        json.dumps(field.get("default"), ensure_ascii=False) if field.get("default") is not None else None,
    )


def diff(have, want):
    h = {f.get("name"): f for f in have}
    w = {f.get("name"): f for f in want}
    lines = []
    for name in w:
        if name not in h:
            lines.append(f"+ {name}  {w[name].get('type')}"
                         f"{'[multiple]' if w[name].get('multiple') else ''}")
        elif norm(h[name]) != norm(w[name]):
            lines.append(f"~ {name}  {h[name].get('type')} → {w[name].get('type')}"
                         f"{'[multiple]' if w[name].get('multiple') else ''}")
    for name in h:
        if name not in w:
            lines.append(f"- {name}  {h[name].get('type')}（能力里已无此字段）")
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写回（默认只打印差异）")
    ap.add_argument("--only", default="", help="只处理名字包含该子串的模板")
    args = ap.parse_args()

    st, tok = req("/auth/login", "POST", {"username": USER, "password": PASS})
    if st != 200:
        print(f"登录失败：{st} {tok}")
        return 1
    T = tok["access_token"]

    st, plugins = req("/plugins", token=T)
    items = plugins if isinstance(plugins, list) else plugins.get("items", [])
    by_name = {p.get("name"): p for p in items}
    print(f"插件 {len(items)} 个")

    st, tpls = req("/templates", token=T)
    tpls = tpls if isinstance(tpls, list) else tpls.get("items", [])
    print(f"模板 {len(tpls)} 个\n")

    changed = 0
    skipped = 0
    for t in tpls:
        name, engine, cap_kind = t.get("name"), t.get("engine"), t.get("capability")
        if args.only and args.only not in (name or ""):
            continue
        plugin = by_name.get(engine)
        if not plugin or not cap_kind:
            skipped += 1
            continue
        cap = caps_of(plugin).get(cap_kind)
        if not cap:
            print(f"[skip] {name}：插件 {engine} 已无能力 {cap_kind}")
            skipped += 1
            continue

        want = fields_from_capability(cap)
        have = (t.get("input_schema") or {}).get("fields") or []
        lines = diff(have, want)
        if not lines:
            continue

        print(f"[{name}]  engine={engine}  cap={cap_kind}")
        for ln in lines:
            print("    " + ln)
        changed += 1

        if args.apply:
            # 保留用户可能自定义过的值：label 与 placeholder 以模板为准
            hmap = {f.get("name"): f for f in have}
            for f in want:
                old = hmap.get(f["name"])
                if not old:
                    continue
                if old.get("label"):
                    f["label"] = old["label"]
                if old.get("placeholder"):
                    f["placeholder"] = old["placeholder"]
            st, resp = req(f"/templates/{t['id']}", "PUT",
                           {"input_schema": {"fields": want}}, token=T)
            print(f"    → PUT {st}" + ("" if st == 200 else f"  {resp}"))
        print()

    print(f"需要对齐 {changed} 个，跳过 {skipped} 个")
    if changed and not args.apply:
        print("（这是 dry-run，加 --apply 才写回）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
