"""Local Playwright server fixture. Never use this entry point for real interviews.

PYTHONPATH=backend python -m uvicorn e2e_app:app --app-dir tests --port 8000
Real session routes/engine, deterministic fake reasoning, no provider requests.
"""
from app.main import app
from app.nemotron import get_reasoning_service
from app.reasoning import Decision


class CompletionReasoner:
    async def decide(self, context):
        return Decision(action="MOVE_ON", reason="Offline completion regression fixture.", next_prompt=None)


app.dependency_overrides[get_reasoning_service] = lambda: CompletionReasoner()
