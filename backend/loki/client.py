from datetime import datetime, timedelta
from typing import Optional

import requests

from backend.config.settings import Settings


class LokiClient:
    """Client for querying Loki log aggregation."""

    def __init__(self, settings: Settings):
        self.base_url = settings.loki_url
        self.session = requests.Session()

    def query_range(
        self,
        query: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> dict:
        """
        Execute a range query against Loki.
        
        Args:
            query: LogQL query string (e.g., '{container="ubuntu-test"}')
            start_time: Query start (default: 1 hour ago)
            end_time: Query end (default: now)
            limit: Max results to return
            
        Returns:
            dict with 'status', 'data' (list of log streams)
        """
        if start_time is None:
            start_time = datetime.utcnow() - timedelta(hours=1)
        if end_time is None:
            end_time = datetime.utcnow()

        # Convert to nanoseconds for Loki
        start_ns = int(start_time.timestamp() * 1e9)
        end_ns = int(end_time.timestamp() * 1e9)

        url = f"{self.base_url}/loki/api/v1/query_range"
        params = {
            "query": query,
            "start": start_ns,
            "end": end_ns,
            "limit": limit,
        }

        response = self.session.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def get_logs_for_container(
        self,
        container_name: str,
        since_minutes: int = 60,
        limit: int = 1000,
    ) -> list[dict]:
        """
        Fetch recent logs for a specific container.
        
        Args:
            container_name: Name of container (e.g., 'ubuntu-test')
            since_minutes: How far back to fetch (default: 60)
            limit: Max log entries
            
        Returns:
            List of log entries with timestamp, message, container
        """
        query = f'{{container="{container_name}"}}'
        start_time = datetime.utcnow() - timedelta(minutes=since_minutes)

        try:
            result = self.query_range(query, start_time=start_time, limit=limit)
        except requests.RequestException:
            # Loki unavailable, return empty
            return []

        logs = []
        if result.get("status") == "success" and result.get("data"):
            for stream in result["data"].get("result", []):
                for timestamp_ns, message in stream.get("values", []):
                    ts = datetime.fromtimestamp(int(timestamp_ns) / 1e9)
                    logs.append(
                        {
                            "timestamp": ts.isoformat(),
                            "container": container_name,
                            "message": message,
                        }
                    )

        return logs

    def get_logs_by_label(
        self,
        label_query: str,
        since_minutes: int = 60,
        limit: int = 1000,
    ) -> list[dict]:
        """
        Fetch logs matching a LogQL label query.
        
        Args:
            label_query: LogQL query (e.g., '{job="proxmox"}')
            since_minutes: How far back to fetch
            limit: Max entries
            
        Returns:
            List of log entries
        """
        start_time = datetime.utcnow() - timedelta(minutes=since_minutes)

        try:
            result = self.query_range(label_query, start_time=start_time, limit=limit)
        except requests.RequestException:
            return []

        logs = []
        if result.get("status") == "success" and result.get("data"):
            for stream in result["data"].get("result", []):
                labels = stream.get("stream", {})
                for timestamp_ns, message in stream.get("values", []):
                    ts = datetime.fromtimestamp(int(timestamp_ns) / 1e9)
                    logs.append(
                        {
                            "timestamp": ts.isoformat(),
                            "labels": labels,
                            "message": message,
                        }
                    )

        return logs
