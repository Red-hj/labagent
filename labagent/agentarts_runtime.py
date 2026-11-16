"""Official AgentArts SDK adapter over the existing LangGraph workflow."""
import threading

from .material_service import MaterialService


def create_app(database=None, completion=None):
    from agentarts.sdk import AgentArtsRuntimeApp
    from starlette.responses import JSONResponse

    app = AgentArtsRuntimeApp(max_concurrency=4)
    service = MaterialService(database, completion)
    app.material_service = service
    session_locks = [threading.Lock() for _ in range(32)]
    with service.store.connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS agentarts_sessions (platform_id TEXT PRIMARY KEY, session_id TEXT NOT NULL)")

    @app.entrypoint
    def invoke(request, context):
        try:
            platform_id = context.session_id if context else None
            with session_locks[hash(platform_id or "new") % len(session_locks)]:
                if isinstance(request, dict) and not request.get("session_id") and platform_id:
                    with service.store.connect() as db:
                        saved = db.execute("SELECT session_id FROM agentarts_sessions WHERE platform_id=?", (platform_id,)).fetchone()
                    session_id = saved[0] if saved else platform_id
                    if service.store.history(session_id):
                        request = {**request, "session_id": session_id}
                response = service.invoke(request)
                platform_id = platform_id or response["session_id"]
                with service.store.connect() as db:
                    db.execute("INSERT OR REPLACE INTO agentarts_sessions(platform_id, session_id) VALUES (?, ?)",
                               (platform_id, response["session_id"]))
            if context:
                context.session_id = platform_id
            response["platform_session_id"] = platform_id
            return response
        except ValueError as error:
            return JSONResponse({"status": "invalid_request", "message": str(error)}, status_code=400)

    return app
