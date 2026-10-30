"""单字段多图（FieldSpec.multiple）验证 —— 离线跑，不需要后端服务

背景：MiniMax H3 图生视频的 images 是数组，早期用 image / image2 / image3 三个槽位
绕过去，等于把平台的表达力缺口转嫁给用户。改造后一个图片字段声明 multiple=True，
表单值变成数组，插件统一按列表消费。

覆盖：
- FieldSpec.multiple 默认 False，且 asdict() 会带出去（plugin_loader 用它序列化）
- 四个插件的图片字段：只有一个、multiple=True、没有 image2/image3 残留
- _resolve_images：单值 / 列表保序 / 空列表 / None / 空串混入 / 文件丢失报错
- build_payload：多图按选择顺序进请求体；必填能力缺图要报错
- upload_input 产出的 wbinput: 短标记能被 _resolve_images 还原
- MiniMax H3：时长与分辨率联动校验（1080p 最多 10 秒）

用法：python tests/verify_multi_image.py
"""
import asyncio
import base64
import importlib.util
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engines.base import FieldSpec  # noqa: E402
from app.filestore import save_asset_file  # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
ok_all = True

# 1x1 红色 PNG
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def report(label, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def load_provider(filename: str):
    """按插件加载器的方式载入 examples 里的文件，返回 provider 实例（假 api_key）。"""
    path = EXAMPLES / filename
    module_name = f"verify_multi_{path.stem}_{uuid.uuid4().hex[:6]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    cls = next(
        v for v in vars(module).values()
        if isinstance(v, type) and getattr(v, "name", "") and hasattr(v, "capabilities")
    )
    return cls({"api_key": "sk-verify", "base_url": "https://example.invalid"})


def image_fields(cap) -> list[FieldSpec]:
    return [f for f in cap.fields if f.type == "image"]


# ── ① FieldSpec 契约 ────────────────────────────────────
print("\n[1] FieldSpec.multiple")
f = FieldSpec(name="image", label="参考图", type="image")
report("默认 False（老插件行为不变）", f.multiple is False)
report("asdict 带出 multiple", "multiple" in asdict(f), str(sorted(asdict(f))))

# ── ② 四个插件的图片字段形态 ─────────────────────────────
# 语义分工：图生图（image_to_image）= 单张参考图，多图合成（image_compose）= 一个框多张。
# 所以 multiple 只在「本来就要多张」的能力上开，不是所有图片字段一律开。
print("\n[2] 插件图片字段（一框多图，无槽位残留）")
CASES = [
    ("provider_agnes.py", [("image_to_image", False), ("image_compose", True)]),
    ("provider_grsai.py", [("image_to_image", False), ("image_compose", True)]),
    ("provider_grsai_gpt_image.py", [("image_to_image", False), ("image_compose", True)]),
    ("provider_grsai_minimax_h3.py", [("image_to_video", True)]),
]
providers = {}
for filename, kinds in CASES:
    p = load_provider(filename)
    providers[p.name] = p
    for kind, expect_multi in kinds:
        cap = p.capabilities.get(kind)
        if cap is None:
            report(f"{p.name}/{kind} 存在", False)
            continue
        imgs = image_fields(cap)
        names = [x.name for x in imgs]
        report(f"{p.name}/{kind} 只有一个图片字段", len(imgs) == 1, str(names))
        report(f"{p.name}/{kind} multiple={expect_multi}", bool(imgs) and imgs[0].multiple is expect_multi)
        leftovers = [n for n in names if n != "image"]
        report(f"{p.name}/{kind} 无 image2/image3 残留", not leftovers, str(leftovers))

# ── ③ _resolve_images 各分支 ────────────────────────────
print("\n[3] _resolve_images 分支")
h3 = providers["grsai_minimax_h3"]

key_a = f"verify-multi/{uuid.uuid4().hex}.png"
key_b = f"verify-multi/{uuid.uuid4().hex}.png"
save_asset_file(key_a, TINY_PNG, "image/png")
save_asset_file(key_b, TINY_PNG, "image/png")
mark_a, mark_b = f"wbinput:{key_a}", f"wbinput:{key_b}"

report("单值 → 1 项", len(h3._resolve_images(mark_a)) == 1)
report("列表 → 2 项", len(h3._resolve_images([mark_a, mark_b])) == 2)

# 保序：靠 base64 前缀区分不了内容相同的图，所以换个角度 —— 用 URL 形态校验顺序
urls = ["https://a.example/1.png", "https://b.example/2.png"]
got = h3._resolve_images(urls)
report("列表保序", got == urls, str(got))

report("None → 空列表", h3._resolve_images(None) == [])
report("空列表 → 空列表", h3._resolve_images([]) == [])
report("空串混入被过滤", h3._resolve_images(["", mark_a, None]) == h3._resolve_images([mark_a]))

try:
    h3._resolve_images([f"wbinput:verify-multi/{uuid.uuid4().hex}.png"])
    report("文件丢失要报错", False)
except ValueError as e:
    report("文件丢失要报错", "临时文件已丢失" in str(e), str(e)[:60])

try:
    h3._resolve_images(["not-a-url"])
    report("无法识别的值要报错", False)
except ValueError as e:
    report("无法识别的值要报错", "无法识别" in str(e), str(e)[:60])

# ── ④ build_payload 多图进请求体 ───────────────────────
print("\n[4] build_payload")
payload = h3.build_payload("image_to_video", {
    "prompt": "测试", "image": urls, "duration": 5, "resolution": "480p",
})
report("images 是 2 项且保序", payload.get("images") == urls, str(payload.get("images")))
report("未填音频时不出 audios 键", "audios" not in payload)
report("promptSkill 默认不下发（避免无谓覆盖）", "promptSkill" not in payload)

try:
    h3.build_payload("image_to_video", {"prompt": "测试", "image": []})
    report("图生视频缺图要报错", False)
except ValueError as e:
    report("图生视频缺图要报错", "至少一张参考图" in str(e), str(e)[:60])

for name in ("grsai", "grsai_gpt_image"):
    p = providers[name]
    pl = p.build_payload("image_to_image", {"prompt": "测试", "image": urls})
    report(f"{name} images 保序", pl.get("images") == urls, str(pl.get("images")))
    pl_t2i = p.build_payload("text_to_image", {"prompt": "测试", "image": []})
    report(f"{name} 文生图 images 为空数组", pl_t2i.get("images") == [])

agnes = providers["agnes"]
pl = agnes.build_payload("image_compose", {"prompt": "测试", "image": urls, "size": "2K"})
report("agnes 多图进 extra_body.image", pl.get("extra_body", {}).get("image") == urls)
pl_t2i = agnes.build_payload("text_to_image", {"prompt": "测试", "image": []})
report("agnes 文生图走 return_base64", pl_t2i.get("return_base64") is True and "extra_body" not in pl_t2i)

# ── ⑤ 时长 / 分辨率联动 ─────────────────────────────────
print("\n[5] MiniMax H3 时长校验")
base = {"prompt": "测试", "image": urls}
for dur, res, should_pass in [(10, "1080p", True), (11, "1080p", False), (15, "480p", True), (16, "480p", False), (0, "480p", False)]:
    try:
        h3.build_payload("image_to_video", {**base, "duration": dur, "resolution": res})
        passed = True
    except ValueError:
        passed = False
    report(f"{res} / {dur}s → {'通过' if should_pass else '报错'}", passed == should_pass)

# ── ⑥ upload_input 产出的标记可还原 ─────────────────────
print("\n[6] upload_input → _resolve_images 闭环")
for name in ("agnes", "grsai", "grsai_gpt_image", "grsai_minimax_h3"):
    p = providers[name]
    marker = asyncio.run(p.upload_input(TINY_PNG, "tiny.png"))
    report(f"{name} 返回 wbinput: 标记", marker.startswith("wbinput:"), marker[:28])
    resolved = p._resolve_images([marker])
    report(f"{name} 标记可还原成 1 张图", len(resolved) == 1 and len(resolved[0]) > 20)

print()
print("全部通过" if ok_all else "存在失败项")
sys.exit(0 if ok_all else 1)
