from fastapi import APIRouter
from app.core.redis import redis_client
router = APIRouter(prefix="/health", tags=["Health"])
# /health endpoint is the prefix for all routes in this router. The tags parameter  Health is used to group the endpoints in the API documentation.

@router.get("")
async def health():
    return {
        "status": "ok",
        "service": "Nexa Wallet API"
    }

@router.get("/health/redis")
async def redis_health():
    return {
        "redis": await redis_client.ping()
    }