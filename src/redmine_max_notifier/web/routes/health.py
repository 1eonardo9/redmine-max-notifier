"""Health-check эндпоинт для мониторинга.

На текущем этапе всегда возвращает 200 OK. На последующих этапах
сюда добавятся проверки живости зависимостей (БД, MAX API, Redmine
API) — с осторожностью, чтобы health-эндпоинт не стал сам источником
нагрузки на внешние сервисы.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["monitoring"])


class HealthResponse(BaseModel):
    """Ответ health-эндпоинта.

    Отдельная модель нужна, чтобы:
    - FastAPI сгенерировал схему в Swagger (видно, что именно возвращаем);
    - при расширении (uptime, версия, статусы зависимостей) не
      переделывать сигнатуру хендлера — только добавить поля модели.
    """

    status: str


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Возвращает статус сервиса. Всегда 200 OK на этом этапе."""
    return HealthResponse(status="ok")
