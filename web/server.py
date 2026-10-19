"""Web demo: serves the page and streams the agent's steps to the browser.

  python -m web.server        # then open http://localhost:8000

Listens on 127.0.0.1 only: every question costs API credits, so don't expose it publicly.
"""
import json
from datetime import date
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent.run import run_agent
from config.models import AGENT_DEFAULT_WEB, AGENT_MODELS

PAGE = Path(__file__).parent / "index.html"

app = FastAPI()
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")  # style.css, app.js


class AskRequest(BaseModel):
    question: str
    model: str = AGENT_DEFAULT_WEB
    today: str | None = None  # "as of" date for backtests; None = really today


@app.get("/")
def page():
    return FileResponse(PAGE)


@app.get("/models")
def models():
    return {"models": AGENT_MODELS, "default": AGENT_DEFAULT_WEB, "today": date.today().isoformat()}


@app.post("/ask")
def ask(req: AskRequest):
    """Runs the agent and streams each event as a Server-Sent Event: 'data: {json}\\n\\n'."""
    if req.model not in AGENT_MODELS:
        raise HTTPException(400, f"unknown model {req.model}")
    if not req.question.strip():
        raise HTTPException(400, "empty question")

    def events():
        try:
            for e in run_agent(req.question, req.today or date.today().isoformat(), req.model):
                yield f"data: {json.dumps(e)}\n\n"
        except Exception as ex:  # e.g. API key missing, network error
            yield f"data: {json.dumps({'type': 'error', 'message': str(ex)})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
