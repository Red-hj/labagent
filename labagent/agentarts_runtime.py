"""Official AgentArts SDK adapter over the existing LangGraph workflow."""
from .material_service import MaterialService


def create_app(database=None, completion=None):
    from agentarts.sdk import AgentArtsRuntimeApp
    from starlette.responses import JSONResponse

    app = AgentArtsRuntimeApp(max_concurrency=4)
    service = MaterialService(database, completion)
    app.material_service = service

    @app.entrypoint
    def invoke(request, context):
        try:
            if isinstance(request, dict) and not request.get("session_id") and context and context.session_id:
                if service.store.history(context.session_id):
                    request = {**request, "session_id": context.session_id}
            response = service.invoke(request)
            if context:
                context.session_id = response["session_id"]
            return response
        except ValueError as error:
            return JSONResponse({"status": "invalid_request", "message": str(error)}, status_code=400)

    return app
