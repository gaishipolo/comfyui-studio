"""inject_params 参数类型对齐验证 —— 前端 select/number 控件给的是字符串，
写进工作流节点前必须按工作流原值的类型对齐，否则类型就漂了。

重点守两类陷阱：
  1. 字符串数值 "480" 直接写进 INT 节点 → 落库/日志类型与实际提交不一致；
  2. 字符串布尔 "false" 透传 → Python bool("false") 是 True，静默得到反义值。

背景（已核实，别再往参数类型上归因）：ComfyUI /prompt 的 validate_inputs
本身也按节点声明强转（execution.py：INT→int()、FLOAT→float()、BOOLEAN→bool()），
且 server.post_prompt 是校验通过才返回 prompt_id。所以类型问题只会导致
400 invalid_input_type，不会走到 CUDA kernel。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engines.comfyui import ComfyUIProvider

ok_all = True


def report(label, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


# ── 1. 真实工作流：LTX-2.3 视频模板（参数映射取自 workbench.db）────────
print("=== 真实工作流（LTX-2.3）：字符串表单值 → 节点输入 ===")

LTX = Path(__file__).resolve().parents[1] / "workflows" / "LTX-2.3_-_I2V_T2V_Basic_GGUF_147b75.json"
if LTX.exists():
    wf = json.loads(LTX.read_text(encoding="utf-8"))
    mapping = {
        "prompt": "121.inputs.text",
        "width": "292.inputs.value",
        "height": "293.inputs.value",
        "time": "291.inputs.value",
        "t2v": "290.inputs.value",
    }
    # 前端 select/number 控件一律给字符串，和 workbench.db 落库一致
    params = {
        "prompt": "A beautiful chinese girl",
        "width": "480", "height": "832", "time": "5", "t2v": "false",
    }
    out = ComfyUIProvider.inject_params(wf, mapping, params)

    report("width  -> int", out["292"]["inputs"]["value"] == 480
           and type(out["292"]["inputs"]["value"]) is int,
           repr(out["292"]["inputs"]["value"]))
    report("height -> int", out["293"]["inputs"]["value"] == 832
           and type(out["293"]["inputs"]["value"]) is int,
           repr(out["293"]["inputs"]["value"]))
    report("time   -> int", out["291"]["inputs"]["value"] == 5
           and type(out["291"]["inputs"]["value"]) is int,
           repr(out["291"]["inputs"]["value"]))
    report("prompt -> str", isinstance(out["121"]["inputs"]["text"], str))
    report("t2v    -> bool，且 'false' 是 False",
           out["290"]["inputs"]["value"] is False,
           repr(out["290"]["inputs"]["value"]))
    report("原工作流未被就地修改", wf["292"]["inputs"]["value"] == 480
           and type(wf["292"]["inputs"]["value"]) is int)
else:
    report("LTX-2.3 工作流文件存在", False, str(LTX))

# ── 2. _coerce_like 边界 ────────────────────────────────────────────
print("\n=== _coerce_like 边界 ===")

c = ComfyUIProvider._coerce_like
report("int 基准 + '480'  -> 480", c(480, "480") == 480 and type(c(480, "480")) is int)
report("int 基准 + '480.0'-> 480", c(480, "480.0") == 480 and type(c(480, "480.0")) is int)
report("int 基准 + 480    -> 480", c(480, 480) == 480)
report("int 基准 + 'abc'  -> 原样返回（交给 ComfyUI 报 invalid_input_type）",
       c(480, "abc") == "abc", repr(c(480, "abc")))
report("float 基准 + '24' -> 24.0", c(24.0, "24") == 24.0 and type(c(24.0, "24")) is float)
report("str  基准 + 123   -> '123'", c("x", 123) == "123")

report("bool 基准 + 'false'-> False（bool('false') 是 True 的陷阱）",
       c(True, "false") is False)
report("bool 基准 + 'False'-> False", c(True, "False") is False)
report("bool 基准 + '0'    -> False", c(True, "0") is False)
report("bool 基准 + ''     -> False", c(True, "") is False)
report("bool 基准 + 'true' -> True", c(True, "true") is True)
report("bool 基准 + '1'    -> True", c(True, "1") is True)
report("bool 基准 + True   -> True", c(True, True) is True)
report("bool 基准 + False  -> False", c(True, False) is False)
report("bool 基准排在 int 之前（bool 是 int 子类）",
       c(True, "1") is True and c(1, "1") == 1)

report("None 基准 -> 原样返回", c(None, "480") == "480")

# ── 3. 映射错误仍要报清楚 ───────────────────────────────────────────
print("\n=== 映射错误仍抛 ValueError ===")
for label, mapping, expect in [
    ("节点不存在", {"width": "999.inputs.value"}, "不存在"),
    ("路径非法", {"width": "no_dots"}, "非法"),
]:
    try:
        ComfyUIProvider.inject_params({"1": {"inputs": {"value": 1}}}, mapping, {"width": "480"})
        report(label, False, "未抛异常")
    except ValueError as e:
        report(label, expect in str(e), str(e)[:60])

# ── 4. 未映射的字段 / 未提供的字段 ──────────────────────────────────
print("\n=== 其它 ===")
wf2 = {"1": {"class_type": "X", "inputs": {"value": 480, "other": 1}}}
out2 = ComfyUIProvider.inject_params(wf2, {"a": "1.inputs.value"}, {"b": "zzz"})
report("params 里没有的字段跳过", out2["1"]["inputs"]["value"] == 480)
out3 = ComfyUIProvider.inject_params(wf2, {"a": "1.inputs.other"}, {"a": "keep"})
report("字符串基准保持 str", out3["1"]["inputs"]["other"] == "keep")

print("\n" + ("ALL OK" if ok_all else "HAS FAILURE"))
sys.exit(0 if ok_all else 1)
