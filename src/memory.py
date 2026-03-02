"""Memoria del agente: Qdrant (semántica) + Redis (chat buffer conversacional).
Embeddings y reranking vía Cohere API.
"""
import json
import logging
import uuid
from datetime import timedelta

import cohere
import redis.asyncio as aioredis
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from src.config import settings

logger = logging.getLogger(__name__)

# ─── Clientes (lazy init) ─────────────────────────────────────────────────────

_qdrant: AsyncQdrantClient | None = None
_redis: aioredis.Redis | None = None
_cohere: cohere.AsyncClientV2 | None = None


def get_qdrant() -> AsyncQdrantClient:
    global _qdrant
    if _qdrant is None:
        _qdrant = AsyncQdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
    return _qdrant


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis


def get_cohere() -> cohere.AsyncClientV2:
    global _cohere
    if _cohere is None:
        _cohere = cohere.AsyncClientV2(api_key=settings.cohere_api_key)
    return _cohere


# ─── Inicialización de Qdrant ─────────────────────────────────────────────────

async def ensure_collection_exists() -> None:
    """Crea la colección Qdrant si no existe.
    Cohere embed-multilingual-v3.0 produce vectores de 1024 dimensiones.
    """
    qdrant = get_qdrant()
    collections = await qdrant.get_collections()
    names = [c.name for c in collections.collections]
    if settings.qdrant_collection not in names:
        await qdrant.create_collection(
            collection_name=settings.qdrant_collection,
            vectors_config=VectorParams(size=1024, distance=Distance.COSINE),
        )
        logger.info("Colección Qdrant '%s' creada (1024 dims, Cohere).", settings.qdrant_collection)


# ─── Embeddings con Cohere ────────────────────────────────────────────────────

async def embed(text: str, input_type: str = "search_document") -> list[float]:
    """
    Genera embedding con Cohere embed-multilingual-v3.0.
    input_type:
      "search_document" → para textos que se van a indexar
      "search_query"    → para textos de búsqueda
    """
    co = get_cohere()
    response = await co.embed(
        texts=[text],
        model=settings.cohere_embed_model,
        input_type=input_type,
        embedding_types=["float"],
    )
    return response.embeddings.float_[0]


# ─── Reranking con Cohere ─────────────────────────────────────────────────────

async def rerank(query: str, documents: list[str], top_n: int = 5) -> list[dict]:
    """
    Reordena documentos por relevancia usando Cohere rerank-multilingual-v3.0.
    Retorna lista de {index, text, relevance_score} ordenada por score.
    """
    if not documents:
        return []
    co = get_cohere()
    response = await co.rerank(
        query=query,
        documents=documents,
        model=settings.cohere_rerank_model,
        top_n=top_n,
    )
    return [
        {
            "index": r.index,
            "text": documents[r.index],
            "relevance_score": r.relevance_score,
        }
        for r in response.results
    ]


# ─── Qdrant: memoria semántica ────────────────────────────────────────────────

async def save_to_memory(text: str, metadata: dict) -> None:
    """Guarda texto + metadata en Qdrant con embedding de Cohere."""
    if len(text.strip()) < 20:
        return
    vector = await embed(text, input_type="search_document")
    point = PointStruct(id=str(uuid.uuid4()), vector=vector, payload={"text": text, **metadata})
    await get_qdrant().upsert(collection_name=settings.qdrant_collection, points=[point])


async def search_memory(query: str, limit: int = 10, rerank_top: int = 5) -> list[dict]:
    """
    Busca en Qdrant los textos más similares y luego aplica reranking con Cohere.
    1. Qdrant devuelve los 'limit' candidatos por similitud vectorial
    2. Cohere reranker los ordena por relevancia semántica real → top rerank_top
    """
    vector = await embed(query, input_type="search_query")
    results = await get_qdrant().search(
        collection_name=settings.qdrant_collection,
        query_vector=vector,
        limit=limit,
        with_payload=True,
    )

    if not results:
        return []

    # Extraer textos para reranking
    texts = [r.payload.get("text", "") for r in results]
    payloads = [r.payload for r in results]

    # Reranking
    try:
        reranked = await rerank(query, texts, top_n=rerank_top)
        return [
            {"text": r["text"], "relevance_score": r["relevance_score"], **payloads[r["index"]]}
            for r in reranked
        ]
    except Exception as exc:
        logger.warning("Cohere rerank failed, returning raw Qdrant results: %s", exc)
        return [{"text": r.payload.get("text", ""), "score": r.score, **r.payload} for r in results[:rerank_top]]


# ─── Redis: chat buffer conversacional ───────────────────────────────────────

def _chat_key(chat_id: str) -> str:
    return f"chat:{chat_id}"


async def get_chat_history(chat_id: str) -> list[dict]:
    """Retorna los últimos N mensajes del buffer conversacional (orden cronológico)."""
    redis = get_redis()
    raw = await redis.lrange(_chat_key(chat_id), 0, settings.chat_buffer_size - 1)
    history = []
    for item in raw:
        try:
            history.append(json.loads(item))
        except json.JSONDecodeError:
            continue
    return list(reversed(history))  # Más viejo primero


async def save_to_chat_buffer(chat_id: str, role: str, content: str) -> None:
    """Agrega mensaje al buffer circular y renueva TTL de 24h."""
    redis = get_redis()
    key = _chat_key(chat_id)
    message = json.dumps({"role": role, "content": content})
    await redis.lpush(key, message)
    await redis.ltrim(key, 0, settings.chat_buffer_size - 1)
    await redis.expire(key, int(timedelta(hours=24).total_seconds()))


def _chat_state_key(chat_id: str) -> str:
    return f"chat_state:{chat_id}"


async def get_chat_state(chat_id: str) -> dict:
    """Retorna metadata breve de la conversación actual."""
    redis = get_redis()
    raw = await redis.get(_chat_state_key(chat_id))
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


async def update_chat_state(chat_id: str, **fields) -> dict:
    """Actualiza metadata conversacional persistida en Redis."""
    redis = get_redis()
    key = _chat_state_key(chat_id)
    state = await get_chat_state(chat_id)
    for field, value in fields.items():
        if value is None:
            state.pop(field, None)
        else:
            state[field] = value
    await redis.set(key, json.dumps(state), ex=int(timedelta(hours=24).total_seconds()))
    return state


async def clear_chat_context(chat_id: str) -> None:
    """Borra historial y estado conversacional del chat."""
    redis = get_redis()
    await redis.delete(_chat_key(chat_id), _chat_state_key(chat_id))
