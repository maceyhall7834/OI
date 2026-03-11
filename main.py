import os
import json
import asyncio
import subprocess
from typing import List, Optional
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Depends, Request, status
from fastapi.responses import RedirectResponse, FileResponse
from pydantic import BaseModel, EmailStr
from motor.motor_asyncio import AsyncIOMotorClient
from passlib.context import CryptContext
import uvicorn

# Configuration
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "openwebui")
WEBUI_SECRET_KEY = os.getenv("WEBUI_SECRET_KEY")
if not WEBUI_SECRET_KEY:
    raise RuntimeError("WEBUI_SECRET_KEY is required and must be stable across restarts.")

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "admin-token")
PROVIDERS_PATH = os.getenv("PROVIDERS_PATH", "config/providers.json")
OPENWEBUI_SERVE_CMD = os.getenv("OPENWEBUI_SERVE_CMD", "open-webui serve --no-local-models")
PORT = int(os.getenv("PORT", "8080"))

# DB
mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo[DB_NAME]

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

app = FastAPI(title="OpenWebUI Proxy + Persistence")

# Static brand file serving
BRAND_DIR = os.path.join(os.getcwd(), "brand")
os.makedirs(BRAND_DIR, exist_ok=True)
NAME_PATH = os.path.join(BRAND_DIR, "name.txt")
if not os.path.exists(NAME_PATH):
    with open(NAME_PATH, "w", encoding="utf-8") as f:
        f.write("OpenWebUI")

# Pydantic models
class UserCreate(BaseModel):
    id: str
    email: EmailStr
    password: Optional[str]
    is_admin: Optional[bool] = False

class UserOut(BaseModel):
    id: str
    email: EmailStr
    is_admin: bool
    created_at: Optional[datetime]

class ConversationIn(BaseModel):
    id: str
    user_id: str
    title: Optional[str]

class MessageIn(BaseModel):
    id: str
    conversation_id: str
    role: str
    content: str
    created_at: Optional[datetime] = None

class ProviderEntry(BaseModel):
    id: str
    name: str
    base_url: Optional[str] = None
    token_env: Optional[str] = None
    extra: Optional[dict] = None

# Utilities
def hash_password(password: str) -> str:
    return pwd_ctx.hash(password)

async def ensure_indexes():
    await db.users.create_index("email", unique=True)
    await db.conversations.create_index([("user_id", 1), ("updated_at", -1)])
    await db.messages.create_index([("conversation_id", 1), ("created_at", 1)])

def load_providers_file(path=PROVIDERS_PATH) -> List[dict]:
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        example = [
            {
                "id": "openai",
                "name": "OpenAI (example)",
                "base_url": "https://api.openai.com/v1",
                "token_env": "OPENAI_KEY",
                "extra": {"model": "gpt-4o-mini"}
            }
        ]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(example, f, indent=2)
        return example
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_providers_file(entries: List[dict], path=PROVIDERS_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)

def require_admin_token(request: Request):
    token = request.headers.get("authorization") or request.headers.get("Authorization")
    if token:
        if token.lower().startswith("bearer "):
            token = token.split(None, 1)[1]
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

ow_proc = None

def start_openwebui_subprocess():
    global ow_proc
    env = os.environ.copy()
    env["WEBUI_SECRET_KEY"] = WEBUI_SECRET_KEY
    cmd = OPENWEBUI_SERVE_CMD.split()
    ow_proc = subprocess.Popen(cmd, env=env)
    return ow_proc

async def stop_openwebui_subprocess():
    global ow_proc
    if ow_proc and ow_proc.poll() is None:
        ow_proc.terminate()
        try:
            ow_proc.wait(timeout=10)
        except Exception:
            ow_proc.kill()
    ow_proc = None

@app.on_event("startup")
async def on_startup():
    await ensure_indexes()
    existing = await db.users.find_one({"email": ADMIN_EMAIL})
    if not existing:
        admin_doc = {
            "id": os.getenv("ADMIN_ID", "admin-1"),
            "email": ADMIN_EMAIL,
            "password_hash": hash_password(ADMIN_PASSWORD) if ADMIN_PASSWORD else None,
            "is_admin": True,
            "created_at": datetime.now(timezone.utc),
        }
        await db.users.insert_one(admin_doc)
    load_providers_file()
    start_openwebui_subprocess()

@app.on_event("shutdown")
async def on_shutdown():
    await stop_openwebui_subprocess()
    mongo.close()

# Brand static endpoints
@app.get("/brand/logo.png")
async def brand_logo():
    logo_path = os.path.join(BRAND_DIR, "logo.png")
    if not os.path.exists(logo_path):
        raise HTTPException(status_code=404, detail="logo.png not found")
    return FileResponse(logo_path, media_type="image/png")

@app.get("/brand/name.txt")
async def brand_name():
    return FileResponse(NAME_PATH, media_type="text/plain")

@app.post("/admin/brand/name", dependencies=[Depends(require_admin_token)])
async def admin_set_name(payload: dict):
    name = payload.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    with open(NAME_PATH, "w", encoding="utf-8") as f:
        f.write(name)
    return {"ok": True, "name": name}

# Users/conversations/messages
@app.post("/users", response_model=UserOut, status_code=201)
async def create_user(payload: UserCreate):
    if await db.users.find_one({"email": payload.email}):
        raise HTTPException(status_code=400, detail="email already exists")
    doc = {
        "id": payload.id,
        "email": payload.email,
        "password_hash": hash_password(payload.password) if payload.password else None,
        "is_admin": bool(payload.is_admin),
        "created_at": datetime.now(timezone.utc),
    }
    await db.users.insert_one(doc)
    return UserOut(id=doc["id"], email=doc["email"], is_admin=doc["is_admin"], created_at=doc["created_at"])

@app.post("/conversations", status_code=201)
async def create_conversation(conv: ConversationIn):
    doc = {
        "id": conv.id,
        "user_id": conv.user_id,
        "title": conv.title,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    await db.conversations.insert_one(doc)
    return {"id": conv.id}

@app.get("/conversations/{user_id}")
async def list_conversations(user_id: str, limit: int = 100):
    cursor = db.conversations.find({"user_id": user_id}).sort("updated_at", -1).limit(limit)
    res = []
    async for c in cursor:
        res.append({"id": c["id"], "user_id": c["user_id"], "title": c.get("title"), "created_at": c.get("created_at")})
    return res

@app.post("/messages", status_code=201)
async def append_message(msg: MessageIn):
    doc = {
        "id": msg.id,
        "conversation_id": msg.conversation_id,
        "role": msg.role,
        "content": msg.content,
        "created_at": msg.created_at or datetime.now(timezone.utc),
    }
    await db.messages.insert_one(doc)
    await db.conversations.update_one({"id": msg.conversation_id}, {"$set": {"updated_at": datetime.now(timezone.utc)}})
    return {"id": msg.id}

@app.get("/messages/{conversation_id}")
async def get_messages(conversation_id: str, limit: int = 200):
    cursor = db.messages.find({"conversation_id": conversation_id}).sort("created_at", 1).limit(limit)
    res = []
    async for m in cursor:
        res.append({
            "id": m["id"],
            "conversation_id": m["conversation_id"],
            "role": m["role"],
            "content": m["content"],
            "created_at": m.get("created_at"),
        })
    return res

# Providers admin
@app.get("/admin/providers", dependencies=[Depends(require_admin_token)])
async def admin_list_providers():
    entries = load_providers_file()
    for e in entries:
        token_env = e.get("token_env")
        e["_token_present"] = bool(token_env and os.getenv(token_env))
    return entries

@app.post("/admin/providers", dependencies=[Depends(require_admin_token)])
async def admin_save_providers(entries: List[ProviderEntry]):
    raw = [e.dict() for e in entries]
    save_providers_file(raw)
    return {"ok": True, "count": len(raw)}

@app.get("/healthz")
async def health():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}

@app.get("/")
async def root():
    return RedirectResponse(url="/")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")
