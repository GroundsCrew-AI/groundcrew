from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

VERSION = "0.1.0"

app = FastAPI(title="Groundcrew API", version=VERSION)

# Local dev only. Tighten to the hosted web origin in Phase 6.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


class Health(BaseModel):
    status: str
    version: str


@app.get("/health")
def health() -> Health:
    return Health(status="ok", version=VERSION)
