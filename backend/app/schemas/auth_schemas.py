"""认证相关 Schema"""
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=6, max_length=128)
    display_name: str = ""
    role: Literal["admin", "member"] = "member"


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    """改自己的密码 —— 必须提供当前密码，防止别人捡到未退出的会话就改掉账号"""
    old_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=6, max_length=128)


class ResetPasswordRequest(BaseModel):
    """管理员重置他人密码 —— 不需要对方当前密码"""
    new_password: str = Field(..., min_length=6, max_length=128)


class UserUpdateRequest(BaseModel):
    """管理员调整账号：角色 / 启用停用 / 显示名。留空的字段不改"""
    role: Literal["admin", "member"] | None = None
    is_active: bool | None = None
    display_name: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "UserResponse"


class UserResponse(BaseModel):
    id: str
    username: str
    display_name: str
    role: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True
