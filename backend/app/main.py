from fastapi import FastAPI

app = FastAPI(title="Rehearse API", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "rehearse-api"}
