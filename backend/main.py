import asyncio
import logging
import os
from contextlib import asynccontextmanager

import httpx
from agents.main_agent import ping_agent_service
from database import models as user_models
from database.connection import Base as UserBase
from database.connection import engine as db_engine
from datapool import models as datapool_models
from datapool.connection import Base as DataPoolBase
from datapool.connection import engine as dp_engine
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers.agent import router as agent_router
from routers.auth import router as auth_router
from routers.datapool import router as datapool_router
from routers.health_connect import router as health_connect_router
from routers.skills import router as skills_router
from routers.urls import router as urls_router
from routers.users import router as users_router
from routers.webhooks import router as webhooks_router
from services.quest_expiry import quest_expiry_loop
from sqlalchemy import text

logger = logging.getLogger(__name__)


def register_cron_job_on_startup():
    """Programmatically creates or syncs a keep-alive job on cron-job.org on startup."""
    api_key = os.getenv("CRON_JOB_API_KEY")
    if not api_key:
        logger.info("CRON_JOB_API_KEY not set; skipping cron-job.org registration.")
        return

    agent_url = os.getenv(
        "AGENT_SERVICE_URL",
        "https://ai-skill-sense-agent.onrender.com",
    ).rstrip("/")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # cron-job.org PUT payload to create/update a job hitting the agent every 10 minutes
    payload = {
        "job": {
            "url": f"{agent_url}/",
            "enabled": True,
            "title": "AI Skill Sense Agent Keep-Alive",
            "saveResponses": False,
            "schedule": {
                "timezone": "UTC",
                "hours": [-1],
                "mdays": [-1],
                "minutes": [0, 10, 20, 30, 40, 50],  # Every 10 mins
                "months": [-1],
                "wdays": [-1],
            },
        }
    }

    try:
        response = httpx.put(
            "https://api.cron-job.org/jobs",
            json=payload,
            headers=headers,
            timeout=10.0,
        )
        if response.status_code in (200, 201):
            logger.info("Successfully registered/updated keep-alive cron job on cron-job.org.")
        else:
            logger.warning(
                "Failed to register cron-job.org job: HTTP %d - %s",
                response.status_code,
                response.text,
            )
    except Exception as exc:
        logger.warning("Error communicating with cron-job.org API: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    UserBase.metadata.create_all(bind=db_engine)
    DataPoolBase.metadata.create_all(bind=dp_engine)

    agent_status = ping_agent_service()
    app.state.agent_status = agent_status

    # Register/sync the cron-job.org external trigger asynchronously on boot
    asyncio.to_thread(register_cron_job_on_startup)

    # This is to check quest expiry repeatedly
    expiry_task = asyncio.create_task(quest_expiry_loop())

    try:
        yield
    finally:
        # Stop the background task before shutdown.
        expiry_task.cancel()

        try:
            await expiry_task
        except asyncio.CancelledError:
            pass

        db_engine.dispose()
        dp_engine.dispose()


app = FastAPI(
    title="AI Skill Sense Backend",
    lifespan=lifespan,
)

allowed_origins = [
    origin.strip()
    for origin in os.getenv(
        "FRONTEND_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Agent-Secret",
        "X-Health-Connect-Token",
    ],
)

app.include_router(urls_router)
app.include_router(users_router)
app.include_router(skills_router)
app.include_router(datapool_router)
app.include_router(agent_router)
app.include_router(auth_router)
app.include_router(webhooks_router)
app.include_router(health_connect_router)


@app.get("/")
def health_check():
    return {"status": "healthy"}


@app.get("/health/db")
def database_health_check():
    with db_engine.connect() as connection:
        db_result = connection.execute(
            text("SELECT current_database()")
        ).scalar_one()

    with dp_engine.connect() as connection:
        dp_result = connection.execute(
            text("SELECT current_database()")
        ).scalar_one()

    return {
        "status": "connected",
        "database": db_result,
        "datapool": dp_result,
    }