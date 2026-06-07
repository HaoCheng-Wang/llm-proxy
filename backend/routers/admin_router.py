from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from models import User
from schemas import UserApproval, UserInfo, AdminUserList
from auth import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/users", response_model=AdminUserList)
def list_users(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """List all users (admin only)."""
    users = db.query(User).order_by(User.created_at.desc()).all()
    return AdminUserList(users=[
        UserInfo(
            id=u.id,
            username=u.username,
            role=u.role,
            is_approved=u.is_approved,
            created_at=u.created_at,
        ) for u in users
    ])


@router.put("/users/approve", response_model=dict)
def approve_user(data: UserApproval, admin: User = Depends(require_admin),
                 db: Session = Depends(get_db)):
    """Approve or reject a user (admin only)."""
    user = db.query(User).filter(User.id == data.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == "admin":
        raise HTTPException(status_code=400, detail="Cannot modify admin status")

    user.is_approved = data.is_approved
    db.commit()
    action = "approved" if data.is_approved else "rejected"
    return {"message": f"User '{user.username}' has been {action}."}


@router.delete("/users/{user_id}", response_model=dict)
def delete_user(user_id: int, admin: User = Depends(require_admin),
                db: Session = Depends(get_db)):
    """Delete a user (admin only)."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == "admin":
        raise HTTPException(status_code=400, detail="Cannot delete admin")

    username = user.username
    db.delete(user)
    db.commit()
    return {"message": f"User '{username}' has been deleted."}
