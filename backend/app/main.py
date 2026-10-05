from fastapi import FastAPI

from app.session_routes import router as session_router
from app.history_routes import router as history_router

app = FastAPI(title="Rehearse API", docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(session_router)
app.include_router(history_router)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "rehearse-api"}
