#!/usr/bin/env python3
"""Rubick Vector Search Engine.

Qdrant embedded mode + sentence-transformers for semantic code search.
Anti-hallucination: every retrieved snippet carries provenance metadata
(repo, file, line, commit SHA) verified against the filesystem.

Usage:
    rubick_vectors.py embed <db_path> --project <slug> --repo <path>
    rubick_vectors.py search <db_path> --query <text> [--limit N] [--project <slug>]
    rubick_vectors.py stats
    rubick_vectors.py verify <db_path> --project <slug>
"""

import sys
import os
import json
import argparse
import subprocess
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import brain_config as cfg
except ImportError:
    cfg = None

logger = logging.getLogger("rubick_vectors")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_QDRANT_DATA_PATH = str(cfg.QDRANT_DATA_PATH) if cfg and hasattr(cfg, "QDRANT_DATA_PATH") else "workspace/qdrant_data"
_COLLECTION = cfg.QDRANT_COLLECTION if cfg and hasattr(cfg, "QDRANT_COLLECTION") else "rubick_code"
_MODEL_NAME = cfg.QDRANT_EMBEDDING_MODEL if cfg and hasattr(cfg, "QDRANT_EMBEDDING_MODEL") else "sentence-transformers/all-MiniLM-L6-v2"
_DIM = cfg.QDRANT_EMBEDDING_DIM if cfg and hasattr(cfg, "QDRANT_EMBEDDING_DIM") else 384
_BATCH_SIZE = cfg.QDRANT_BATCH_SIZE if cfg and hasattr(cfg, "QDRANT_BATCH_SIZE") else 256
_SEARCH_LIMIT = cfg.QDRANT_SEARCH_LIMIT if cfg and hasattr(cfg, "QDRANT_SEARCH_LIMIT") else 20
_SCORE_THRESHOLD = cfg.QDRANT_SCORE_THRESHOLD if cfg and hasattr(cfg, "QDRANT_SCORE_THRESHOLD") else 0.35
_MAX_BODY_CHARS = cfg.QDRANT_MAX_BODY_CHARS if cfg and hasattr(cfg, "QDRANT_MAX_BODY_CHARS") else 2048

# ---------------------------------------------------------------------------
# Lazy singletons (avoid loading heavy libs at import time)
# ---------------------------------------------------------------------------

_qdrant_client = None
_embedding_model = None


def init_qdrant(data_path: Optional[str] = None):
    """Initialize Qdrant client in embedded (local) mode."""
    global _qdrant_client
    if _qdrant_client is not None:
        return _qdrant_client

    from qdrant_client import QdrantClient
    path = data_path or _QDRANT_DATA_PATH
    os.makedirs(path, exist_ok=True)
    _qdrant_client = QdrantClient(path=path)
    ensure_collection(_qdrant_client, _COLLECTION, _DIM)
    return _qdrant_client


def init_embedding_model(model_name: Optional[str] = None):
    """Load SentenceTransformer model (cached after first download)."""
    global _embedding_model
    if _embedding_model is not None:
        return _embedding_model

    from sentence_transformers import SentenceTransformer
    _embedding_model = SentenceTransformer(model_name or _MODEL_NAME)
    return _embedding_model


def ensure_collection(client, collection_name: str, dim: int):
    """Create Qdrant collection if it doesn't exist."""
    from qdrant_client.models import Distance, VectorParams
    collections = [c.name for c in client.get_collections().collections]
    if collection_name not in collections:
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
        logger.info("Created collection '%s' (dim=%d)", collection_name, dim)


# ---------------------------------------------------------------------------
# Embedding Pipeline
# ---------------------------------------------------------------------------

def _get_commit_sha(repo_path: str) -> str:
    """Get current HEAD commit SHA for a repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip()[:12] if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def embed_project(db_path: str, project_slug: str, repo_path: str) -> dict:
    """Embed all code bodies for a project from rubick.db into Qdrant."""
    import sqlite3
    from qdrant_client.models import PointStruct

    client = init_qdrant()
    model = init_embedding_model()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    commit_sha = _get_commit_sha(repo_path)
    now = datetime.now(timezone.utc).isoformat()

    rows = conn.execute(
        "SELECT cb.id, cb.node_id, cb.file_path, cb.start_line, cb.end_line, "
        "cb.language, cb.body, cb.byte_length, n.type as node_type, n.name as node_name "
        "FROM code_bodies cb JOIN nodes n ON cb.node_id = n.id "
        "WHERE cb.project_slug = ?",
        (project_slug,)
    ).fetchall()

    if not rows:
        conn.close()
        return {"project": project_slug, "embedded": 0, "skipped": 0}

    points = []
    point_id_base = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM code_bodies"
    ).fetchone()[0] * 2

    for row in rows:
        row = dict(row)
        sig_text = f"{row['node_type']}:{row['node_name']} @ {row['file_path']}:{row['start_line']}"
        body_text = row["body"][:_MAX_BODY_CHARS] if row["body"] else ""

        payload = {
            "node_id": row["node_id"],
            "node_type": row["node_type"],
            "node_name": row["node_name"],
            "project_slug": project_slug,
            "file_path": row["file_path"],
            "line_number": row["start_line"],
            "language": row["language"],
            "commit_sha": commit_sha,
            "embedded_at": now,
        }

        # Signature point
        sig_payload = {**payload, "chunk_type": "signature", "text_preview": sig_text[:200]}
        points.append({"text": sig_text, "payload": sig_payload, "id": point_id_base + row["id"] * 2})

        # Body point (if substantial)
        if len(body_text) > 50:
            body_payload = {**payload, "chunk_type": "body", "text_preview": body_text[:200]}
            points.append({"text": body_text, "payload": body_payload, "id": point_id_base + row["id"] * 2 + 1})

    conn.close()

    embedded = 0
    for batch_start in range(0, len(points), _BATCH_SIZE):
        batch = points[batch_start:batch_start + _BATCH_SIZE]
        texts = [p["text"] for p in batch]
        vectors = model.encode(texts, show_progress_bar=False).tolist()

        qdrant_points = [
            PointStruct(id=batch[i]["id"], vector=vectors[i], payload=batch[i]["payload"])
            for i in range(len(batch))
        ]
        client.upsert(collection_name=_COLLECTION, points=qdrant_points)
        embedded += len(batch)

    return {"project": project_slug, "embedded": embedded, "total_bodies": len(rows), "commit_sha": commit_sha}


# ---------------------------------------------------------------------------
# Incremental Updates
# ---------------------------------------------------------------------------

def get_changed_files(repo_path: str, since_sha: str) -> list[str]:
    """Get files changed since a given commit SHA."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{since_sha}..HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return [f.strip() for f in result.stdout.strip().split('\n') if f.strip()]
    except Exception:
        pass
    return []


def reembed_changed(db_path: str, project_slug: str, repo_path: str,
                    since_sha: str) -> dict:
    """Re-embed only functions in files that changed since last embed."""
    import sqlite3
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    changed = get_changed_files(repo_path, since_sha)
    if not changed:
        return {"project": project_slug, "changed_files": 0, "reembedded": 0}

    client = init_qdrant()

    # Delete old points for changed files
    for fpath in changed:
        client.delete(
            collection_name=_COLLECTION,
            points_selector=Filter(
                must=[
                    FieldCondition(key="project_slug", match=MatchValue(value=project_slug)),
                    FieldCondition(key="file_path", match=MatchValue(value=fpath)),
                ]
            ),
        )

    result = embed_project(db_path, project_slug, repo_path)
    result["changed_files"] = len(changed)
    return result


# ---------------------------------------------------------------------------
# Vector Search
# ---------------------------------------------------------------------------

def vector_search(query: str, limit: int = 0, project_slug: Optional[str] = None,
                  node_type: Optional[str] = None,
                  score_threshold: Optional[float] = None) -> list[dict]:
    """Semantic search over embedded code."""
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    client = init_qdrant()
    model = init_embedding_model()

    query_vector = model.encode(query).tolist()
    limit = limit or _SEARCH_LIMIT
    threshold = score_threshold or _SCORE_THRESHOLD

    filters = []
    if project_slug:
        filters.append(FieldCondition(key="project_slug", match=MatchValue(value=project_slug)))
    if node_type:
        filters.append(FieldCondition(key="node_type", match=MatchValue(value=node_type)))

    search_filter = Filter(must=filters) if filters else None

    results = client.query_points(
        collection_name=_COLLECTION,
        query=query_vector,
        query_filter=search_filter,
        limit=limit,
        score_threshold=threshold,
    ).points

    return [
        {
            "id": r.id,
            "score": round(r.score, 4),
            **r.payload,
        }
        for r in results
    ]


# ---------------------------------------------------------------------------
# Provenance Verification (Anti-Hallucination)
# ---------------------------------------------------------------------------

def verify_provenance(provenance: dict, repos_base: str = "") -> dict:
    """Verify a provenance dict against the actual filesystem.

    Checks:
    1. File exists on disk
    2. Content at stored line matches text_preview
    3. Commit drift (current HEAD vs stored commit_sha)

    Returns provenance dict with 'verified' and optional warnings.
    """
    repos_base = repos_base or (str(cfg.GITHUB_CLONE_BASE) if cfg else "workspace/repos")
    result = {**provenance, "verified": False, "warnings": []}

    slug = provenance.get("project_slug", "")
    fpath = provenance.get("file_path", "").lstrip("./")
    line = provenance.get("line_number", 0)
    stored_sha = provenance.get("commit_sha", "")
    preview = provenance.get("text_preview", "")

    full_path = os.path.join(repos_base, slug, fpath)

    # Check 1: file exists
    if not os.path.isfile(full_path):
        result["warnings"].append(f"file_missing: {full_path}")
        return result

    # Check 2: line content match
    if line > 0 and preview:
        try:
            with open(full_path, 'r', errors='replace') as f:
                lines = f.readlines()
            if line <= len(lines):
                actual = lines[line - 1].strip()[:80]
                expected = preview.strip()[:80]
                if actual != expected:
                    result["warnings"].append(f"line_mismatch: expected '{expected[:40]}...' got '{actual[:40]}...'")
        except Exception:
            result["warnings"].append("read_error")

    # Check 3: commit drift
    if stored_sha and stored_sha != "unknown":
        repo_dir = os.path.join(repos_base, slug)
        current_sha = _get_commit_sha(repo_dir)
        if current_sha != "unknown" and current_sha != stored_sha:
            result["warnings"].append(f"commit_drift: stored={stored_sha} current={current_sha}")

    result["verified"] = len(result["warnings"]) == 0
    return result


def verify_batch(provenances: list[dict], repos_base: str = "") -> list[dict]:
    """Verify multiple provenance records."""
    return [verify_provenance(p, repos_base) for p in provenances]


# ---------------------------------------------------------------------------
# Collection Stats
# ---------------------------------------------------------------------------

def collection_stats() -> dict:
    """Get Qdrant collection statistics."""
    client = init_qdrant()
    info = client.get_collection(_COLLECTION)
    return {
        "collection": _COLLECTION,
        "points_count": info.points_count,
        "status": str(info.status),
    }


def delete_project_vectors(project_slug: str) -> int:
    """Delete all vectors for a project."""
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    client = init_qdrant()
    client.delete(
        collection_name=_COLLECTION,
        points_selector=Filter(
            must=[FieldCondition(key="project_slug", match=MatchValue(value=project_slug))]
        ),
    )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Rubick Vector Search Engine")
    sub = parser.add_subparsers(dest="command")

    embed_p = sub.add_parser("embed", help="Embed code bodies for a project")
    embed_p.add_argument("db_path")
    embed_p.add_argument("--project", required=True)
    embed_p.add_argument("--repo", required=True)

    search_p = sub.add_parser("search", help="Semantic search over code")
    search_p.add_argument("db_path", nargs="?", default="")
    search_p.add_argument("--query", required=True)
    search_p.add_argument("--limit", type=int, default=10)
    search_p.add_argument("--project", default=None)
    search_p.add_argument("--type", default=None)

    sub.add_parser("stats", help="Collection statistics")

    verify_p = sub.add_parser("verify", help="Verify provenance for a project")
    verify_p.add_argument("db_path")
    verify_p.add_argument("--project", required=True)
    verify_p.add_argument("--limit", type=int, default=10)

    args = parser.parse_args()

    if args.command == "embed":
        result = embed_project(args.db_path, args.project, args.repo)
        print(json.dumps(result, indent=2))

    elif args.command == "search":
        results = vector_search(args.query, limit=args.limit,
                                project_slug=args.project, node_type=args.type)
        for r in results:
            print(f"  {r['score']:.3f}  {r['node_type']}:{r['node_name']}  "
                  f"@ {r['file_path']}:{r['line_number']}  [{r['project_slug']}]")
        print(f"\n{len(results)} results")

    elif args.command == "stats":
        stats = collection_stats()
        print(json.dumps(stats, indent=2))

    elif args.command == "verify":
        results = vector_search("*", limit=args.limit, project_slug=args.project)
        provenances = verify_batch(results)
        verified = sum(1 for p in provenances if p["verified"])
        print(f"Verified: {verified}/{len(provenances)}")
        for p in provenances:
            status = "OK" if p["verified"] else f"WARN: {', '.join(p['warnings'])}"
            print(f"  {p.get('node_name', '?')} — {status}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
