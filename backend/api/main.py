from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from .routes import router

app = FastAPI(title="AI Homelab Assistant - API")
app.include_router(router)

# Mount a simple static frontend at /ui when the `frontend` folder exists
frontend_dir = Path(__file__).resolve().parents[2] / "frontend"
if frontend_dir.exists() and frontend_dir.is_dir():
	app.mount("/ui", StaticFiles(directory=str(frontend_dir), html=True), name="ui")
