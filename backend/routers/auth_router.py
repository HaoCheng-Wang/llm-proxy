from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from database import get_db
from models import User
from schemas import UserRegister, UserLogin, ChangePasswordRequest, TokenResponse, UserInfo
from auth import hash_password, verify_password, create_access_token, get_current_user
from config import REQUIRE_APPROVAL

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=dict)
def register(data: UserRegister, db: Session = Depends(get_db)):
    """Register a new user. Approval depends on REQUIRE_APPROVAL env var."""
    existing = db.query(User).filter(User.username == data.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists")

    auto_approve = not REQUIRE_APPROVAL
    user = User(
        username=data.username,
        password_hash=hash_password(data.password),
        role="user",
        is_approved=auto_approve,
    )
    db.add(user)
    db.commit()

    if auto_approve:
        return {"message": "Registration successful. You can log in now."}
    return {"message": "Registration submitted. Please wait for admin approval."}


@router.post("/login", response_model=TokenResponse)
def login(data: UserLogin, db: Session = Depends(get_db)):
    """Login with username and password."""
    user = db.query(User).filter(User.username == data.username).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")

    if not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    if not user.is_approved and user.role != "admin":
        raise HTTPException(status_code=403, detail="Account not yet approved. Please wait for admin approval.")

    token = create_access_token({"user_id": user.id, "role": user.role})
    return TokenResponse(
        access_token=token,
        username=user.username,
        role=user.role,
        user_id=user.id,
    )


@router.get("/me", response_model=UserInfo)
def get_me(current_user: User = Depends(get_current_user)):
    """Get current logged-in user info."""
    return current_user


@router.post("/change-password", response_model=dict)
def change_password(data: ChangePasswordRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Change password for the currently logged-in user.

    Requires the current password to verify identity, then sets a new password.
    """
    if not verify_password(data.old_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    if data.old_password == data.new_password:
        raise HTTPException(status_code=400, detail="New password must be different from current password")

    current_user.password_hash = hash_password(data.new_password)
    db.commit()
    return {"message": "Password changed successfully"}
