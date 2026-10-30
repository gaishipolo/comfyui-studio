"""normalize_input_schema 幂等性验证 —— 该函数每次模板写入都会跑，重复保存不得累积丢失数据"""
import sys, uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.schemas.schemas import normalize_input_schema

ok_all = True


def report(label, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


print("=== 幂等性：normalize(normalize(x)) == normalize(x) ===")

SAMPLES = {
    "完整字段": {"fields": [{
        "name": "prompt", "label": "提示词", "type": "textarea",
        "required": True, "placeholder": "写点什么", "options": [1, 2],
        "default": 0, "advanced": True,
    }]},
    "零值默认": {"fields": [{
        "name": "n", "label": "数值", "type": "number",
        "required": False, "default": 0, "advanced": False,
    }]},
    "空串默认": {"fields": [{
        "name": "s", "label": "文本", "type": "text",
        "required": False, "default": "",
    }]},
    "空 options": {"fields": [{"name": "x", "options": []}]},
    "缺所有键": {"fields": [{"name": "bare"}]},
    "无 fields 键": {"other": "data"},
    "fields 非列表": {"fields": "oops"},
    "空 schema": {},
    "脏字段混杂": {"fields": [
        {"name": "good", "type": "text", "required": True},
        {"label": "无名"},
        None,
        "字符串不是字典",
        {"name": "  带空格  ", "type": "bogus"},
    ]},
}

for name, schema in SAMPLES.items():
    once = normalize_input_schema(schema)
    twice = normalize_input_schema(once)
    three = normalize_input_schema(twice)
    report(f"{name:12} 二次归一化稳定", once == twice == three,
           f"1st={once}" if once != twice else "")

print("\n=== 关键语义不得丢失 ===")

# default=0 / default="" / advanced=False 这类"假值"必须保留
r = normalize_input_schema({"fields": [{"name": "n", "default": 0}]})
report("default=0 被保留", r["fields"][0].get("default") == 0, f"{r['fields'][0]}")

r = normalize_input_schema({"fields": [{"name": "s", "default": ""}]})
report('default="" 被保留', r["fields"][0].get("default") == "", f"{r['fields'][0]}")

r = normalize_input_schema({"fields": [{"name": "f", "required": False}]})
report("required=False 显式存在", r["fields"][0].get("required") is False)

# required 真值
r = normalize_input_schema({"fields": [{"name": "t", "required": True}]})
report("required=True 保留", r["fields"][0].get("required") is True)

# label 缺省时回落为 name
r = normalize_input_schema({"fields": [{"name": "回落"}]})
report("label 缺省回落 name", r["fields"][0]["label"] == "回落")

# 非法 type 降级
r = normalize_input_schema({"fields": [{"name": "z", "type": "whatever"}]})
report("非法 type -> text", r["fields"][0]["type"] == "text")

# 未知字段保留（不丢键）—— 后端不擅自丢弃扩展信息
r = normalize_input_schema({"fields": [{"name": "e", "custom_key": "v"}]})
report("未知键被清理", "custom_key" not in r["fields"][0], f"{r['fields'][0]}")

# 顶层非 fields 键保留
r = normalize_input_schema({"fields": [], "meta_extra": 123})
report("顶层额外键保留", r.get("meta_extra") == 123)

# 非 dict 输入安全降级
report("非 dict 输入 -> {}", normalize_input_schema("junk") == {})
report("None 输入 -> {}", normalize_input_schema(None) == {})
report("list 输入 -> {}", normalize_input_schema([1, 2]) == {})

# 往返：归一化结果应满足前端 TemplateField 必填键
r = normalize_input_schema(SAMPLES["脏字段混杂"])
need = {"name", "label", "type", "required"}
all_have = all(need <= set(f) for f in r["fields"])
report("所有产出字段满足 TS 必填键", all_have,
       f"字段数 {len(r['fields'])}（无名/非法应已丢弃或降级）")

print(f"\n结果: {'[OK] 归一化幂等且语义无损' if ok_all else '[FAIL] 归一化有问题'}")
sys.exit(0 if ok_all else 1)
