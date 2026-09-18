"""管理后台API入口。本地开发运行：

    .venv-mac/bin/uvicorn src.api.app:app --reload --port 8000

CORS开放给Vite开发服务器（默认5173端口）；这一阶段不做鉴权（/grilling确认过，本地/内网自用）。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routers import experiments, feishu, materials, model_config, poi_photos, presentation, projects, reviews, trash, user_notes
from src.execution_state import (
    JobService,
    RuntimeManager,
    environment_name,
    get_job_service,
    maintenance_enabled_by_default,
)


def create_app(
    *,
    runtime_enabled: bool | None = None,
    job_service: JobService | None = None,
) -> FastAPI:
    service = job_service or get_job_service()
    enabled = maintenance_enabled_by_default() if runtime_enabled is None else runtime_enabled

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        manager = RuntimeManager(
            service=service,
            role="backend",
            environment=environment_name(),
            enabled=enabled,
            coordinator=True,
        )
        application.state.runtime_manager = manager
        manager.start()
        try:
            yield
        finally:
            manager.stop()

    application = FastAPI(title="旅游攻略多Agent助理 · 管理后台API", lifespan=lifespan)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.include_router(projects.router)
    application.include_router(trash.router)
    application.include_router(materials.router)
    application.include_router(presentation.router)
    application.include_router(model_config.router)
    application.include_router(feishu.router)
    application.include_router(poi_photos.router)
    application.include_router(user_notes.router)
    application.include_router(reviews.router)
    application.include_router(reviews.index_router)
    application.include_router(reviews.executor_router)
    application.include_router(experiments.router)

    @application.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    return application


app = create_app()
