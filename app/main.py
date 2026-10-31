from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from app.api.error_handlers import internal_error_handler, validation_error_handler
from app.api.routes.health import router as health_router
from app.api.routes.authentication import router as auth_router
from app.api.routes.users import router as users_router
from app.api.routes.wallet import router as wallet_router
from app.api.routes.transfer import router as transfer_router
from app.api.routes.transaction import router as transaction_router

from app.core.config import settings

from app.core.redis import redis_client

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await redis_client.aclose()


# Create FastAPI app and include routers
app = FastAPI(
    debug=False,  # Never send tracebacks to API clients.
    title=settings.app_name,
    lifespan=lifespan,
    version="1.0.0"
)

app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(Exception, internal_error_handler)

app.include_router(health_router)
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(wallet_router)
app.include_router(transfer_router)
app.include_router(transaction_router)
