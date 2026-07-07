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

@app.get("/test-webhook")
async def test_webhook():
    return {"status": "received"}
    
@app.get("/debug-webhook-token")
async def debug_webhook_token(
    hub_mode: str = None,
    hub_challenge: str = None,
    hub_verify_token: str = None,
):
    """
    TEMPORARY DEBUG ENDPOINT — DELETE AFTER FIXING
    Shows exactly what Meta (or your browser) is sending.
    """
    import os
    actual_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "NOT_SET")
    
    return {
        "received_from_request": {
            "hub_mode": hub_mode,
            "hub_challenge": hub_challenge,
            "hub_verify_token": hub_verify_token,
            "token_length": len(hub_verify_token) if hub_verify_token else 0,
            "token_repr": repr(hub_verify_token),  # Shows hidden chars like spaces
        },
        "loaded_from_env": {
            "WHATSAPP_VERIFY_TOKEN": actual_token,
            "token_length": len(actual_token),
            "token_repr": repr(actual_token),
        },
        "match": hub_verify_token == actual_token if hub_verify_token else False,
    }