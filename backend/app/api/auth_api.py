"""认证 API — 注册、登录、用户信息"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.models import User
from app import login_guard
from app.quota import default_quota_bytes
from app.schemas.auth_schemas import (
    RegisterRequest, LoginRequest, TokenResponse, UserResponse,
    ChangePasswordRequest, ResetPasswordRequest, UserUpdateRequest,
)
from app.auth import (
    hash_password, verify_password, create_token,
    get_current_user, require_admin,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=UserResponse)
async def register(
    data: RegisterRequest,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """注册用户（仅 admin 可创建账户）"""
    existing = await db.execute(select(User).where(User.username == data.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Username already exists")

    user = User(
        username=data.username,
        password_hash=hash_password(data.password),
        display_name=data.display_name or data.username,
        role=data.role,
        # 默认配额：不设就永远是 0（不限），磁盘写满是整站故障而不是个人问题。
        # 管理员在想调整时再改，见 UserUpdateRequest.storage_quota_bytes。
        storage_quota_bytes=default_quota_bytes(),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse)
async def login(data: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """登录，返回 JWT"""
    # 暴力破解防护：连续失败锁定（见 app/login_guard.py）
    key = login_guard.login_key(data.username, request)
    if login_guard.limiter.is_locked(key):
        raise HTTPException(status_code=429, detail="登录失败次数过多，请稍后再试")

    result = await db.execute(select(User).where(User.username == data.username))
    user = result.scalar_one_or_none()

    if not user or not verify_password(data.password, user.password_hash):
        login_guard.limiter.record_failure(key)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if not user.is_active:
        # 停用账号也计入失败：否则可用错误密码反复探测哪些账号被停用
        login_guard.limiter.record_failure(key)
        raise HTTPException(status_code=403, detail="Account disabled")

    login_guard.limiter.record_success(key)
    token = create_token(user)
    return TokenResponse(
        access_token=token,
        user=UserResponse.model_validate(user),
    )


@router.get("/me", response_model=UserResponse)
async def get_me(user: User = Depends(get_current_user)):
    """获取当前登录用户信息"""
    return user


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """用户列表（仅 admin）"""
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    return result.scalars().all()


# ── 账号管理 ──────────────────────────────
# 起因：admin 初始密码是启动时打到控制台的随机串，而此前**没有任何改密接口**
# （auth 只有 register/login/me/users），等于密码永远改不掉。下面三个接口补上这块。

@router.post("/change-password", response_model=UserResponse)
async def change_own_password(
    data: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """改自己的密码。必须提供当前密码。"""
    if not verify_password(data.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="当前密码不正确")
    if data.old_password == data.new_password:
        raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")

    user.password_hash = hash_password(data.new_password)
    user.token_version = int(user.token_version or 0) + 1  # 吊销旧 token（含当前会话）
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/users/{user_id}/reset-password", response_model=UserResponse)
async def reset_user_password(
    user_id: str,
    data: ResetPasswordRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """管理员重置他人密码（忘记密码时用）"""
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.password_hash = hash_password(data.new_password)
    user.token_version = int(user.token_version or 0) + 1  # 被盗账号重置后旧 token 立即失效
    await db.commit()
    await db.refresh(user)
    return user


@router.patch("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    data: UserUpdateRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """管理员调整账号：角色 / 启用停用 / 显示名 / 存储配额。

    有一条硬规则：**不能把自己关掉或把自己降级**。
    否则最后一个 admin 一点「停用」就再也登不进来了，只能去改数据库。

    配额只对普通用户生效：管理员豁免准入检查（否则他连进管理页调配额都要
    先删素材）。给管理员设的值会存下来，但不会拦他。
    """
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    is_self = user.id == admin.id
    if is_self and data.is_active is False:
        raise HTTPException(status_code=400, detail="不能停用自己的账号")
    if is_self and data.role is not None and data.role != "admin":
        raise HTTPException(status_code=400, detail="不能取消自己的管理员身份")

    # 别把最后一个管理员降级/停用 —— 否则系统就没有能登录的管理员了
    if (
        user.role == "admin"
        and (data.role == "member" or data.is_active is False)
    ):
        remaining = await db.execute(
            select(User).where(User.role == "admin", User.is_active.is_(True))
        )
        if len(remaining.scalars().all()) <= 1:
            raise HTTPException(status_code=400, detail="系统必须保留至少一个启用的管理员")

    if data.role is not None:
        user.role = data.role
    if data.is_active is not None:
        user.is_active = data.is_active
    if data.display_name is not None:
        user.display_name = data.display_name
    if data.storage_quota_bytes is not None:
        user.storage_quota_bytes = data.storage_quota_bytes

    await db.commit()
    await db.refresh(user)
    return user
