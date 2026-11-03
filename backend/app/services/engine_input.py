"""把字节推进生成引擎 —— 上传路径与流程编排共用。

两处消费：
- `api/uploads.py`：用户上传参考图（表单里的 file_upload 字段）
- `services/flow_service.py`：流程步骤之间传产物（上游产物 → 下游引擎）

抽出来是因为「推进引擎」这件事本身与字节从哪来无关 —— 流程里上游步骤
可能是 ComfyUI 产出的，下游步骤是供应商 API，两边引擎不同，但推进的
动作完全一样（读字节 → 交给 Provider 的 upload_input → 拿回引擎侧名字）。

服务层只管成功/失败，HTTP 状态码的翻译留给 API 层。
"""
import logging

from app.engines.factory import acquire_provider

logger = logging.getLogger(__name__)


class EngineInputError(RuntimeError):
    """推进引擎失败。

    unsupported=True 表示引擎压根不支持上传输入文件（区别于「传的时候
    出错」—— 前者是配置问题，提示语和处置方式都不一样）。
    """

    def __init__(self, message: str, *, unsupported: bool = False):
        super().__init__(message)
        self.unsupported = unsupported


async def push_bytes_to_engine(engine: str, data: bytes, name: str) -> str:
    """把字节推进引擎，返回引擎侧文件名（可直接当表单字段值用）。

    返回值形态由各引擎自己定：ComfyUI 落 input 目录返回文件名，
    供应商插件多数落自己的 filestore 并返回 `wbinput:xxx` 短标记
    （短标记是为了不把 MB 级 base64 塞进任务列表的轮询响应体）。
    """
    try:
        lease = acquire_provider(engine)
    except ValueError as exc:
        logger.error(
            "Provider upload acquisition failed engine=%s filename=%s",
            engine, name, exc_info=(type(exc), exc, exc.__traceback__),
        )
        raise EngineInputError(f"引擎不可用：{engine}") from exc
    try:
        with lease as provider:
            return await provider.upload_input(data, name)
    except NotImplementedError as exc:
        logger.error(
            "Provider upload unsupported engine=%s filename=%s",
            engine, name, exc_info=(type(exc), exc, exc.__traceback__),
        )
        raise EngineInputError(
            f"引擎不支持上传输入文件：{engine}", unsupported=True
        ) from exc
    except EngineInputError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 引擎侧任何炸法都收成一种错误
        logger.error(
            "Provider upload failed engine=%s filename=%s",
            engine, name, exc_info=(type(exc), exc, exc.__traceback__),
        )
        raise EngineInputError(f"上传到生成引擎失败：{exc}") from exc
