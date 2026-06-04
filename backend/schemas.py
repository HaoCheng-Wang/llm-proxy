from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


# ---- Auth ----
class UserRegister(BaseModel):
    username: str = Field(..., min_length=2, max_length=50)
    password: str = Field(..., min_length=4, max_length=100)


class UserLogin(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    role: str
    user_id: int


class UserInfo(BaseModel):
    id: int
    username: str
    role: str
    is_approved: bool
    created_at: datetime

    class Config:
        from_attributes = True


# ---- Port ----
class PortCreate(BaseModel):
    target_url: str = Field(..., min_length=5, max_length=500)
    description: str = Field(default="", max_length=200)


class PortInfo(BaseModel):
    id: int
    port_number: int
    target_url: str
    description: str
    is_active: bool
    created_at: datetime
    request_count: int = 0
    username: str = ""  # creator username (visible to admin)

    class Config:
        from_attributes = True


# ---- Request ----
class RequestInfo(BaseModel):
    id: int
    port_id: int
    method: str
    path: str
    request_headers: Optional[str]
    request_body: Optional[str]
    response_headers: Optional[str]
    response_body: Optional[str]
    response_body_raw: Optional[str] = None
    status_code: Optional[int]
    duration_ms: Optional[int]
    created_at: datetime

    class Config:
        from_attributes = True


class PortHistory(BaseModel):
    port: PortInfo
    requests: List[RequestInfo]


# ---- Admin ----
class UserApproval(BaseModel):
    user_id: int
    is_approved: bool


class AdminUserList(BaseModel):
    users: List[UserInfo]
