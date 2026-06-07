from fastapi import APIRouter
from config import DISPLAY_IP, API_PORT

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("")
def get_config():
    """Return static config values for the frontend."""
    return {
        "display_ip": DISPLAY_IP,
        "api_port": API_PORT,
    }
