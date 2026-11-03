"""应用配置"""
from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    # 应用
    APP_NAME: str = "ComfyUI Workbench"
    APP_VERSION: str = "0.1.0"
    APP_TIMEZONE: str = "Asia/Shanghai"  # 用户输入的纯日期按此时区解释，数据库仍存 naive UTC
    # 运行环境：development | production。
    # 只有 production 会在启动时对缺失的关键配置**拒绝启动**（见 app/preflight.py）；
    # development 一律降级为警告，避免本地开发被生产要求卡住。
    APP_ENV: str = "development"

    # 安全
    SECRET_KEY: str = ""  # JWT 签名密钥，生产环境必须配置；留空则启动时临时生成（重启后 token 失效）
    ADMIN_INITIAL_PASSWORD: str = ""  # 首次启动创建默认管理员的密码；留空则生成随机密码并打印一次
    CORS_ORIGINS: str = "*"  # 逗号分隔的允许来源，生产环境收紧为前端域名
    SQL_ECHO: bool = False  # 是否打印 SQL 日志

    # 数据库
    DATABASE_URL: str = "sqlite+aiosqlite:///./workbench.db"

    # ComfyUI
    COMFYUI_BASE_URL: str = "http://127.0.0.1:8188"
    COMFYUI_TIMEOUT: int = 300  # 生成超时（秒）

    # 文件存储
    # 后端可切换：local = 本机目录（默认）| minio = MinIO / S3 兼容对象存储。
    # 换后端只改这一个值，业务代码不感知（见 app/filestore.py）。
    STORAGE_BACKEND: str = "local"
    STORAGE_DIR: Path = Path("./storage").resolve()
    WORKFLOW_DIR: Path = Path("./workflows").resolve()
    FFMPEG_PATH: str = ""
    # 前端构建产物目录；存在则由后端直接托管，实现单进程部署（免 nginx）
    FRONTEND_DIST: Path = Path("../frontend/dist").resolve()

    # MinIO / S3 兼容对象存储（STORAGE_BACKEND=minio 时生效）
    MINIO_ENDPOINT: str = "127.0.0.1:9000"
    MINIO_ACCESS_KEY: str = ""
    MINIO_SECRET_KEY: str = ""
    MINIO_BUCKET: str = "comfyui-workbench"
    MINIO_SECURE: bool = False
    MINIO_REGION: str = ""

    # 任务
    MAX_CONCURRENT_TASKS: int = 2
    TASK_POLL_INTERVAL: float = 2.0
    # 单个任务从提交到出结果的最长等待秒数。超时会向引擎发取消，
    # 别让跑飞的 prompt 继续占显存。LTX 121 帧实测约 310 秒，
    # 视频拉长/分辨率提高后很容易破 10 分钟，默认给 30 分钟。
    TASK_TIMEOUT_SECONDS: int = 1800

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        # 允许旧 .env 保留已删除键（如 DEBUG），避免升级后仅因废弃配置无法启动。
        extra = "ignore"


settings = Settings()

# 确保存储目录存在
settings.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
