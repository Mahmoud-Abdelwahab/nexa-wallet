from fastapi import FastAPI

from app.api.health import router as health_router

# Create FastAPI app and include routers
app = FastAPI(title="Nexa Wallet API")

app.include_router(health_router)