from fastapi import FastAPI

from .api import router

app = FastAPI(
    title="Memory Governor",
    description="Governance control plane for GBrain durable memory writes.",
    version="0.1.0",
)

app.include_router(router)
