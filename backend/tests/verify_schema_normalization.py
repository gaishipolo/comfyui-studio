"""验证 input_schema 归一化：写入时补齐缺键，读出时也归一化（兼容存量脏数据）"""
import json, sys, urllib.error, urllib.request, uuid

BASE = "http://127.0.0.1:8000/api"
FIELD_KEYS = {"name", "label", "type", "required"}
# 模板名称有唯一约束，必须用唯一值 —— 否则上次残留会导致本次 400
UNIQUE = f"归一化验证-{uuid.uuid4().hex[:8]}"
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
    ok_all &= cond
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


_, tok = req("/auth/login", "POST", {"username": "admin", "password": "admin123"})
T = tok["access_token"]

print("=== 1. 存量脏数据：读出时应被归一化 ===")
_, tpls = req("/templates?include_inactive=true", token=T)
for t in tpls:
    for f in t["input_schema"]["fields"]:
        missing = FIELD_KEYS - set(f)
        report(f"读出 {t['name'][:16]}/{f['name']}", not missing,
               f"缺 {sorted(missing)}" if missing else "必填键齐全")

print("\n=== 2. 手写残缺字段：写入应被补齐/丢弃 ===")
bad_schema = {"fields": [
    {"name": "prompt", "label": "提示词", "type": "textarea"},   # 缺 required
    {"name": "width", "type": "select", "options": [512, 768]},  # 缺 label + required
    {"label": "没名字的字段", "type": "text"},                     # 无 name -> 丢弃
    {"name": "weird", "type": "不存在的类型"},                    # 非法 type -> 降级 text
    {"name": "adv", "label": "高级", "type": "number",
     "required": True, "advanced": True, "default": 3},          # 完整
    {"name": "image", "label": "参考图", "type": "image_upload",  # 多值图片字段
     "required": True, "multiple": True},
    {"name": "txt_multi", "label": "文本", "type": "text",        # multiple 对文本无意义 -> 丢弃
     "multiple": True},
    {"name": "promptSkill", "label": "提示词优化", "type": "select",  # bool 走下拉：布尔选项 + 布尔默认值
     "options": [True, False], "default": True},
    {"name": "offFlag", "label": "关闭", "type": "select",            # 默认 false 不能被当成「没填」
     "options": [True, False], "default": False},
]}
st, created = req("/templates", "POST", {
    "name": UNIQUE, "input_schema": bad_schema,
    "param_mapping": {"prompt": "1.inputs.text"},
}, token=T)
report("创建模板", st == 200, f"status={st} name={UNIQUE}")
tid = created.get("id")

fields = created["input_schema"]["fields"]
report("无 name 字段被丢弃", len(fields) == 8, f"实际 {len(fields)} 个")

by_name = {f["name"]: f for f in fields}
report("prompt 补上 required", by_name.get("prompt", {}).get("required") is False)
report("width 补上 label+required",
       by_name.get("width", {}).get("label") == "width"
       and by_name.get("width", {}).get("required") is False)
report("非法 type 降级为 text", by_name.get("weird", {}).get("type") == "text")
report("完整字段保持 advanced/default",
       by_name.get("adv", {}).get("advanced") is True
       and by_name.get("adv", {}).get("default") == 3)
report("image_upload 保留 multiple",
       by_name.get("image", {}).get("multiple") is True, str(by_name.get("image")))
report("text 上的 multiple 被丢弃",
       "multiple" not in by_name.get("txt_multi", {}), str(by_name.get("txt_multi")))
report("布尔选项 [True, False] 原样保留",
       by_name.get("promptSkill", {}).get("options") == [True, False],
       str(by_name.get("promptSkill", {}).get("options")))
report("布尔默认值 True 保留",
       by_name.get("promptSkill", {}).get("default") is True,
       str(by_name.get("promptSkill", {}).get("default")))
report("布尔默认值 False 保留（不能被当成没填）",
       by_name.get("offFlag", {}).get("default") is False,
       str(by_name.get("offFlag", {})))

# 全字段必填键齐全
for f in fields:
    missing = FIELD_KEYS - set(f)
    report(f"写入后 {f['name']}", not missing, f"缺 {sorted(missing)}" if missing else "")

print("\n=== 3. 更新路径同样归一化 ===")
st, upd = req(f"/templates/{tid}", "PUT", {
    "input_schema": {"fields": [{"name": "only_name"}]},
}, token=T)
report("更新归一化", st == 200 and upd["input_schema"]["fields"][0].keys() >= FIELD_KEYS,
       f"字段={upd['input_schema']['fields'][0] if st == 200 else st}")

print("\n=== 4. 清理 ===")
st, _ = req(f"/templates/{tid}", "DELETE", token=T)
report("删除测试模板", st == 200, f"status={st}")

print(f"\n结果: {'[OK] 归一化生效' if ok_all else '[FAIL] 归一化有漏'}")
sys.exit(0 if ok_all else 1)
