"""Pydantic models for the Nemesis v2 FastAPI event bus."""

from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional


# ── Request models ───────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    node_id: Optional[int] = None
    session_id: Optional[str] = None


class FeatureCreateRequest(BaseModel):
    name: str
    description: Optional[str] = ""
    slack_threads: Optional[str] = ""
    google_docs: Optional[str] = ""
    gmail_threads: Optional[str] = ""


class SessionCreateRequest(BaseModel):
    title: str = "New chat"


class InitRunRequest(BaseModel):
    class Config:
        extra = "allow"


class SkillRunRequest(BaseModel):
    command: str = ""
    args: Optional[str] = ""
    feature_slug: Optional[str] = None


class SyncTriggerRequest(BaseModel):
    source: str


class FeatureRunRequest(BaseModel):
    feature_name: Optional[str] = None
    session_id: Optional[str] = None


class UploadResult(BaseModel):
    name: str
    status: str = "ingested"
    size: int = 0
    error: Optional[str] = None


# ── Response models ──────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str = "healthy"
    db: dict = Field(default_factory=dict)
    qdrant: dict = Field(default_factory=dict)
    claude: dict = Field(default_factory=dict)
    sync: dict = Field(default_factory=dict)
    skills: dict = Field(default_factory=dict)


class StatsResponse(BaseModel):
    total_nodes: int = 0
    total_edges: int = 0
    by_type: dict = Field(default_factory=dict)


class NodeListResponse(BaseModel):
    nodes: list[dict] = Field(default_factory=list)
    total: int = 0
    offset: int = 0
    limit: int = 60


class ChatResponse(BaseModel):
    response: str
    rubick_target: str = ""
    elapsed: float = 0.0
    session_id: Optional[str] = None
    usage: dict = Field(default_factory=dict)


class FeatureListResponse(BaseModel):
    features: list[dict] = Field(default_factory=list)


class SessionListResponse(BaseModel):
    sessions: list[dict] = Field(default_factory=list)


class UsageResponse(BaseModel):
    total: dict = Field(default_factory=dict)
    by_target: list[dict] = Field(default_factory=list)


class SkillInfo(BaseModel):
    id: str
    name: str
    command: str
    description: str
    mode: str = "claude"
    status: str = "active"
    commands: list[str] = Field(default_factory=list)


class SSEEvent(BaseModel):
    event: str
    data: dict = Field(default_factory=dict)
