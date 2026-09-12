"""FastAPI 应用入口：路由装配、CORS、生命周期钩子。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api import tasks
from .config import PROJECT_ROOT, settings
from .database import init_db, mark_interrupted_tasks

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    await init_db()
    stale = await mark_interrupted_tasks()
    if stale:
        logger.warning("检测到 %d 个上次未跑完的任务，已标记为 INTERRUPTED，可调用 /resume 续跑", stale)
    if settings.mock_llm:
        logger.warning("MOCK_LLM=true：AI 层返回模拟结论，不会发起真实模型调用")
    elif not settings.llm_configured:
        logger.warning("未配置 OPENAI_API_KEY：上传后的任务会因缺少凭证而失败")
    logger.info("%s 已就绪 | 模型=%s | 并发=%d", settings.app_name, settings.llm_model, settings.llm_concurrency)
    yield


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description="基于多模态大模型的 Excel 多语种 UI 截图缺陷（截断/重叠/缺字）自动检测系统",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.cors_origins.strip() == "*" else [o.strip() for o in settings.cors_origins.split(",")],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

app.include_router(tasks.router)


@app.get("/api/v1/health", tags=["system"])
async def health() -> dict:
    return {
        "code": 200,
        "message": "ok",
        "data": {
            "app": settings.app_name,
            "model": settings.llm_model,
            "mock_llm": settings.mock_llm,
            "llm_configured": settings.llm_configured,
            "concurrency": settings.llm_concurrency,
        },
    }


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.status_code, "message": str(exc.detail), "data": None},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"code": 422, "message": "请求参数校验失败", "data": exc.errors()},
    )


# 若前端已执行过 `npm run build`，直接把产物挂到根路径——单端口部署时无需再起 Node。
# 注意：Starlette 按注册顺序匹配，挂载 "/" 必须放在**所有接口定义之后**，
# 否则它会拦截 /api/... 的请求（表现为所有接口 404）。
_frontend_dist = PROJECT_ROOT / "frontend" / "dist"
if _frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=str(_frontend_dist), html=True), name="frontend")
    logger.info("已挂载前端静态产物: %s（单端口模式）", _frontend_dist)
