"""认证模块 — 密码哈希、JWT、权限依赖"""
import secrets
from datetime import timedelta
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.models import User
from app.timeutils import utcnow

# 配置 — SECRET_KEY 从环境变量读取，未配置时生成临时密钥（重启后已有 token 失效）
SECRET_KEY = settings.SECRET_KEY
if not SECRET_KEY:
    SECRET_KEY = secrets.token_urlsafe(32)
    print("[Auth] WARNING: SECRET_KEY 未配置，已生成临时密钥。生产环境请在 .env 中设置 SECRET_KEY！")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()


# ── 密码工具 ──────────────────────────────

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ── JWT 工具 ──────────────────────────────

def create_token(user: User) -> str:
    payload = {
        "sub": user.id,
        "username": user.username,
        "role": user.role,
        # 版本号随改密递增：JWT 无状态，靠它让旧 token 在改密后立即失效
        "ver": int(getattr(user, "token_version", 0) or 0),
        "exp": utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def token_version_matches(payload: dict, user: User) -> bool:
    """校验 token 里的版本号是否仍是当前用户的有效版本。

    老 token（改密功能上线前签发）没有 ver 字段，视为版本 0；用户一旦改密
    token_version 变为 >=1，这些老 token 立即失效。"""
    token_ver = int(payload.get("ver", 0) or 0)
    user_ver = int(getattr(user, "token_version", 0) or 0)
    return token_ver == user_ver


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )


# ── 权限依赖 ──────────────────────────────

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    """从 JWT 中解析当前用户"""
    payload = decode_token(credentials.credentials)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    user = await db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    # 改密/重置后旧 token 立即失效（版本号对不上）
    if not token_version_matches(payload, user):
        raise HTTPException(status_code=401, detail="Token has been revoked")
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """要求管理员权限"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin permission required")
    return user
