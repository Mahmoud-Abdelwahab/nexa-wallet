from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.api.health import router as health_router
from app.api.authentication import router as auth_router
from app.core.config import settings


# Create FastAPI app and include routers
app = FastAPI(
    title=settings.app_name,
    version="1.0.0"
)

app.include_router(health_router)
app.include_router(auth_router)
