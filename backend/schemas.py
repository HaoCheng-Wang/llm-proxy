from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, List
from datetime import datetime


# ---- Auth ----
class UserRegister(BaseModel):
    username: str = Field(..., min_length=2, max_length=50)
    password: str = Field(..., min_length=4, max_length=100)


class UserLogin(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=100)
    new_password: str = Field(..., min_length=1, max_length=100)


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

    model_config = ConfigDict(from_attributes=True)


# ---- Port ----
class PortCreate(BaseModel):
    target_url: str = Field(..., min_length=5, max_length=500)
    description: str = Field(default="", max_length=200)
    prefer_http2: Optional[bool] = None  # None=HTTP/1.1, set later on edit
    api_key: Optional[str] = Field(None, max_length=500)  # None=pass-through, set=override


class PortUpdate(BaseModel):
    """Fields that can be edited on an existing port.

    All fields optional — only provided fields are updated.
    Changing port_number requires the new port to be free.
    """
    port_number: Optional[int] = None
    target_url: Optional[str] = Field(None, min_length=5, max_length=500)
    description: Optional[str] = Field(None, max_length=200)
    prefer_http2: Optional[bool] = None  # None=don't change, False/True=set
    api_key: Optional[str] = Field(None, max_length=500)  # None=don't change, ""=clear, set=override


class PortInfo(BaseModel):
    id: int
    port_number: int
    target_url: str
    description: str
    is_active: bool
    prefer_http2: Optional[bool] = None  # None/False=HTTP/1.1, True=HTTP/2
    api_key: Optional[str] = None  # None=pass-through, set=override agent's key
    deleted_at: Optional[datetime] = None
    created_at: datetime
    request_count: int = 0
    username: str = ""  # creator username (visible to admin)

    model_config = ConfigDict(from_attributes=True)


# ---- Request ----
class RequestInfo(BaseModel):
    id: int
    port_id: Optional[int] = None
    method: str
    path: str
    request_headers: Optional[str]
    request_body: Optional[str]
    response_headers: Optional[str]
    response_body: Optional[str]
    response_body_raw: Optional[str] = None
    status_code: Optional[int]
    duration_ms: Optional[int]
    reconstruction_error: bool = False
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PortHistory(BaseModel):
    port: PortInfo
    requests: List[RequestInfo]


# ---- Admin ----
class UserApproval(BaseModel):
    user_id: int
    is_approved: bool


class AdminUserList(BaseModel):
    users: List[UserInfo]


class DeletedPortInfo(PortInfo):
    """Extended port info for admin's deleted-ports view."""
    creator_username: str = ""


class DeletedPortList(BaseModel):
    ports: List[PortInfo]
