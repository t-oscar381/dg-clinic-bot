"""
DG Clinic — WhatsApp Doctor Assistant
FastAPI Application Entry Point
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routes.webhook import router as webhook_router
from app.config import get_settings

settings = get_settings()

app = FastAPI(
    title="DG Clinic WhatsApp Bot",
    description="Private AI assistant for the head doctor",
    version="1.0.0",
    docs_url="/docs" if settings.DEBUG else None,   # Hide docs in production
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://graph.facebook.com"],   # Only Meta's servers
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(webhook_router)


@app.get("/")
async def root():
    return {"status": "DG Clinic bot is running", "clinic": settings.CLINIC_NAME}


@app.get("/health")
async def health():
    """Railway health check endpoint."""
    return {"status": "ok"}
