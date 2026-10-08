"""Verified semantic memory with local and PostgreSQL/pgvector storage."""
import asyncio
import json
import math
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
import httpx


def _validate_embedding(vector: list[float], dimensions: int) -> list[float]:
    if len(vector) != dimensions or not all(isinstance(value, (int, float)) and math.isfinite(value)
                                             for value in vector):
        raise ValueError("Embedding has invalid dimensions or values")
    return [float(value) for value in vector]


class OllamaEmbedder:
    def __init__(self, url: str, model: str, dimensions: int):
        if not 8 <= dimensions <= 4096:
            raise ValueError("Embedding dimensions must be between 8 and 4096")
        self.url, self.model, self.dimensions = url.rstrip("/"), model, dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts or len(texts) > 32 or any(not text or len(text) > 20_000 for text in texts):
            raise ValueError("Embedding batch is empty or exceeds limits")
        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            response = await client.post(self.url + "/api/embed", json={
                "model": self.model, "input": texts, "truncate": False,
                "dimensions": self.dimensions,
            })
            response.raise_for_status()
            vectors = response.json().get("embeddings", [])
        if len(vectors) != len(texts):
            raise ValueError("Embedding service returned an unexpected batch")
        return [_validate_embedding(vector, self.dimensions) for vector in vectors]


class SQLiteVectorMemory:
    def __init__(self, path: Path, dimensions: int):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.dimensions = path, dimensions
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS memories ("
                       "id TEXT PRIMARY KEY, task_id TEXT UNIQUE, repository TEXT, kind TEXT, "
                       "content TEXT, metadata TEXT, embedding TEXT, created_at TEXT)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def upsert(self, task_id, repository, kind, content, metadata, embedding):
        embedding = _validate_embedding(embedding, self.dimensions)
        with self.connect() as db:
            db.execute("INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                       "ON CONFLICT(task_id) DO UPDATE SET repository=excluded.repository, "
                       "kind=excluded.kind, content=excluded.content, metadata=excluded.metadata, "
                       "embedding=excluded.embedding, created_at=excluded.created_at",
                       (uuid.uuid4().hex, task_id, repository, kind, content, json.dumps(metadata),
                        json.dumps(embedding), datetime.now(timezone.utc).isoformat()))

    def search(self, repository, embedding, limit, kinds=None):
        query = _validate_embedding(embedding, self.dimensions)
        with self.connect() as db:
            rows = db.execute("SELECT task_id, kind, content, metadata, embedding, created_at "
                              "FROM memories WHERE repository=?", (repository,)).fetchall()
        wanted = set(kinds or [])
        scored = []
        query_norm = math.sqrt(sum(value * value for value in query)) or 1.0
        for task_id, kind, content, metadata, raw, created_at in rows:
            if wanted and kind not in wanted:
                continue
            vector = json.loads(raw)
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            score = sum(a * b for a, b in zip(query, vector)) / (query_norm * norm)
            scored.append({"task_id": task_id, "kind": kind, "content": content,
                           "metadata": json.loads(metadata), "created_at": created_at, "score": score})
        return sorted(scored, key=lambda item: item["score"], reverse=True)[:limit]


class PostgresVectorMemory:
    def __init__(self, dsn: str, dimensions: int):
        if not 8 <= dimensions <= 4096:
            raise ValueError("Embedding dimensions must be between 8 and 4096")
        self.dsn, self.dimensions = dsn, dimensions
        self.initialize()

    def connect(self):
        import psycopg
        from pgvector.psycopg import register_vector
        connection = psycopg.connect(self.dsn)
        register_vector(connection)
        return connection

    def initialize(self):
        import psycopg
        with psycopg.connect(self.dsn) as db:
            db.execute("CREATE EXTENSION IF NOT EXISTS vector")
            db.execute("CREATE TABLE IF NOT EXISTS coding_brain_memory_meta ("
                       "singleton boolean PRIMARY KEY DEFAULT true, dimensions integer NOT NULL)")
            row = db.execute("SELECT dimensions FROM coding_brain_memory_meta WHERE singleton=true").fetchone()
            if row and row[0] != self.dimensions:
                raise ValueError("Configured embedding dimensions differ from the existing database")
            db.execute("INSERT INTO coding_brain_memory_meta(singleton, dimensions) VALUES(true, %s) "
                       "ON CONFLICT(singleton) DO NOTHING", (self.dimensions,))
            db.execute(f"CREATE TABLE IF NOT EXISTS coding_brain_memories ("
                       f"id uuid PRIMARY KEY, task_id text UNIQUE, repository text, kind text, content text, "
                       f"metadata jsonb, embedding vector({self.dimensions}), created_at timestamptz)")
            db.execute("CREATE INDEX IF NOT EXISTS coding_brain_memories_embedding_hnsw "
                       "ON coding_brain_memories USING hnsw (embedding vector_cosine_ops)")

    def upsert(self, task_id, repository, kind, content, metadata, embedding):
        from pgvector import Vector
        from psycopg.types.json import Jsonb
        vector = Vector(_validate_embedding(embedding, self.dimensions))
        with self.connect() as db:
            db.execute("INSERT INTO coding_brain_memories VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                       "ON CONFLICT(task_id) DO UPDATE SET repository=excluded.repository, "
                       "kind=excluded.kind, content=excluded.content, metadata=excluded.metadata, "
                       "embedding=excluded.embedding, created_at=excluded.created_at",
                       (uuid.uuid4(), task_id, repository, kind, content, Jsonb(metadata), vector,
                        datetime.now(timezone.utc)))

    def search(self, repository, embedding, limit, kinds=None):
        from pgvector import Vector
        vector = Vector(_validate_embedding(embedding, self.dimensions))
        filters, parameters = "repository=%s", [vector, repository]
        if kinds:
            filters += " AND kind = ANY(%s)"
            parameters.append(kinds)
        parameters.extend([vector, limit])
        with self.connect() as db:
            rows = db.execute("SELECT task_id, kind, content, metadata, created_at, "
                              "1 - (embedding <=> %s) AS score FROM coding_brain_memories WHERE " +
                              filters + " ORDER BY embedding <=> %s LIMIT %s", parameters).fetchall()
        return [{"task_id": row[0], "kind": row[1], "content": row[2], "metadata": row[3],
                 "created_at": row[4].isoformat(), "score": float(row[5])} for row in rows]


class SemanticMemory:
    ALLOWED_KINDS = {"episodic", "semantic", "procedural"}

    def __init__(self, embedder: OllamaEmbedder, backend):
        self.embedder, self.backend = embedder, backend

    async def remember(self, task_id, repository, kind, content, metadata, verified=False):
        if not verified:
            raise ValueError("Only verified outcomes may enter long-term memory")
        if kind not in self.ALLOWED_KINDS:
            raise ValueError("Unknown memory kind")
        vector = (await self.embedder.embed([content]))[0]
        await asyncio.to_thread(self.backend.upsert, task_id, repository, kind, content, metadata, vector)

    async def search(self, repository, query, limit=5, kinds=None):
        if not 1 <= limit <= 20:
            raise ValueError("Memory limit must be between 1 and 20")
        vector = (await self.embedder.embed([query]))[0]
        return await asyncio.to_thread(self.backend.search, repository, vector, limit, kinds)
