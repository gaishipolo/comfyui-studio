"""视频输出收集验证 —— 之前「生成的视频没有返回到前端」。

根因：get_status 只遍历 `node_output.get("images", [])`，而 ComfyUI 各节点把
产出挂在**不同的 ui 键**上：
    SaveImage / PreviewImage → images
    VHS_VideoCombine        → gifs（内容其实是 mp4/webm，不是 gif）
    其它节点                → video / audio / files …
LTX 视频任务的 /history 里 node 140 的键是 `['gifs']`，于是 outputs 恒为空；
再叠加模板 output_config.nodes=["140"] 把预览 PNG 过滤掉，任务 done 但
result.outputs = [] —— 前端自然什么都拿不到。

本脚本用**真实 /history 响应形状**（含上面两条实测记录）驱动 _collect_outputs，
不连真引擎，可重复跑。
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engines.comfyui import ComfyUIProvider
from app.services.task_service import TaskService

ok_all = True


def report(label, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


# ── 1. 真实 LTX 视频任务：键是 gifs，必须被收进来 ──────────────────
print("=== 真实 /history：VHS_VideoCombine 的 gifs ===")

ltx_history = {
    "outputs": {
        "140": {
            "gifs": [
                {
                    "filename": "LTX-2_00004-audio.mp4",
                    "subfolder": "",
                    "type": "output",
                    "format": "video/h264-mp4",
                }
            ]
        }
    },
    "status": {"status_str": "success", "completed": True, "messages": []},
}

got = ComfyUIProvider._collect_outputs(ltx_history)
report("视频产出被收集到", len(got) == 1, f"{got}")
report("文件名正确", got and got[0]["filename"] == "LTX-2_00004-audio.mp4")
report("节点号正确", got and got[0]["node_id"] == "140")
report("type 正确（output，不是 temp）", got and got[0]["type"] == "output")
report("无多余键名（保持 schema 兼容）",
       got and set(got[0]) == {"filename", "subfolder", "type", "node_id"},
       f"{set(got[0]) if got else None}")

# ── 2. 真实图像任务：键是 images，不能被新逻辑弄坏 ────────────────
print("\n=== 真实 /history：SaveImage 的 images（回归） ===")

img_history = {
    "outputs": {
        "108": {
            "images": [
                {"filename": "ComfyUI_temp_pvsfe_00001_.png", "subfolder": "", "type": "temp"}
            ]
        }
    },
    "status": {"status_str": "success", "completed": True, "messages": []},
}

got = ComfyUIProvider._collect_outputs(img_history)
report("图片产出仍被收集到", len(got) == 1 and got[0]["filename"] == "ComfyUI_temp_pvsfe_00001_.png",
       f"{got}")
report("temp 类型保留（后面可下载）", got and got[0]["type"] == "temp")

# ── 3. 混合节点：一个节点同时给预览图 + 成片 ──────────────────────
print("\n=== 同节点 images + gifs 都要，不能只挑一个 ===")

mixed = {
    "outputs": {
        "140": {
            "images": [{"filename": "preview.png", "subfolder": "", "type": "temp"}],
            "gifs": [{"filename": "final.mp4", "subfolder": "", "type": "output"}],
        }
    },
    "status": {"status_str": "success"},
}
got = ComfyUIProvider._collect_outputs(mixed)
report("两种产出都收集", len(got) == 2, f"{sorted(o['filename'] for o in got)}")

# ── 4. 去重：同一文件被多个 ui 键重复报出来 ───────────────────────
print("\n=== 去重 ===")

dup = {
    "outputs": {
        "10": {
            "images": [{"filename": "a.png", "subfolder": "", "type": "output"}],
            "files": [{"filename": "a.png", "subfolder": "", "type": "output"}],
        }
    },
    "status": {"status_str": "success"},
}
got = ComfyUIProvider._collect_outputs(dup)
report("同名同目录同类型只留一条", len(got) == 1, f"{got}")

# 同名但不同目录 / 不同类型是**不同文件**，不能误去重
diff = {
    "outputs": {
        "10": {
            "images": [
                {"filename": "a.png", "subfolder": "sub", "type": "output"},
                {"filename": "a.png", "subfolder": "", "type": "temp"},
            ]
        }
    },
    "status": {"status_str": "success"},
}
got = ComfyUIProvider._collect_outputs(diff)
report("不同 subfolder/type 不去重", len(got) == 2, f"{got}")

# ── 5. 非文件的 ui 输出不能被当产出 ───────────────────────────────
print("\n=== 非文件输出被跳过 ===")

texty = {
    "outputs": {
        "9": {
            "text": ["some log line", "another"],
            "ui": {"note": "not a list"},
            "images": [],
        }
    },
    "status": {"status_str": "success"},
}
got = ComfyUIProvider._collect_outputs(texty)
report("纯文本列表不算产出", got == [], f"{got}")

# ── 6. 边界：outputs 缺失 / 为 None / 形状不对 ────────────────────
print("\n=== 边界 ===")

report("无 outputs 键返回空", ComfyUIProvider._collect_outputs({}) == [])
report("outputs=None 返回空", ComfyUIProvider._collect_outputs({"outputs": None}) == [])
report("节点值是字符串也不炸",
       ComfyUIProvider._collect_outputs({"outputs": {"1": "oops"}}) == [])
report("列表项缺 filename 被跳过",
       ComfyUIProvider._collect_outputs({"outputs": {"1": {"images": [{"type": "output"}]}}}) == [])
report("列表项不是 dict 被跳过",
       ComfyUIProvider._collect_outputs({"outputs": {"1": {"images": ["str-item"]}}}) == [])

# ── 7. 与 output_config 过滤串起来：LTX 模板只留 node 140 ─────────
print("\n=== _select_outputs 串联（LTX 模板场景） ===")


class _T:
    output_config = {"nodes": ["140"]}


combo = {
    "outputs": {
        "108": {"images": [{"filename": "preview.png", "subfolder": "", "type": "temp"}]},
        "140": {"gifs": [{"filename": "LTX-2_00004-audio.mp4", "subfolder": "", "type": "output"}]},
    },
    "status": {"status_str": "success"},
}
all_out = ComfyUIProvider._collect_outputs(combo)
kept = TaskService._select_outputs(_T, all_out)
report("两个节点都收上来（共 2）", len(all_out) == 2, f"{[o['filename'] for o in all_out]}")
report("白名单过滤后只剩视频", len(kept) == 1 and kept[0]["filename"].endswith(".mp4"),
       f"{[o['filename'] for o in kept]}")

class _All:
    output_config = {"nodes": []}


report("空配置不过滤（防御兜底）", len(TaskService._select_outputs(_All, all_out)) == 2)

# ── 8. 回归守卫 ──────────────────────────────────────────────────
print("\n=== 回归守卫 ===")

src = inspect.getsource(ComfyUIProvider._collect_outputs)
report("不再只读 images 键", 'get("images"' not in src)
report("扫描的是全部 ui 输出值", "for items in node_output.values()" in src)

get_status_src = inspect.getsource(ComfyUIProvider.get_status)
report("get_status 走 _collect_outputs", "_collect_outputs" in get_status_src)

print("\n" + ("ALL OK" if ok_all else "HAS FAILURE"))
sys.exit(0 if ok_all else 1)
