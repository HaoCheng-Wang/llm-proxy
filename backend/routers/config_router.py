from fastapi import APIRouter
from config import DISPLAY_IP

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("")
def get_config():
    """Return static config values (display IP from config.py)."""
    return {
        "display_ip": DISPLAY_IP,
    }
