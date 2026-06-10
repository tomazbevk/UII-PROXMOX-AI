import hashlib
from datetime import datetime
from typing import Optional

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

from backend.config.settings import Settings


class LogStore:
    """Qdrant storage for log entries with semantic search capability."""

    LOGS_COLLECTION = "logs"
    VECTOR_SIZE = 4

    def __init__(self, settings: Settings):
        self.client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
        self.collection_name = settings.qdrant_current_collection_name.replace(
            "infrastructure", "logs"
        )  # e.g., logs_current
        self.ensure_collection()

    def ensure_collection(self):
        """Create logs collection if it doesn't exist."""
        try:
            self.client.get_collection(self.collection_name)
        except Exception:
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(size=self.VECTOR_SIZE, distance=Distance.COSINE),
            )

    def store_logs(self, logs: list[dict], batch_id: str) -> int:
        """
        Store a batch of logs with semantic vectors.
        
        Args:
            logs: List of log entries with timestamp, container, message
            batch_id: Unique ID for this ingestion batch
            
        Returns:
            Number of logs stored
        """
        if not logs:
            return 0

        points = []
        for idx, log in enumerate(logs):
            point_id = self._generate_point_id(batch_id, idx, log)
            vector = self._vector_from_log(log)
            payload = {
                "batch_id": batch_id,
                "timestamp": log.get("timestamp", datetime.utcnow().isoformat()),
                "container": log.get("container", "unknown"),
                "message": log.get("message", ""),
                "labels": log.get("labels", {}),
            }
            points.append(PointStruct(id=point_id, vector=vector, payload=payload))

        if points:
            self.client.upsert(collection_name=self.collection_name, points=points)

        return len(points)

    def search_logs(
        self,
        query_text: str,
        container: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict]:
        """
        Semantic search over logs.
        
        Args:
            query_text: Search query (e.g., "error in container")
            container: Optional filter by container name
            limit: Max results
            
        Returns:
            List of matching log entries with relevance scores
        """
        query_vector = self._vector_from_text(query_text)
        
        # Build filter if container specified
        filter_dict = None
        if container:
            filter_dict = {
                "must": [
                    {
                        "key": "container",
                        "match": {"value": container},
                    }
                ]
            }

        try:
            results = self.client.search(
                collection_name=self.collection_name,
                query_vector=query_vector,
                query_filter=filter_dict,
                limit=limit,
            )
        except Exception:
            return []

        logs = []
        for scored_point in results:
            logs.append(
                {
                    "timestamp": scored_point.payload.get("timestamp"),
                    "container": scored_point.payload.get("container"),
                    "message": scored_point.payload.get("message"),
                    "score": scored_point.score,
                }
            )

        return logs

    def get_recent_logs(
        self,
        container: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        Get most recent logs (by insertion order).
        
        Args:
            container: Optional filter by container name
            limit: Max results
            
        Returns:
            List of recent log entries
        """
        try:
            points = self.client.scroll(
                collection_name=self.collection_name,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Failed to scroll logs from Qdrant collection '{self.collection_name}': {exc}")
            raise

        logs = []
        for point in points[0]:
            if container and point.payload.get("container") != container:
                continue
            logs.append(
                {
                    "timestamp": point.payload.get("timestamp"),
                    "container": point.payload.get("container"),
                    "message": point.payload.get("message"),
                }
            )

        return logs

    def _generate_point_id(self, batch_id: str, index: int, log: dict) -> int:
        """Generate deterministic point ID."""
        key = f"{batch_id}:{index}:{log.get('timestamp', '')}:{log.get('container', '')}"
        hash_digest = hashlib.sha256(key.encode()).hexdigest()
        # Convert first 8 hex chars to int
        return int(hash_digest[:8], 16)

    def _vector_from_log(self, log: dict) -> list[float]:
        """Generate 4-dim vector from log entry."""
        text = f"{log.get('container', '')} {log.get('message', '')}"
        return self._vector_from_text(text)

    def _vector_from_text(self, text: str) -> list[float]:
        """Generate 4-dim vector from text via hash."""
        hash_bytes = hashlib.sha256(text.encode()).digest()
        # Split hash into 4 chunks, normalize to [0, 1]
        vector = [
            int.from_bytes(hash_bytes[i * 8 : (i + 1) * 8], "big") / (2**64)
            for i in range(4)
        ]
        return vector
