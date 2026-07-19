from fastapi import APIRouter

router = APIRouter(prefix="/health", tags=["Health"])
# /health endpoint is the prefix for all routes in this router. The tags parameter  Health is used to group the endpoints in the API documentation.

@router.get("")
async def health():
    return {
        "status": "ok",
        "service": "Nexa Wallet API"
    }