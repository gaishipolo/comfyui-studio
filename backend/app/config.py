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
    # 是否让连 ComfyUI 的 HTTP 客户端继承系统代理（httpx trust_env）。
    # 默认关闭：ComfyUI 几乎总是局域网/本机地址，走代理没意义还会出事 ——
    # 本机装了 Clash 之类的代理软件时（Windows 下从注册表读代理设置），
    # 发往内网的请求会被代理接管；而代理端口是随机且会随代理重启变化的，
    # 进程内缓存的 HTTP 客户端会一直指向旧端口，表现就是「明明能 ping 通
    # ComfyUI，平台却一直显示离线，且只有重启后端才能恢复」。
    # 只有 ComfyUI 在需要代理才能访问的网络上时，才把它设为 true。
    COMFYUI_TRUST_ENV: bool = False

    # 文件存储
    # 后端可切换：local = 本机目录（默认）| minio = MinIO / S3 兼容对象存储。
    # 换后端只改这一个值，业务代码不感知（见 app/filestore.py）。
    STORAGE_BACKEND: str = "local"
    STORAGE_DIR: Path = Path("./storage").resolve()
    WORKFLOW_DIR: Path = Path("./workflows").resolve()
    FFMPEG_PATH: str = ""
    # 前端构建产物目录；存在则由后端直接托管，实现单进程部署（免 nginx）
    FRONTEND_DIST: Path = Path("../frontend/dist").resolve()
    # 素材回收站保留天数。软删除的素材超过这个天数，启动时会被彻底清理
    # （记录 + 磁盘文件）。0 = 关闭自动清理，只能手动清空回收站。
    TRASH_RETENTION_DAYS: int = 30
    # 新用户的默认存储配额（GB）。0 = 不限。
    #
    # 不设默认值等于没有防线：50 人 × 每天几十张 2MB 的图，一个月几十 GB，
    # 磁盘写满时是**整站故障**（所有上传 + 所有生成都失败），不是某个人受影响。
    # 只作用于新建用户，存量用户不动 —— 别在升级时把人的上传突然掐了。
    DEFAULT_STORAGE_QUOTA_GB: float = 20.0

    # MinIO / S3 兼容对象存储（STORAGE_BACKEND=minio 时生效）
    MINIO_ENDPOINT: str = "127.0.0.1:9000"
    MINIO_ACCESS_KEY: str = ""
    MINIO_SECRET_KEY: str = ""
    MINIO_BUCKET: str = "comfyui-workbench"
    MINIO_SECURE: bool = False
    MINIO_REGION: str = ""

    # ── 流程编排 Agent（LLM）──
    # 只走 OpenAI 兼容的 /chat/completions 协议，不绑厂商：OpenAI、DeepSeek、
    # 通义、Kimi、本地 vLLM / Ollama 的兼容层都能填。留空 = 未启用，
    # 界面会提示改用人工编排，不会静默失败。
    #
    # **为什么不把模型配置放进数据库**：这是系统级凭据，不是某个人的偏好；
    # 放 .env 由部署方管，不会随备份/导出泄露，也不用为「谁能改模型」设计权限。
    LLM_BASE_URL: str = ""  # 如 https://api.deepseek.com 或 http://127.0.0.1:11434/v1
    LLM_API_KEY: str = ""  # 本地模型可留空
    LLM_MODEL: str = ""  # 如 deepseek-chat / gpt-4o-mini / qwen2.5:14b
    LLM_TIMEOUT: float = 90.0  # 单次生成超时（秒）。生成长 JSON 比聊天慢，别设太小
    # 校验不过时把错误回喂给模型自修的次数。3 次足够：再多是在烧钱，
    # 而且说明模型压根没理解规格。
    LLM_MAX_ATTEMPTS: int = 3
    # 访问外部模型是否继承系统代理。国内访问 OpenAI 之类通常需要代理，
    # 与 ComfyUI 的内网地址正好相反，所以这里默认开启。
    LLM_TRUST_ENV: bool = True

    # 任务
    MAX_CONCURRENT_TASKS: int = 2
    # 重启时把「排队中、还没提交到引擎」的任务重新排一遍。
    # 关掉它，重启会把这些任务标成失败让用户手动重试 —— 只在「重提可能造成
    # 重复计费」的引擎上才需要关。
    RESUME_PENDING_ON_STARTUP: bool = True
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
