"""Celery app instance.

Workers are started with:

    celery -A flows.celery_app worker --loglevel=info
"""

from __future__ import annotations

from celery import Celery

from config.settings import settings


celery_app = Celery(
    "trading_intelligence",
    broker=settings.REDIS_URL or "redis://localhost:6379/0",
    backend=settings.REDIS_URL or "redis://localhost:6379/0",
    include=[
        "ingestion.edgar.rss_watcher",
    ],
)

celery_app.conf.update(
    task_default_queue="default",
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    timezone=settings.TIMEZONE,
    enable_utc=True,
    broker_connection_retry_on_startup=True,
)
