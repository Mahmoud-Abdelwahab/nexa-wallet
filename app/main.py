from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.api.health import router as health_router
from app.api.authentication import router as auth_router
from app.api.users import router as users_router
from app.core.config import settings

from app.core.redis import redis_client

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await redis_client.aclose()


# Create FastAPI app and include routers
app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
    version="1.0.0"
)

app.include_router(health_router)
app.include_router(auth_router)
app.include_router(users_router)
