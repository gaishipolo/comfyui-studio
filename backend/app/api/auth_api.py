"""认证 API — 注册、登录、用户信息"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.models import User
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
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse)
async def login(data: LoginRequest, db: AsyncSession = Depends(get_db)):
    """登录，返回 JWT"""
    result = await db.execute(select(User).where(User.username == data.username))
    user = result.scalar_one_or_none()

    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")

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
    """管理员调整账号：角色 / 启用停用 / 显示名。

    有一条硬规则：**不能把自己关掉或把自己降级**。
    否则最后一个 admin 一点「停用」就再也登不进来了，只能去改数据库。
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

    await db.commit()
    await db.refresh(user)
    return user
