"""API route aggregation."""

from fastapi import APIRouter

from lifeman.routes import (
    chat,
    inputs,
    memory,
    notifications,
    observations,
    outputs,
    permissions,
    schedules,
    system,
    tools,
    ui,
)

api_router = APIRouter(prefix="/api")
api_router.include_router(tools.router, prefix="/tools", tags=["tools"])
api_router.include_router(permissions.router, prefix="/permissions", tags=["permissions"])
api_router.include_router(schedules.router, prefix="/schedules", tags=["schedules"])
api_router.include_router(notifications.router, prefix="/notifications", tags=["notifications"])
api_router.include_router(outputs.router, prefix="/outputs", tags=["outputs"])
api_router.include_router(inputs.router, prefix="/inputs", tags=["inputs"])
api_router.include_router(memory.router, prefix="/memory", tags=["memory"])
api_router.include_router(observations.router, prefix="/observations", tags=["observations"])
api_router.include_router(chat.router, prefix="/chat", tags=["chat"])
api_router.include_router(system.router, tags=["system"])

ui_router = ui.router
