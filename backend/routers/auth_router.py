from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from database import get_db
from models import User
from schemas import UserRegister, UserLogin, TokenResponse, UserInfo
from auth import hash_password, verify_password, create_access_token, get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=dict)
def register(data: UserRegister, db: Session = Depends(get_db)):
    """Register a new user. Requires admin approval before use."""
    existing = db.query(User).filter(User.username == data.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists")

    user = User(
        username=data.username,
        password_hash=hash_password(data.password),
        role="user",
        is_approved=False,
    )
    db.add(user)
    db.commit()
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
