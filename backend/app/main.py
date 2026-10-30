"""FastAPI 应用入口"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.config import settings
from app.database import init_db, async_session
from app.api import templates, tasks, health, assets, auth_api, uploads, stats, plugins


async def create_default_admin():
    """创建默认管理员（如果不存在）"""
    import secrets
    from sqlalchemy import select
    from app.models.models import User
    from app.auth import hash_password

    async with async_session() as db:
        result = await db.execute(select(User).where(User.username == "admin"))
        if not result.scalar_one_or_none():
            # 初始密码：优先取环境变量；未配置则生成随机密码并打印一次
            password = settings.ADMIN_INITIAL_PASSWORD
            generated = False
            if not password:
                password = secrets.token_urlsafe(12)
                generated = True
            admin = User(
                username="admin",
                password_hash=hash_password(password),
                display_name="管理员",
                role="admin",
            )
            db.add(admin)
            await db.commit()
            if generated:
                print(f"[Auth] Default admin created. username=admin password={password} （仅显示一次，请立即登录后修改）")
            else:
                print("[Auth] Default admin created with ADMIN_INITIAL_PASSWORD from config")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时初始化数据库 + 默认管理员 + 引擎插件 + 收拾上个进程遗留的中断任务
    await init_db()
    await create_default_admin()
    # 插件恢复要在任务收割之前 —— 收割会 resume 遗留任务，得先有引擎可用
    from app.api.plugins import restore_plugins
    async with async_session() as db:
        plugin_results = await restore_plugins(db)
    loaded = [p["name"] for p in plugin_results if p["ok"]]
    failed = [p["file"] for p in plugin_results if not p["ok"]]
    if loaded or failed:
        print(f"[Plugin] 引擎插件恢复：加载 {loaded or '无'}，失败 {failed or '无'}", flush=True)
    from app.services.task_service import TaskService
    # 有 prompt_id 的接回去继续跑（别让 5 分钟的视频白跑），没提交成的标成可重试
    reaped = await TaskService.reap_stale_tasks()
    if reaped["resumed"] or reaped["failed"]:
        print(
            f"[Task] 启动收拾遗留任务：接回 {len(reaped['resumed'])} 条"
            f"（继续等引擎出结果），标记失败 {len(reaped['failed'])} 条",
            flush=True,  # 启动日志要立刻可见：stdout 重定向到文件时是块缓冲，不刷就查不到
        )
    print(f"[{settings.APP_NAME}] v{settings.APP_VERSION} started", flush=True)
    yield
    # 关闭时清理
    print("Shutting down...")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

# CORS — 允许前端开发服务器访问（生产环境通过 CORS_ORIGINS 收紧）
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(health.router)
app.include_router(auth_api.router)
app.include_router(templates.router)
app.include_router(tasks.router)
app.include_router(assets.router)
app.include_router(uploads.router)
app.include_router(stats.router)
app.include_router(plugins.router)


@app.get("/api/info")
async def app_info():
    """应用信息（原 / 路由；/ 现在让给前端页面）"""
    return {"app": settings.APP_NAME, "version": settings.APP_VERSION}


# ── 前端静态托管（单进程部署，免 nginx）─────────────────
# 注意：这些路由必须放在 /api/* 之后注册 —— Starlette 按注册顺序匹配，
# 已注册的 API 路由优先命中，此兜底只接管非 API 路径。
_DIST = Path(settings.FRONTEND_DIST)


@app.get("/{full_path:path}")
async def spa(full_path: str):
    """托管前端构建产物；未命中的路径回落到 index.html

    前端是纯 tab 切换、无客户端路由，因此不需要深度的 history 兜底，
    但保留回落以防将来引入路由。
    """
    if full_path.startswith("api/"):
        # 未知 API 路径必须是 404 JSON，不能吞成 HTML 页面
        raise HTTPException(status_code=404, detail="Not Found")
    if not _DIST.exists():
        raise HTTPException(
            status_code=503,
            detail=f"前端未构建：找不到 {_DIST}，请先在 frontend/ 执行 npm run build",
        )
    candidate = (_DIST / full_path).resolve()
    # 防目录穿越：确保解析后仍在 dist 目录内
    if full_path and candidate.is_file() and candidate.is_relative_to(_DIST):
        return FileResponse(candidate)
    return FileResponse(_DIST / "index.html")
