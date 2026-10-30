# 设计：图片上传字段支持从素材库选择（2026-10-28）

## 需求

需要上传图片的工作流，除了选择本地文件外，还可以选择素材库里已有的图片。
后续可能扩展到视频。

## 已确认的决策（用户拍板）

1. **后端中转接口**（而非前端下载再转传）：新增 `POST /api/upload/from-asset`
2. **弹窗素材网格**（而非下拉/侧栏）：新组件 `AssetPickerModal`
3. 开做，用户自测（不跑测试套件与 UI 回归）

## 为什么必须"再传一次"

素材库的文件在**本服务磁盘**上，而 ComfyUI（192.168.77.155:8888）是独立机器，
看不到我们的存储目录。参考图的工作方式是"文件名填进工作流参数 + 字节躺在
ComfyUI 输入目录"。所以从素材库选完，**仍必须把字节推进引擎输入目录**，
拿到引擎侧文件名后走与本地上传完全相同的参数注入链路。

## 后端

`backend/app/api/uploads.py` 新增 `POST /api/upload/from-asset`，body
`AssetUploadRequest { asset_id, engine = "comfyui" }`：

- 查 `Asset` 记录 → 404「素材不存在」
- **mime 前缀白名单** `ALLOWED_MIME_PREFIXES = ("image/",)` → 400（以后支持视频加一行）
- `asset_disk_path()` 解析磁盘路径（**复用 filestore 的唯一越界守卫，不复制第二份**）
  → None 或非文件 = 404「素材文件在磁盘上找不到了」
- `read_bytes()` → OSError 归 404
- `provider.upload_input(data, target.name)` → 引擎挂 502，与 `/image` 同款语义
- 返回 `{filename, engine}`，与 `/image` 同构
- **不按 20MB 卡**：库里的文件本就是生成产物/历史上传，入库时已校验过一次

## 前端

- `client.ts`：`uploadFromAsset(assetId, engine)` 返回引擎侧文件名（与 `uploadImage` 同构）
- **`AssetPickerModal.tsx`（新）**：弹窗素材网格
  - 防抖搜索 300ms（与素材库一致），一次取最近 60 张
  - `kind='image'` → 按 mime 前缀本地过滤（后端列表接口暂不支持 mime 筛选）；
    以后支持视频只换 kind，后端白名单同步扩
  - 缩略图走 `AuthImage`（素材接口带 JWT，`<img src>` 直连会整排 401）
  - 四态齐全：loading 骨架 / error + 重试 / 空 / 网格
  - 所有按钮 `type="button"`；搜索框拦回车（含 IME isComposing 判断），防误触发表单提交
- **`DynamicForm.tsx` 的 `image_upload` 字段**：
  - 空态：「点击上传{label}」+「从素材库选择」两个入口
  - 已选态：「更换 / 从素材库 / 移除」
  - 选中素材 → 预览 blob 先出图（失败不致命，退回只有文件名的值态）→
    `uploadFromAsset` 推引擎 → 填值。**顺序执行**保证推送失败清预览时，
    不会有慢半拍的预览又冒出来
  - `setPreview` 泛化为 `(name, Blob, fileName)`，object URL 回收机制不变
    （换图/移除/卸载三处）

## 红线（全程未动）

- 任务提交链路 / `task_service` / 测试一个没动
- UI 回归钩子全保留（上传区本来就没有钩子断言）
- 注入工作流部分零改动：两个来源产出同样的引擎侧文件名

## 验收

- `tsc -b` + `vite build` 全绿（`index-D3L_AmGm.js` / `index-BAPEGlpD.css`）
- 后端 `py_compile` + `from app.main import app` 导入通过
- **后端改动需重启 uvicorn 才生效**（不带 --reload）
