# AI Homelab Assistant (ai-stack)

FastAPI backend with a chat-centric web UI for local model interactions and approvals.

Run the API locally (after installing dependencies):

```bash
python -m pip install -r requirements.txt
uvicorn backend.api.main:app --reload --host 0.0.0.0 --port 8000
```

Then open `http://127.0.0.1:8000/ui` in your browser.

For a local backend talking to a Proxmox host on another machine, set `PROXMOX_HOST_IP` and `PROXMOX_PORT` in your environment. The existing Proxmox API token settings stay the same.
