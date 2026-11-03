r"""素材文件的统一存取入口 —— 本地磁盘 / MinIO 对象存储可切换。

**调用方不许绕过这里直接摸 `settings.STORAGE_DIR`**，也不许假设文件一定有本地路径。
换存储后端（本机目录 → MinIO）时只改 `settings.STORAGE_BACKEND`，业务代码不动。

三层职责：

1. **键的校验与归一**（``safe_key``）—— 路径穿越防护只允许有这一份实现，
   复制到多个 API 模块就可能出现一处忘了加的情况。
2. **后端实现**（``LocalFileStore`` / ``MinioFileStore``）—— 同一组语义。
3. **取用入口**（``get_store`` 与模块级函数）—— 业务代码只认这几个名字。

关于对象键的分隔符：Windows 下 ``str(Path("a") / "b.png")`` 得到 ``a\\b.png``，
而 ``mc mirror`` / S3 语义里分隔符是 ``/``。历史上 ``assets.file_path`` 存的就是
反斜杠形式，本地用还能凑合（Windows 接受 ``/`` 也接受 ``\``），一上对象存储就对不上号。
所以这里统一归一成 ``/``，写入端（``task_service._save_asset``）也按 POSIX 分隔符落库；
读入端两种分隔符都吃，老记录不用迁移。
"""
from __future__ import annotations

import io
import logging
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

from app.config import settings

logger = logging.getLogger(__name__)

# 素材文件的单次流式读取块大小
_CHUNK = 1024 * 256

_MISSING_OBJECT_CODES = {"NoSuchKey", "NoSuchObject"}
FILESTORE_UNAVAILABLE_DETAIL = "文件存储服务暂时不可用"


class FileStoreUnavailableError(RuntimeError):
    """The configured storage backend could not complete an operation."""

    def __init__(self, *_diagnostic):
        super().__init__(FILESTORE_UNAVAILABLE_DETAIL)


def _object_error_code(exc: Exception) -> str:
    code = getattr(exc, "code", "")
    if code:
        return str(code)
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error", response)
        if isinstance(error, dict) and error.get("Code"):
            return str(error["Code"])
    return ""


def _object_error_reason(exc: Exception) -> str:
    code = _object_error_code(exc)
    parts = [type(exc).__name__]
    if code:
        parts.append(code)
    if str(exc):
        parts.append(str(exc))
    return ": ".join(parts)


# ────────────────────────────── 键的校验 ──────────────────────────────

def safe_key(file_path: str) -> str | None:
    """把库里记录的存储键归一成 POSIX 形式；非法则返回 None，调用方必须拒绝操作。

    判非法的依据：
    - 空串
    - 归一后出现 ``..`` 段（``a/../../etc/passwd``）
    - 绝对路径（``/etc/passwd``）或 Windows 盘符（``C:/x``）
    """
    if not isinstance(file_path, str) or not file_path.strip():
        return None

    raw = file_path.strip().replace("\\", "/")

    # 绝对路径（/etc/passwd）和 Windows 盘符（C:/x）都不该出现在库里 ——
    # 出现就说明数据有问题，明确拒绝，别把 /etc/passwd 静默改写成 etc/passwd
    # 悄悄落到别处去。
    if raw.startswith("/"):
        return None
    if ":" in raw.split("/", 1)[0]:
        return None

    segments = [seg for seg in raw.split("/") if seg not in ("", ".")]
    if not segments or any(seg == ".." for seg in segments):
        return None

    return "/".join(segments)


# ────────────────────────────── 读取句柄 ──────────────────────────────

@dataclass
class ObjectHandle:
    """一次读取的统一视图。

    ``local_path`` 只有本地后端会给。**有就尽量走 ``FileResponse``** ——
    那条路支持 HTTP Range，视频拖进度条才不卡；对象存储后端是 None，
    调用方退回流式转发（前端取的是整段 blob，不影响现有交互）。
    """

    size: int
    local_path: Path | None = None
    _stream: Iterator[bytes] | None = None
    _cleanup: object = None

    def iter_bytes(self) -> Iterator[bytes]:
        if self._stream is not None:
            return self._stream
        if self.local_path is None:
            return iter(())

        # 不能直接把文件对象当迭代器用：那是**按行**迭代的，
        # 二进制里没有 \n 就整块读进内存、有 \n 就被错误切开。
        def _from_file() -> Iterator[bytes]:
            with self.local_path.open("rb") as fh:
                while True:
                    chunk = fh.read(_CHUNK)
                    if not chunk:
                        break
                    yield chunk

        return _from_file()

    def close(self) -> None:
        if self._cleanup is not None:
            try:
                self._cleanup()
            except Exception:  # noqa: BLE001 —— 收尾失败不该盖掉真正的业务结果
                pass
            self._cleanup = None


# ────────────────────────────── 后端接口 ──────────────────────────────

class FileStore(Protocol):
    def save(self, file_path: str, data: bytes, content_type: str | None = None) -> None: ...

    def exists(self, file_path: str) -> bool: ...

    def read_bytes(self, file_path: str) -> bytes | None: ...

    def open(self, file_path: str) -> ObjectHandle | None: ...

    def delete(self, file_path: str) -> tuple[bool, str]: ...


class LocalFileStore:
    """本地目录实现。相对路径拼在 ``settings.STORAGE_DIR`` 下面。"""

    def _resolve(self, file_path: str) -> Path | None:
        key = safe_key(file_path)
        if key is None:
            return None
        root = settings.STORAGE_DIR.resolve()
        target = (root / key).resolve()
        # safe_key 已经挡过 ..，这里是纵深防御：真的解析出来越界就一定拒
        if not target.is_relative_to(root):
            return None
        return target

    def save(self, file_path: str, data: bytes, content_type: str | None = None) -> None:
        # content_type 对本地文件无意义（MIME 由下载接口按文件名现推），仅为接口对齐
        target = self._resolve(file_path)
        if target is None:
            raise ValueError(f"非法的存储路径: {file_path!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def exists(self, file_path: str) -> bool:
        # 与 read_bytes / open 保持同一套错误语义：解析不了的路径是「存储不可用」
        # （503），不是「文件不存在」（404）
        try:
            target = self._resolve(file_path)
            return target is not None and target.is_file()
        except OSError as exc:
            logger.error(
                "检查本地素材是否存在失败 key=%s",
                file_path,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            raise FileStoreUnavailableError() from exc

    def read_bytes(self, file_path: str) -> bytes | None:
        try:
            target = self._resolve(file_path)
        except OSError as exc:
            logger.error(
                "解析本地素材路径失败 key=%s",
                file_path,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            raise FileStoreUnavailableError() from exc
        if target is None:
            return None
        try:
            return target.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.error(
                "读取本地素材文件失败 path=%s",
                target,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            raise FileStoreUnavailableError() from exc

    def open(self, file_path: str) -> ObjectHandle | None:
        try:
            target = self._resolve(file_path)
        except OSError as exc:
            logger.error(
                "解析本地素材路径失败 key=%s",
                file_path,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            raise FileStoreUnavailableError() from exc
        if target is None:
            return None
        try:
            stat_result = target.stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.error(
                "读取本地素材文件信息失败 path=%s",
                target,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            raise FileStoreUnavailableError() from exc
        mode = getattr(stat_result, "st_mode", None)
        if mode is not None and not stat_module.S_ISREG(mode):
            return None
        return ObjectHandle(size=stat_result.st_size, local_path=target)

    def delete(self, file_path: str) -> tuple[bool, str]:
        """尽力而为地删掉素材文件，返回 (是否真的删掉, 失败原因)。

        **文件删不掉绝不能让「删除记录」这个主动作失败** —— 记录才是主体，
        文件清理是附带的。所以这里吞掉一切异常并如实上报，由调用方决定怎么提示用户。

        已知失败来源：
        - 文件被别的进程占用
        - 本环境的 sitecustomize 有 safe-delete 钩子，会给 unlink 套回收站 + 批量删除守卫，
          触发阈值时抛 SystemExit（不是 Exception 的子类，必须显式捕获）
        """
        target = self._resolve(file_path)
        if target is None:
            return False, "路径越界，未做任何文件操作"
        try:
            if not target.exists():
                return False, "文件本就不存在"
            target.unlink()
            return True, ""
        except (OSError, RuntimeError, SystemExit) as exc:
            reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            logger.warning("删除素材文件失败 %s —— %s", target, reason)
            return False, reason

    def prune_empty_dir(self, file_path: str) -> None:
        """素材删干净后顺手收掉因此变空的目录。

        每个任务目录 `{task_id}/`、每个上传目录 `uploads/{asset_id}/` 都只装
        一个源文件（外加一个缩略图），删完必然变空。不收就会在 storage 下攒出
        成百上千个空壳目录（实测一次回归测试就能留下 200+ 个）。
        目录非空时 ``rmdir`` 抛 OSError —— 那正好表示「这里还有别的素材」，忽略即可。
        """
        target = self._resolve(file_path)
        if target is None:
            return
        parent = target.parent
        root = settings.STORAGE_DIR.resolve()
        if parent == root or not parent.is_relative_to(root):
            return
        try:
            parent.rmdir()
        except (OSError, RuntimeError, SystemExit):
            pass


class MinioFileStore:
    """MinIO / S3 兼容对象存储实现。

    ``client`` 可注入（测试用假客户端），不传则按配置懒加载真客户端 ——
    **必须懒加载**：本地模式不该被 ``minio`` 这个依赖绑住，没装也能起服务。
    """

    def __init__(self, client=None):
        self._client = client

    @property
    def bucket(self) -> str:
        return settings.MINIO_BUCKET

    def _c(self):
        if self._client is None:
            try:
                from minio import Minio
            except ImportError as exc:  # pragma: no cover - 取决于环境
                raise RuntimeError(
                    "STORAGE_BACKEND=minio 但未安装 minio 客户端，"
                    "请先 pip install minio，或改回 STORAGE_BACKEND=local"
                ) from exc
            self._client = Minio(
                settings.MINIO_ENDPOINT,
                access_key=settings.MINIO_ACCESS_KEY,
                secret_key=settings.MINIO_SECRET_KEY,
                secure=settings.MINIO_SECURE,
                region=settings.MINIO_REGION or None,
            )
        return self._client

    def save(self, file_path: str, data: bytes, content_type: str | None = None) -> None:
        key = safe_key(file_path)
        if key is None:
            raise ValueError(f"非法的存储路径: {file_path!r}")
        # content_type 存在对象上：日后走预签名直连时浏览器要靠它判断能不能播
        self._c().put_object(
            self.bucket,
            key,
            io.BytesIO(data),
            length=len(data),
            content_type=content_type or "application/octet-stream",
        )

    def exists(self, file_path: str) -> bool:
        key = safe_key(file_path)
        if key is None:
            return False
        try:
            self._c().stat_object(self.bucket, key)
            return True
        except Exception as exc:
            if (
                isinstance(exc, FileNotFoundError)
                or _object_error_code(exc) in _MISSING_OBJECT_CODES
            ):
                return False
            logger.error(
                "检查对象存储素材失败 bucket=%s key=%s code=%s",
                self.bucket,
                key,
                _object_error_code(exc),
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            raise FileStoreUnavailableError() from exc

    def read_bytes(self, file_path: str) -> bytes | None:
        handle = self.open(file_path)
        if handle is None:
            return None
        try:
            return b"".join(handle.iter_bytes())
        except Exception as exc:
            if (
                isinstance(exc, FileNotFoundError)
                or _object_error_code(exc) in _MISSING_OBJECT_CODES
            ):
                return None
            logger.error(
                "读取对象存储素材失败 bucket=%s key=%s",
                self.bucket,
                safe_key(file_path) or "<invalid>",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            raise FileStoreUnavailableError() from exc
        finally:
            handle.close()

    def open(self, file_path: str) -> ObjectHandle | None:
        key = safe_key(file_path)
        if key is None:
            return None
        try:
            client = self._c()
            stat = client.stat_object(self.bucket, key)
        except Exception as exc:
            if (
                isinstance(exc, FileNotFoundError)
                or _object_error_code(exc) in _MISSING_OBJECT_CODES
            ):
                return None
            logger.error(
                "读取对象存储素材信息失败 bucket=%s key=%s code=%s",
                self.bucket,
                key,
                _object_error_code(exc) or type(exc).__name__,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            raise FileStoreUnavailableError() from exc

        resp = None

        def _cleanup():
            if resp is not None:
                try:
                    resp.close()
                    resp.release_conn()
                except Exception:  # noqa: BLE001
                    pass

        def _iter() -> Iterator[bytes]:
            nonlocal resp
            try:
                resp = client.get_object(self.bucket, key)
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    yield chunk
            finally:
                _cleanup()

        return ObjectHandle(
            size=int(getattr(stat, "size", 0) or 0),
            local_path=None,
            _stream=_iter(),
            _cleanup=_cleanup,
        )

    def delete(self, file_path: str) -> tuple[bool, str]:
        """与本地实现同语义：对象本来就没有 → (False, "文件本就不存在")。

        ``remove_object`` 对不存在的键也是成功返回，所以先 stat 再删。
        """
        key = safe_key(file_path)
        if key is None:
            return False, "路径越界，未做任何文件操作"
        try:
            client = self._c()
            client.stat_object(self.bucket, key)
        except Exception as exc:
            if (
                isinstance(exc, FileNotFoundError)
                or _object_error_code(exc) in _MISSING_OBJECT_CODES
            ):
                return False, "文件本就不存在"
            reason = _object_error_reason(exc)
            logger.warning(
                "检查对象存储素材失败 %s/%s —— %s",
                self.bucket,
                key,
                reason,
            )
            return False, reason
        try:
            client.remove_object(self.bucket, key)
            return True, ""
        except Exception as exc:
            if (
                isinstance(exc, FileNotFoundError)
                or _object_error_code(exc) in _MISSING_OBJECT_CODES
            ):
                return False, "文件本就不存在"
            reason = _object_error_reason(exc)
            logger.warning("删除对象存储素材失败 %s/%s —— %s", self.bucket, key, reason)
            return False, reason


# ────────────────────────────── 取用入口 ──────────────────────────────

_store: FileStore | None = None


def get_store() -> FileStore:
    """按配置返回当前存储后端。**进程内缓存**，改配置要重启进程才生效。"""
    global _store
    if _store is None:
        backend = (settings.STORAGE_BACKEND or "local").strip().lower()
        if backend == "local":
            _store = LocalFileStore()
        elif backend == "minio":
            _store = MinioFileStore()
        else:
            raise ValueError(
                f"未知的 STORAGE_BACKEND: {settings.STORAGE_BACKEND!r}（支持 local / minio）"
            )
    return _store


def reset_store() -> None:
    """清掉缓存的后端实例（测试或热切换配置时用）。"""
    global _store
    _store = None


# 模块级便捷函数 —— 业务代码只认这几个名字，不直接摸 get_store()

def save_asset_file(file_path: str, data: bytes, content_type: str | None = None) -> None:
    get_store().save(file_path, data, content_type)


def asset_exists(file_path: str) -> bool:
    return get_store().exists(file_path)


def read_asset_bytes(file_path: str) -> bytes | None:
    return get_store().read_bytes(file_path)


def open_asset(file_path: str) -> ObjectHandle | None:
    return get_store().open(file_path)


def delete_asset_file(file_path: str) -> tuple[bool, str]:
    """尽力而为地删掉素材文件。返回 (是否真的删掉, 失败原因)。"""
    return get_store().delete(file_path)


def prune_empty_asset_dir(file_path: str) -> None:
    """素材文件都删完后，清掉因此变空的目录（尽力而为，不空就什么也不做）。

    对象存储没有目录概念，后端没实现这个方法就直接跳过。
    """
    prune = getattr(get_store(), "prune_empty_dir", None)
    if prune is None:
        return
    try:
        prune(file_path)
    except (OSError, RuntimeError, SystemExit) as exc:  # noqa: BLE001 - 收尾失败不该外溢
        logger.warning("清理空素材目录失败 key=%s —— %s", file_path, exc)
