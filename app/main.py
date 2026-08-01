from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.database_init import init_db
from app.api.health import router as health_router
from app.core.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()

    yield

     # Shutdown
     # cleanup resources later



# Create FastAPI app and include routers
app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(health_router)
