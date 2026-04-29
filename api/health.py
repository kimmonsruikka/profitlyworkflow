from datetime import datetime, timezone

from fastapi import APIRouter

from config.settings import settings

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "environment": settings.ENVIRONMENT,
        "broker_mode": settings.BROKER_MODE,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
