import logging
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from backend.approvals.store import ApprovalStore
from backend.config.settings import get_settings
from backend.ollama.client import OllamaClient, get_tool_definitions
from backend.proxmox.client import ProxmoxClient
from backend.qdrant.snapshots import SnapshotStore
from backend.loki.client import LokiClient
from backend.qdrant.logs import LogStore
from backend.execution.service import ExecutionService

from .health import probe_http_service
from .models import (
    Container,
    HealthReport,
    HealthResponse,
    InfrastructureHistoryItem,
    InfrastructureSummary,
    ScanResult,
    ServiceHealth,
    LogIngestionRequest,
    LogIngestionResult,
    LogSearchRequest,
    LogSearchResult,
    LogEntry,
    ChatRequest,
    ChatResponse,
    SuggestedAction,
    ApprovalCreateRequest,
    ApprovalDecisionRequest,
    ApprovalItem,
    ExecuteRequest,
    ExecutionResult,
    SettingsResponse,
    SettingsUpdateRequest,
    SettingsSavedResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()
approval_store = ApprovalStore()
exec_service = ExecutionService()


def build_container_brief(containers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": container.get("name"),
            "type": container.get("type"),
            "node": container.get("node"),
            "status": container.get("status"),
            "ip": container.get("ip"),
        }
        for container in containers
    ]


def fetch_container_scan_context(settings) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    client = ProxmoxClient(settings)
    scan_data = client.scan_inventory()
    containers = scan_data.get("containers", [])
    return containers, build_container_brief(containers), scan_data


def format_container_scan_context(scan_data: dict[str, Any], container_brief: list[dict[str, Any]]) -> str:
    diagnostics = scan_data.get("diagnostics", [])
    scanned_nodes = scan_data.get("scanned_nodes", 0)
    context = [
        f"Fresh container scan ({len(container_brief)} items across {scanned_nodes} nodes):\n{container_brief}",
    ]
    if diagnostics:
        context.append(f"Scan diagnostics: {diagnostics}")
    return "\n\n".join(context) + "\n\n"


def collect_service_health() -> list[ServiceHealth]:
    settings = get_settings()
    probes = [
        probe_http_service(
            "proxmox",
            settings.proxmox_api_base_url,
            "/api2/json/version",
            verify_ssl=settings.proxmox_verify_ssl,
            headers={"Authorization": settings.proxmox_auth_header},
        ),
        probe_http_service("qdrant", settings.qdrant_url, "/healthz"),
        probe_http_service("ollama", settings.ollama_url, "/api/version"),
        probe_http_service("loki", settings.loki_url, "/ready"),
        probe_http_service("prometheus", settings.prometheus_url, "/-/ready"),
    ]
    return [ServiceHealth(**probe.__dict__) for probe in probes]


@router.get("/health/live", response_model=HealthResponse)
def health_live():
    return HealthResponse(status="ok")


@router.get("/health", response_model=HealthReport)
def health():
    services = collect_service_health()
    status = "ok" if all(service.ok for service in services) else "degraded"
    if status != "ok":
        raise HTTPException(status_code=503, detail="one or more services are unavailable")
    return HealthReport(status=status, services=services)


@router.get("/health/services", response_model=HealthReport)
def health_services():
    services = collect_service_health()
    status = "ok" if all(service.ok for service in services) else "degraded"
    return HealthReport(status=status, services=services)


@router.get("/containers", response_model=List[Container])
def list_containers():
    settings = get_settings()
    try:
        client = ProxmoxClient(settings)
        containers = client.list_all_containers()
        return [Container(**c) for c in containers]
    except Exception as exc:
        logger.error(f"Failed to fetch containers: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch containers from Proxmox: {exc}")
    return []


@router.get("/infrastructure/current", response_model=List[Container])
def get_current_infrastructure():
    settings = get_settings()
    try:
        snapshot_store = SnapshotStore(settings)
        current_points = snapshot_store.list_current_infrastructure()
        return [Container(**point) for point in current_points]
    except Exception as exc:
        logger.error(f"Failed to fetch current infrastructure: {exc}")
        raise HTTPException(status_code=500, detail="Failed to fetch current infrastructure")


@router.get("/infrastructure/history", response_model=List[InfrastructureHistoryItem])
def get_infrastructure_history(limit: int = 20):
    settings = get_settings()
    try:
        snapshot_store = SnapshotStore(settings)
        history_points = snapshot_store.list_history_scans(limit=limit)
        return [
            InfrastructureHistoryItem(
                scan_id=point.get("scan_id", ""),
                timestamp=point.get("timestamp", datetime.now(timezone.utc)),
                container_count=point.get("container_count", 0),
                scanned_nodes=point.get("scanned_nodes", 0),
                diagnostics=point.get("diagnostics", []),
                containers=[Container(**container) for container in point.get("containers", [])],
            )
            for point in history_points
        ]
    except Exception as exc:
        logger.error(f"Failed to fetch infrastructure history: {exc}")
        raise HTTPException(status_code=500, detail="Failed to fetch infrastructure history")


@router.get("/infrastructure/history/{scan_id}", response_model=InfrastructureHistoryItem)
def get_infrastructure_history_item(scan_id: str):
    settings = get_settings()
    try:
        snapshot_store = SnapshotStore(settings)
        point = snapshot_store.get_history_scan(scan_id)
        if not point:
            raise HTTPException(status_code=404, detail="Scan not found")
        return InfrastructureHistoryItem(
            scan_id=point.get("scan_id", scan_id),
            timestamp=point.get("timestamp", datetime.now(timezone.utc)),
            container_count=point.get("container_count", 0),
            scanned_nodes=point.get("scanned_nodes", 0),
            diagnostics=point.get("diagnostics", []),
            containers=[Container(**container) for container in point.get("containers", [])],
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to fetch infrastructure history item: {exc}")
        raise HTTPException(status_code=500, detail="Failed to fetch infrastructure history item")


@router.get("/infrastructure", response_model=InfrastructureSummary)
def get_infrastructure_summary():
    settings = get_settings()
    try:
        snapshot_store = SnapshotStore(settings)
        current_points = snapshot_store.list_current_infrastructure()
        history_points = snapshot_store.list_history_scans(limit=20)

        current_containers = [Container(**point) for point in current_points]
        history_items = [
            InfrastructureHistoryItem(
                scan_id=point.get("scan_id", ""),
                timestamp=point.get("timestamp", datetime.now(timezone.utc)),
                container_count=point.get("container_count", 0),
                scanned_nodes=point.get("scanned_nodes", 0),
                diagnostics=point.get("diagnostics", []),
                containers=[Container(**container) for container in point.get("containers", [])],
            )
            for point in history_points
        ]

        return InfrastructureSummary(
            current=current_containers,
            latest_scan=history_items[0] if history_items else None,
            history=history_items,
        )
    except Exception as exc:
        logger.error(f"Failed to fetch infrastructure summary: {exc}")
        raise HTTPException(status_code=500, detail="Failed to fetch infrastructure summary")


@router.get("/debug/proxmox")
def debug_proxmox():
    """Debug endpoint to test Proxmox connectivity."""
    import traceback
    settings = get_settings()
    try:
        client = ProxmoxClient(settings)
        base_url = client.base_url
        verify = client.session.verify
        auth = client.session.headers.get("Authorization", "")[:60] + "..."
        try:
            nodes = client.get_nodes()
            return {"ok": True, "base_url": base_url, "verify_ssl": verify, "auth_header": auth, "nodes": nodes}
        except Exception as e:
            return {"ok": False, "base_url": base_url, "verify_ssl": verify, "auth_header": auth, "error": str(e), "traceback": traceback.format_exc()}
    except Exception as e:
        return {"ok": False, "error": str(e), "traceback": traceback.format_exc()}


@router.post("/scan", response_model=ScanResult)
def scan_infrastructure():
    settings = get_settings()
    try:
        client = ProxmoxClient(settings)
        scan_data = client.scan_inventory()
        container_models = [Container(**c) for c in scan_data["containers"]]
        snapshot_store = SnapshotStore(settings)
        snapshot_id = snapshot_store.store_scan_snapshot(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "container_count": len(container_models),
                "containers": container_models,
                "scanned_nodes": scan_data["scanned_nodes"],
                "diagnostics": scan_data["diagnostics"],
            }
        )
        return ScanResult(
            timestamp=datetime.now(timezone.utc),
            container_count=len(container_models),
            containers=container_models,
            success=True,
            scanned_nodes=scan_data["scanned_nodes"],
            diagnostics=scan_data["diagnostics"],
            history_snapshot_id=snapshot_id,
        )
    except Exception as exc:
        logger.error(f"Failed to scan infrastructure: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to scan infrastructure: {exc}")


@router.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest):
    settings = get_settings()

    system_prompt = (
        "You are an on-prem Proxmox homelab DevOps assistant. "
        "You have access to tools that can scan containers, fetch logs, search logs, and manage containers. "
        "ALWAYS use the relevant tool first when the user asks about containers, VMs, logs, or infrastructure. "
        "For example, if the user asks about running containers, call scan_containers. "
        "After you have the tool results, provide a clear and helpful answer. "
        "When you have all the context you need (after tool results), respond with ONLY a JSON object: "
        '{"summary": "your answer to the user", "reasoning": "brief reasoning", "confidence": 0.0-1.0, '
        '"suggested_actions": [{"action": "action description", "command": "executable shell command or null", "target": "container name or null", "risk": "low|medium|high"}]}. '
        "CRITICAL: The command field must contain a REAL Proxmox/Linux shell command that can be executed on the host. "
        "Valid commands include: pct list, pct config <vmid>, qm list, qm config <vmid>, pvesh /nodes/<node>/status, "
        "journalctl -u <service>, systemctl status <service>, ip addr, df -h, free -m, ps aux. "
        "NEVER put tool names like 'scan_containers' or 'get_logs' in the command field. "
        "If you cannot provide a real shell command, set command to null."
    )

    # Build messages array with system prompt, history, and current query
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    for msg in payload.history:
        messages.append({"role": msg.role, "content": msg.content})
    messages.append({"role": "user", "content": payload.query})

    try:
        ollama_client = OllamaClient(settings)
        if payload.model:
            ollama_client.model = payload.model

        # chat() handles the full tool-calling loop internally:
        # call LLM -> check tool_calls -> execute tools -> append results -> repeat
        model_result = ollama_client.chat(messages, get_tool_definitions())
    except Exception as exc:
        logger.error(f"Failed to query Ollama: {exc}")
        raise HTTPException(status_code=500, detail="Failed to generate chat response")

    # Parse suggested_actions from the model result
    raw_actions = model_result.get("suggested_actions", [])
    normalized_actions: list[SuggestedAction] = []
    if isinstance(raw_actions, list):
        for item in raw_actions:
            if not isinstance(item, dict):
                continue
            try:
                normalized_actions.append(
                    SuggestedAction(
                        action=str(item.get("action", "Investigate issue")),
                        command=item.get("command"),
                        target=item.get("target"),
                        risk=str(item.get("risk", "medium")),
                    )
                )
            except Exception:
                continue

    try:
        confidence = float(model_result.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return ChatResponse(
        timestamp=datetime.now(timezone.utc),
        query=payload.query,
        summary=str(model_result.get("summary", "No summary available.")),
        reasoning=str(model_result.get("reasoning", "No reasoning provided.")),
        confidence=confidence,
        suggested_actions=normalized_actions,
        context={
            "model": settings.ollama_model,
        },
    )


@router.post("/chat/stream")
def chat_stream(payload: ChatRequest):
    settings = get_settings()

    # Clean up old executed/rejected approvals so they don't reappear
    try:
        with approval_store._lock:
            with approval_store._conn:
                approval_store._conn.execute(
                    "DELETE FROM approvals WHERE status NOT IN ('pending',)"
                )
                approval_store._conn.commit()
    except Exception:
        pass

    system_prompt = (
        "You are an on-prem Proxmox homelab DevOps assistant. "
        "You have access to tools that can scan containers, fetch logs, search logs, and manage containers. "
        "ALWAYS use the relevant tool first when the user asks about containers, VMs, logs, or infrastructure. "
        "For example, if the user asks about running containers, call scan_containers. "
        "After you have the tool results, provide a clear and helpful answer. "
        "When you have all the context you need (after tool results), respond with ONLY a JSON object: "
        '{"summary": "your answer to the user", "reasoning": "brief reasoning", "confidence": 0.0-1.0, '
        '"suggested_actions": [{"action": "action description", "command": "executable shell command or null", "target": "container name or null", "risk": "low|medium|high"}]}. '
        "CRITICAL: The command field must contain a REAL Proxmox/Linux shell command that can be executed on the host. "
        "Valid commands include: pct list, pct config <vmid>, qm list, qm config <vmid>, pvesh /nodes/<node>/status, "
        "journalctl -u <service>, systemctl status <service>, ip addr, df -h, free -m, ps aux. "
        "NEVER put tool names like 'scan_containers' or 'get_logs' in the command field. "
        "If you cannot provide a real shell command, set command to null."
    )

    # Build messages array with system prompt, history, and current query
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    for msg in payload.history:
        messages.append({"role": msg.role, "content": msg.content})
    messages.append({"role": "user", "content": payload.query})

    def generator():
        try:
            ollama_client = OllamaClient(settings)
            if payload.model:
                ollama_client.model = payload.model

            # Show thinking indicator while the model processes
            yield json.dumps({"type": "chunk", "text": "Thinking..."}) + "\n"

            # Use client.chat() which handles the full tool-calling loop
            # internally: call LLM -> check tool_calls -> execute -> repeat
            model_result = ollama_client.chat(messages, get_tool_definitions())

            # Stream the final answer summary character by character
            summary = model_result.get("summary", "")
            if summary:
                for char in summary:
                    yield json.dumps({"type": "chunk", "text": char}) + "\n"

            # Emit any suggested_actions as tool_call events for the UI
            for action in model_result.get("suggested_actions", []):
                if isinstance(action, dict) and action.get("command"):
                    yield json.dumps({
                        "type": "tool_call",
                        "tool": "execute",
                        "args": {
                            "action": action.get("action", "command"),
                            "command": action.get("command", ""),
                            "target": action.get("target"),
                            "risk": action.get("risk", "medium"),
                        },
                    }) + "\n"

            yield json.dumps({"type": "final", "payload": model_result}) + "\n"
        except Exception as exc:
            logger.error(f"Streaming failed: {exc}")
            yield json.dumps({"type": "error", "error": str(exc)}) + "\n"

    return StreamingResponse(generator(), media_type="application/x-ndjson")


@router.get("/models", response_model=List[str])
def get_models():
    settings = get_settings()
    try:
        client = OllamaClient(settings)
        models = client.list_models()
        return models
    except Exception as exc:
        logger.error(f"Failed to list models: {exc}")
        raise HTTPException(status_code=500, detail="Failed to list models")


@router.post("/ingest/logs", response_model=LogIngestionResult)
def ingest_logs(request: LogIngestionRequest):
    """Fetch logs from Loki and persist to Qdrant."""
    settings = get_settings()
    batch_id = str(uuid.uuid4())
    
    try:
        loki_client = LokiClient(settings)
        all_logs = []

        # If a LogQL label_query is provided, use it (e.g. '{job="prometheus"}')
        if request.label_query:
            try:
                all_logs = loki_client.get_logs_by_label(
                    request.label_query, since_minutes=request.since_minutes
                )
                containers = [f"label_query:{request.label_query}"]
            except Exception as e:
                logger.warning(f"Failed to fetch logs for label_query {request.label_query}: {e}")
                containers = []
        else:
            # Get list of containers to ingest
            if request.containers:
                containers = request.containers
            else:
                # Get all containers from Proxmox
                client = ProxmoxClient(settings)
                container_list = client.list_all_containers()
                containers = [c["name"] for c in container_list]

            # Fetch logs from Loki for each container
            for container_name in containers:
                try:
                    logs = loki_client.get_logs_for_container(
                        container_name, since_minutes=request.since_minutes
                    )
                    all_logs.extend(logs)
                except Exception as e:
                    logger.warning(f"Failed to fetch logs for {container_name}: {e}")
        
        # Attempt to map host-level logs to known containers (simple heuristic)
        try:
            client = ProxmoxClient(settings)
            container_infos = client.list_all_containers()
            container_names = [c.get("name", "").lower() for c in container_infos]
            container_hostnames = [c.get("hostname", "") for c in container_infos if c.get("hostname")]
        except Exception:
            container_infos = []
            container_names = []
            container_hostnames = []

        for log in all_logs:
            # prefer existing container label if present
            if log.get("container") and not str(log.get("container")).startswith("label_query:"):
                continue
            msg = str(log.get("message", "")).lower()
            assigned = None
            for name in container_names:
                if name and name in msg:
                    assigned = name
                    break
            if not assigned:
                for hn in container_hostnames:
                    if hn and hn.lower() in msg:
                        # find container with this hostname
                        for c in container_infos:
                            if c.get("hostname") and c.get("hostname").lower() == hn.lower():
                                assigned = c.get("name")
                                break
                        if assigned:
                            break
            if assigned:
                log["container"] = assigned
            else:
                # fallback: keep host label or mark as host
                if request.label_query:
                    log["container"] = f"label_query:{request.label_query}"
                else:
                    log.setdefault("container", "host")

        # Store in Qdrant
        log_store = LogStore(settings)
        total_ingested = log_store.store_logs(all_logs, batch_id)
        
        return LogIngestionResult(
            batch_id=batch_id,
            timestamp=datetime.now(timezone.utc),
            total_logs_ingested=total_ingested,
            containers_processed=containers,
            success=True,
        )
    except Exception as exc:
        logger.error(f"Failed to ingest logs: {exc}")
        raise HTTPException(status_code=500, detail="Failed to ingest logs")


@router.post("/logs/search", response_model=LogSearchResult)
def search_logs(request: LogSearchRequest):
    """Semantic search over ingested logs."""
    settings = get_settings()
    
    try:
        log_store = LogStore(settings)
        results = log_store.search_logs(
            query_text=request.query,
            container=request.container,
            limit=request.limit,
        )
        
        log_entries = [
            LogEntry(
                timestamp=r["timestamp"],
                container=r["container"],
                message=r["message"],
                labels=r.get("labels", {}),
            )
            for r in results
        ]
        
        return LogSearchResult(
            query=request.query,
            timestamp=datetime.now(timezone.utc),
            results=log_entries,
            total_results=len(log_entries),
        )
    except Exception as exc:
        logger.error(f"Failed to search logs: {exc}")
        raise HTTPException(status_code=500, detail="Failed to search logs")


@router.get("/logs/recent", response_model=List[LogEntry])
def get_recent_logs(container: str | None = None, limit: int = 100):
    """Get most recent logs."""
    settings = get_settings()
    
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 1000")
    
    try:
        log_store = LogStore(settings)
        results = log_store.get_recent_logs(container=container, limit=limit)
        
        logger.info(f"Returning {len(results)} recent logs (container={container}, limit={limit})")
        
        return [
            LogEntry(
                timestamp=r["timestamp"],
                container=r["container"],
                message=r["message"],
                labels=r.get("labels", {}),
            )
            for r in results
        ]
    except Exception as exc:
        logger.error(f"Failed to get recent logs: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to get recent logs: {exc}")


@router.post("/approvals", response_model=ApprovalItem)
def create_approval(request: ApprovalCreateRequest):
    try:
        item = approval_store.create(
            action=request.action,
            command=request.command,
            target=request.target,
            risk=request.risk,
            source_query=request.source_query,
            requested_by=request.requested_by,
        )
        return ApprovalItem(**item)
    except Exception as exc:
        logger.error(f"Failed to create approval: {exc}")
        raise HTTPException(status_code=500, detail="Failed to create approval")


@router.get("/approvals", response_model=List[ApprovalItem])
def list_approvals(status: str | None = None):
    allowed = {"pending", "approved", "rejected"}
    if status and status not in allowed:
        raise HTTPException(status_code=400, detail="status must be pending, approved, or rejected")

    try:
        items = approval_store.list(status=status)
        return [ApprovalItem(**item) for item in items]
    except Exception as exc:
        logger.error(f"Failed to list approvals: {exc}")
        raise HTTPException(status_code=500, detail="Failed to list approvals")


@router.get("/approvals/{approval_id}", response_model=ApprovalItem)
def get_approval(approval_id: str):
    try:
        item = approval_store.get(approval_id)
        if not item:
            raise HTTPException(status_code=404, detail="Approval not found")
        return ApprovalItem(**item)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to get approval: {exc}")
        raise HTTPException(status_code=500, detail="Failed to get approval")


@router.delete("/approvals/{approval_id}")
def delete_approval(approval_id: str):
    try:
        existing = approval_store.get(approval_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Approval not found")
        # perform a hard delete from the database
        with approval_store._lock:
            with approval_store._conn:
                approval_store._conn.execute("DELETE FROM approvals WHERE id = ?", (approval_id,))
        return {"deleted": approval_id}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to delete approval: {exc}")
        raise HTTPException(status_code=500, detail="Failed to delete approval")


@router.post("/approvals/cleanup")
def cleanup_approvals(remove_empty: bool = True, action: str | None = None):
    """Remove approvals matching simple filters.

    - `remove_empty`: if true, delete approvals where `command` is NULL or empty
    - `action`: if provided, delete only approvals with this action value
    """
    try:
        with approval_store._lock:
            conn = approval_store._conn
            query = "DELETE FROM approvals WHERE 1=1"
            params: list = []
            if remove_empty:
                query += " AND (command IS NULL OR trim(command) = '')"
            if action:
                query += " AND action = ?"
                params.append(action)
            cur = conn.execute(query, params)
            deleted = cur.rowcount if cur is not None else 0
            conn.commit()
        return {"deleted": deleted}
    except Exception as exc:
        logger.error(f"Failed to cleanup approvals: {exc}")
        raise HTTPException(status_code=500, detail="Failed to cleanup approvals")


@router.patch("/approvals/{approval_id}", response_model=ApprovalItem)
def decide_approval(approval_id: str, request: ApprovalDecisionRequest):
    if request.decision not in {"approved", "rejected"}:
        raise HTTPException(status_code=400, detail="decision must be approved or rejected")

    try:
        existing = approval_store.get(approval_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Approval not found")

        updated = approval_store.decide(
            approval_id=approval_id,
            decision=request.decision,
            reviewer=request.reviewer,
            note=request.note,
        )
        if not updated:
            raise HTTPException(status_code=500, detail="Failed to update approval")
        return ApprovalItem(**updated)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to decide approval: {exc}")
        raise HTTPException(status_code=500, detail="Failed to decide approval")


@router.post("/execute", response_model=ExecutionResult)
def execute_command(request: ExecuteRequest):
    """Execute an approved, validated diagnostic command through ProxVNC.

    Only executions tied to an approval (status == 'approved') are allowed.
    """
    # Resolve command and approval
    cmd = request.command
    target = request.target
    approval_id = request.approval_id

    if approval_id:
        existing = approval_store.get(approval_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Approval not found")
        if existing.get("status") != "approved":
            raise HTTPException(status_code=400, detail="Approval is not approved for execution")
        if not cmd:
            cmd = existing.get("command")
            target = existing.get("target")
    else:
        # For safety, disallow executions without an approval record in this MVP
        raise HTTPException(status_code=400, detail="Execution requires an approved approval_id")

    if not cmd:
        raise HTTPException(status_code=400, detail="No command available to execute")

    try:
        result = exec_service.execute(cmd, target, timeout=request.timeout)
        return ExecutionResult(
            approval_id=approval_id,
            command=cmd,
            target=target,
            returncode=result.get("returncode", -1),
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
            executed_at=datetime.now(timezone.utc),
        )
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as exc:
        logger.error(f"Execution failed: {exc}")
        raise HTTPException(status_code=500, detail=f"Execution failed: {exc}")


@router.post("/execute/direct", response_model=ExecutionResult)
def execute_direct(request: ExecuteRequest):
    """Execute a command immediately without an approval record. Use with caution.

    This endpoint is intended for interactive UIs where the user explicitly confirms execution
    of a model-suggested command. It requires `command` to be provided.
    """
    cmd = request.command
    target = request.target

    if not cmd:
        raise HTTPException(status_code=400, detail="Direct execution requires a command")

    try:
        result = exec_service.execute(cmd, target, timeout=request.timeout)
        return ExecutionResult(
            approval_id=None,
            command=cmd,
            target=target,
            returncode=result.get("returncode", -1),
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
            executed_at=datetime.now(timezone.utc),
        )
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as exc:
        logger.error(f"Direct execution failed: {exc}")
        raise HTTPException(status_code=500, detail=f"Direct execution failed: {exc}")


@router.get("/settings", response_model=SettingsResponse)
def get_current_settings():
    """Return current non-sensitive configuration."""
    s = get_settings()
    return SettingsResponse(
        app_env=s.app_env,
        app_host=s.app_host,
        app_port=s.app_port,
        proxmox_url=s.proxmox_url,
        proxmox_host_ip=s.proxmox_host_ip,
        proxmox_ip=s.proxmox_ip,
        proxmox_node=s.proxmox_node,
        proxmox_port=s.proxmox_port,
        proxmox_realm=s.proxmox_realm,
        proxmox_user=s.proxmox_user,
        proxmox_token_id=s.proxmox_token_id,
        proxmox_verify_ssl=s.proxmox_verify_ssl,
        qdrant_url=s.qdrant_url,
        qdrant_api_key=s.qdrant_api_key,
        qdrant_current_collection_name=s.qdrant_current_collection_name,
        qdrant_history_collection_name=s.qdrant_history_collection_name,
        ollama_url=s.ollama_url,
        ollama_model=s.ollama_model,
        loki_url=s.loki_url,
        prometheus_url=s.prometheus_url,
        approval_db_path=s.approval_db_path,
    )


@router.patch("/settings", response_model=SettingsSavedResponse)
def update_settings(payload: SettingsUpdateRequest):
    """Update .env file with provided values. Server restart required for changes to take effect."""
    env_path = Path(__file__).resolve().parents[2] / ".env"

    # Read current .env
    env = _read_env_file(env_path)

    updated_fields: list[str] = []
    for field_name, value in payload.model_dump(exclude_none=True).items():
        env_key = _ENV_VAR_MAP.get(field_name)
        if env_key is None:
            continue
        str_value = str(value) if not isinstance(value, bool) else str(value).lower()
        env[env_key] = str_value
        updated_fields.append(field_name)

    if not updated_fields:
        return SettingsSavedResponse(saved=False, message="No fields provided to update.")

    _write_env_file(env_path, env)

    return SettingsSavedResponse(
        saved=True,
        message=f"Updated {len(updated_fields)} field(s): {', '.join(updated_fields)}. Restart the server for changes to take effect.",
    )


# ---------------------------------------------------------------------------
# Settings endpoints
# ---------------------------------------------------------------------------

_ENV_VAR_MAP: dict[str, str] = {
    "app_env": "APP_ENV",
    "app_host": "APP_HOST",
    "app_port": "APP_PORT",
    "proxmox_url": "PROXMOX_URL",
    "proxmox_host_ip": "PROXMOX_HOST_IP",
    "proxmox_ip": "PROXMOX_IP",
    "proxmox_node": "PROXMOX_NODE",
    "proxmox_port": "PROXMOX_PORT",
    "proxmox_realm": "PROXMOX_REALM",
    "proxmox_user": "PROXMOX_USER",
    "proxmox_token_id": "PROXMOX_TOKEN_ID",
    "proxmox_token_secret": "PROXMOX_TOKEN_SECRET",
    "proxmox_password": "PROXMOX_PASSWORD",
    "proxmox_verify_ssl": "PROXMOX_VERIFY_SSL",
    "qdrant_url": "QDRANT_URL",
    "qdrant_api_key": "QDRANT_API_KEY",
    "qdrant_current_collection_name": "QDRANT_CURRENT_COLLECTION_NAME",
    "qdrant_history_collection_name": "QDRANT_HISTORY_COLLECTION_NAME",
    "ollama_url": "OLLAMA_URL",
    "ollama_model": "OLLAMA_MODEL",
    "loki_url": "LOKI_URL",
    "prometheus_url": "PROMETHEUS_URL",
    "approval_db_path": "APPROVAL_DB_PATH",
}


def _read_env_file(path: Path) -> dict[str, str]:
    """Read a .env file into a flat {KEY: value} dict (ignores comments / blanks)."""
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def _write_env_file(path: Path, env: dict[str, str]) -> None:
    """Write a flat {KEY: value} dict back to a .env file, preserving comments."""
    lines: list[str] = []
    if path.is_file():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                lines.append(raw_line)
                continue
            key, _, _ = stripped.partition("=")
            key = key.strip()
            if key in env:
                lines.append(f"{key}={env[key]}")
                del env[key]
            else:
                lines.append(raw_line)
    # Append any new keys that weren't in the original file
    for key, value in env.items():
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")



