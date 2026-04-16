from typing import Any
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from .config import PRIMARY
from .import_urls_batch import run_import_payload
from .db_factory import test_connection, session_scope
from sqlalchemy import text

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"chrome-extension://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

IMPORT_TARGET = PRIMARY
DEFAULT_PROFILE_ID = "local-dev-profile"

def get_current_profile_id() -> str:
    # Temporary dev fallback until auth is wired in.
    return DEFAULT_PROFILE_ID

profile_id = get_current_profile_id()

class PushUrlsRequest(BaseModel):
    source_key: str = Field(..., min_length=1)
    label: str = ""
    # profile_id: str = Field(..., min_length=1)
    payload: dict[str, Any]


@app.get("/ping")
def ping():
    return {"ok": True, "message": "api alive"}


@app.get("/health/db")
def health_db():
    try:
        test_connection(IMPORT_TARGET)
        return {"ok": True, "database": "reachable"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"database check failed: {exc}")


@app.post("/api/imports/push-urls")
def push_urls(req: PushUrlsRequest):
    print("PUSH_URLS HIT", flush=True)
    print(f"source_key={req.source_key}", flush=True)
    print(f"payload_keys={list(req.payload.keys()) if isinstance(req.payload, dict) else 'not-dict'}", flush=True)
    print(f"urls_count={len(req.payload.get('urls', [])) if isinstance(req.payload, dict) and isinstance(req.payload.get('urls'), list) else 'not-list'}", flush=True)

    if not isinstance(req.payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    if "urls" not in req.payload:
        raise HTTPException(status_code=400, detail="payload.urls is required")
    
    profile_id = get_current_profile_id()
    
    with session_scope(IMPORT_TARGET) as session:
        exists = session.execute(
            text("SELECT 1 FROM profiles WHERE id = :id LIMIT 1"),
            {"id": profile_id},
            ).scalar()
        
    if not exists:
        raise HTTPException(status_code=500, detail="default profile_id not found")
    
    print(f"PUSH_URLS profile_id={profile_id} source_key={req.source_key} urls={len(req.payload.get('urls', []))}", flush=True)

    result = run_import_payload(
        source_key=req.source_key,
        payload=req.payload,
        label=req.label,
        target=IMPORT_TARGET,
        profile_id=profile_id,
        )

    return {
        "ok": True,
        "batch_run": result,
    }

