"""模板导出 / 导入：格式校验、自包含工作流、去重、重名处理、引擎缺失告警

导出文件要能「换台机器直接导入」，所以关键不是能不能下载，而是：
内嵌工作流有没有真的带走、input_schema 归一化后有没有变样、
重名会不会悄悄覆盖别人的配置、重复导入会不会堆一堆副本。

需要后端在 127.0.0.1:8000 运行。测试模板与临时工作流文件自建自删。
"""
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# 脚本可能从任意目录启动，先把 backend/ 放进 sys.path 才能 import app.*
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8000/api"
ok = True
# 本次测试创建出来的模板 id（收尾统一删）
CREATED: list[str] = []


def req(path, method="GET", data=None, token=None):
    r = urllib.request.Request(BASE + path, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    body = json.dumps(data).encode() if data is not None else None
    try:
        with urllib.request.urlopen(r, body) as resp:
            raw = resp.read()
            if "application/json" not in (resp.headers.get("Content-Type") or ""):
                return resp.status, raw
            return resp.status, json.loads(raw or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except Exception:
            return e.code, raw


def report(label, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def workflow_files() -> set:
    from app.config import settings
    return {p.name for p in settings.WORKFLOW_DIR.glob("*.json")}


T = req("/auth/login", "POST", {"username": "admin", "password": "admin123"})[1]["access_token"]

# ── 0. 找一个带工作流的模板当导出样本 ──────────────────
print("=== 0. 准备 ===")
st, tpls = req("/templates", token=T)
report("模板列表可读", st == 200 and isinstance(tpls, list), f"共 {len(tpls)} 个")
with_wf = [t for t in tpls if t.get("workflow_file")]
report("存在带工作流的模板（导出才有东西可内嵌）", len(with_wf) > 0,
       f"{len(with_wf)} 个：{', '.join(t['name'] for t in with_wf[:3])}")
if not with_wf:
    print("\n没有带工作流的模板，后续用例无法进行")
    sys.exit(1)
SRC = with_wf[0]
FILES_BEFORE = workflow_files()

# ── 1. 导出 ────────────────────────────────────────────
print(f"\n=== 1. 导出「{SRC['name']}」 ===")
st, raw = req(f"/templates/{SRC['id']}/export", token=T)
report("导出返回 200", st == 200, f"status={st}")
doc = json.loads(raw) if isinstance(raw, bytes) else raw
report("format 标记正确", doc.get("format") == "comfyui-workbench/template", str(doc.get("format")))
report("version = 1", doc.get("version") == 1)
report("带 exported_at / source", bool(doc.get("exported_at")) and bool(doc.get("source")))
report("内嵌了工作流内容", isinstance(doc.get("workflow"), dict) and len(doc["workflow"]) > 0,
       f"{len(doc.get('workflow') or {})} 个节点")

exported = doc["template"]
for key in ("name", "input_schema", "engine", "capability", "workflow_file",
            "param_mapping", "output_config"):
    report(f"模板含 {key}", key in exported)
report("不含本机 id（换实例没意义）", "id" not in exported and "created_at" not in exported)
# 库里可能还存着 image_upload 这种历史别名，导出的应该是归一化后的规范形态
report("input_schema 导出即接口所见（已归一化）",
       exported["input_schema"] == SRC["input_schema"])

# ── 2. 原样导回：重名自动改名 + 工作流去重 ──────────────
print("\n=== 2. 原样导回（重名 → 自动改名） ===")
st, res = req("/templates/import", "POST", doc, token=T)
report("导入返回 200", st == 200, f"status={st} {res if st != 200 else ''}")
if st != 200:
    print("\n导入失败，后续用例无法进行")
    sys.exit(1)
IMPORTED_ID = res["template"]["id"]
CREATED.append(IMPORTED_ID)
report("名字被改成「(导入)」", res["template"]["name"].endswith("(导入)"), res["template"]["name"])
report("给出了重名告警", any("同名" in w for w in res.get("warnings", [])), str(res.get("warnings")))
report("id 是新生成的（不是覆盖）", IMPORTED_ID != SRC["id"])
report("字段一致", res["template"]["input_schema"] == SRC["input_schema"])
report("参数映射一致", res["template"]["param_mapping"] == SRC["param_mapping"])
report("出图节点一致", res["template"]["output_config"] == SRC["output_config"])
report("工作流按内容去重（复用已有文件，不产生副本）",
       res["template"]["workflow_file"] == SRC["workflow_file"],
       res["template"]["workflow_file"])

# 再导一次：目录里不该多出任何文件（重复导入必须幂等）
before = workflow_files()
st, res_again = req("/templates/import", "POST", doc, token=T)
after = workflow_files()
report("重复导入不新增工作流文件", st == 200 and after == before,
       f"新增 {sorted(after - before)}" if after != before else "")
if st == 200:
    CREATED.append(res_again["template"]["id"])

# ── 3. 覆盖模式：保留 id ───────────────────────────────
print("\n=== 3. 覆盖模式（同名 + overwrite=true） ===")
doc2 = json.loads(json.dumps(doc))
doc2["template"]["name"] = res["template"]["name"]  # 用导入后的名字，才撞得上
doc2["template"]["description"] = "覆盖测试 " + uuid.uuid4().hex[:6]
st, res2 = req("/templates/import?overwrite=true", "POST", doc2, token=T)
report("覆盖导入 200", st == 200, f"status={st}")
report("id 保持不变（历史任务不断链）", res2["template"]["id"] == IMPORTED_ID,
       res2["template"]["id"])
report("内容已更新", res2["template"]["description"] == doc2["template"]["description"])
report("有覆盖告警", any("覆盖" in w for w in res2.get("warnings", [])))

# ── 4. 非法输入 ────────────────────────────────────────
print("\n=== 4. 非法输入要明确拒绝 ===")
bad = dict(doc, format="something/else")
st, err = req("/templates/import", "POST", bad, token=T)
report("format 不对 → 400", st == 400, str(err.get("detail"))[:60])

bad = dict(doc, version=99)
st, err = req("/templates/import", "POST", bad, token=T)
report("版本过高 → 400", st == 400, str(err.get("detail"))[:60])

bad = json.loads(json.dumps(doc))
bad["template"] = {"name": "", "input_schema": "not-a-dict"}
st, err = req("/templates/import", "POST", bad, token=T)
report("模板内容不合法 → 400", st == 400, str(err.get("detail"))[:60])

# ── 5. 本机没装引擎：导入成功但要告警 ──────────────────
print("\n=== 5. 引擎缺失 → 导入成功 + 告警 ===")
doc3 = json.loads(json.dumps(doc))
doc3["template"]["name"] = f"deltest-无引擎-{uuid.uuid4().hex[:6]}"
doc3["template"]["engine"] = "no_such_engine"
doc3["template"]["capability"] = "text_to_image"
st, res3 = req("/templates/import", "POST", doc3, token=T)
report("仍能导入（不因缺引擎拒绝）", st == 200, f"status={st} {res3 if st != 200 else ''}")
report("提示了引擎未安装",
       st == 200 and any("no_such_engine" in w for w in res3.get("warnings", [])),
       str(res3.get("warnings") if st == 200 else ""))
if st == 200:
    CREATED.append(res3["template"]["id"])

# ── 6. 没内嵌工作流 + 本机也没有 → 清空绑定并告警 ──────
print("\n=== 6. 工作流没跟着文件走 ===")
doc4 = json.loads(json.dumps(doc))
doc4["template"]["name"] = f"deltest-缺工作流-{uuid.uuid4().hex[:6]}"
doc4["template"]["workflow_file"] = "根本不存在的workflow.json"
doc4["workflow"] = None
st, res4 = req("/templates/import", "POST", doc4, token=T)
report("导入不失败（降级处理）", st == 200, f"status={st} {res4 if st != 200 else ''}")
report("清空了工作流绑定", st == 200 and res4["template"]["workflow_file"] == "")
report("提示了需要重新上传",
       st == 200 and any("工作流" in w for w in res4.get("warnings", [])),
       str(res4.get("warnings") if st == 200 else ""))
if st == 200:
    CREATED.append(res4["template"]["id"])

# ── 清理 ──────────────────────────────────────────────
print("\n=== 清理 ===")
for tid in CREATED:
    st, _ = req(f"/templates/{tid}", "DELETE", token=T)
    report(f"删除测试模板 {tid[:8]}", st == 200, f"status={st}")

# 本次新增且没被任何模板引用的工作流文件删掉（去重生效时这里应为空）
st, tpls = req("/templates", token=T)
referenced = {t["workflow_file"] for t in tpls if t.get("workflow_file")}
leftover = (workflow_files() - FILES_BEFORE) - referenced
report("没有遗留的工作流副本", not leftover, str(sorted(leftover)))
for name in leftover:
    from app.config import settings
    (settings.WORKFLOW_DIR / name).unlink(missing_ok=True)

print()
print("全部通过 ✓" if ok else "存在失败 ✗")
sys.exit(0 if ok else 1)
