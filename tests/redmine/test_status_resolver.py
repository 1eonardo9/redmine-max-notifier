"""Тесты StatusResolver — TTL-кэша «id статуса → имя».

Проверяем не столько «резолвит имена» (это тривиально), сколько сам
контракт кэша: сколько запросов реально уходит в Redmine и при каких
условиях.

Время нигде не мокается: time.monotonic глобален, и event loop меряет
им же свои таймеры — заморозив его, мы бы получили asyncio.sleep,
который никогда не проснётся. Вместо этого берём микроскопический TTL
и спим по-настоящему.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest
from pytest_httpx import HTTPXMock

from redmine_max_notifier.redmine.client import RedmineClient
from redmine_max_notifier.redmine.exceptions import RedmineAuthError
from redmine_max_notifier.status_resolver import StatusResolver
from tests.conftest import load_fixture

# TTL, который заведомо не истечёт за время теста.
LONG_TTL = timedelta(hours=1)

# TTL, который истекает за время короткого sleep'а.
#
# Зазор между TTL и сном — четырёхкратный, и это не паранойя:
# гранулярность системного таймера в Windows ~15.6мс, поэтому
# asyncio.sleep(0.06) просыпается где-то между 0.046 и 0.078.
# С зазором в 10мс тест флакует — сон иногда заканчивается ДО
# истечения TTL, кэш законно остаётся свежим, и проверка падает.
SHORT_TTL = timedelta(seconds=0.05)
SHORT_TTL_ELAPSED = 0.2


def test_rejects_non_positive_ttl(client: RedmineClient) -> None:
    """Нулевой или отрицательный TTL — ошибка конфигурации, а не
    «кэш, который всегда протух»: молча долбить API на каждый резолв
    точно не то, чего хотел вызывающий."""
    with pytest.raises(ValueError, match="ttl должен быть положительным"):
        StatusResolver(client, ttl=timedelta(0))


async def test_resolve_returns_status_name(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Первый resolve заполняет кэш и отдаёт имя статуса."""
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/issue_statuses.json",
        json=load_fixture("issue_statuses.json"),
        status_code=200,
    )
    resolver = StatusResolver(client, ttl=LONG_TTL)

    assert await resolver.resolve(2) == "В работе"
    assert len(httpx_mock.get_requests()) == 1


async def test_second_resolve_hits_cache_not_network(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """В пределах TTL повторные резолвы не ходят в Redmine — в этом
    весь смысл кэша."""
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/issue_statuses.json",
        json=load_fixture("issue_statuses.json"),
        status_code=200,
    )
    resolver = StatusResolver(client, ttl=LONG_TTL)

    assert await resolver.resolve(1) == "Новая"
    assert await resolver.resolve(5) == "Закрыта"
    assert await resolver.resolve(1) == "Новая"

    # Три резолва, один запрос — включая резолв статуса, которого
    # не было в первом обращении.
    assert len(httpx_mock.get_requests()) == 1


async def test_unknown_id_returns_none_without_refetch(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Промах по свежему кэшу — None и никакого повторного запроса.

    Кейс из жизни: статус удалили, но он остался в journal старой задачи.
    Если бы мы рефрешились на каждый промах, такое событие било бы по API
    при каждом проходе поллера.
    """
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/issue_statuses.json",
        json=load_fixture("issue_statuses.json"),
        status_code=200,
    )
    resolver = StatusResolver(client, ttl=LONG_TTL)

    assert await resolver.resolve(1) == "Новая"
    assert await resolver.resolve(999) is None
    assert await resolver.resolve(999) is None

    assert len(httpx_mock.get_requests()) == 1


async def test_cache_refreshes_after_ttl_expires(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """По истечении TTL кэш перечитывается целиком, и новые данные
    Redmine становятся видны — ради этого TTL и нужен (админ добавил
    статус, рестартовать сервис не хочется)."""
    payload = load_fixture("issue_statuses.json")

    # Второй ответ — тот же список, но статус 2 переименован и добавлен
    # новый статус 8. Так проверяем, что кэш заменяется, а не дополняется.
    updated = {
        "issue_statuses": [
            *[s for s in payload["issue_statuses"] if s["id"] != 2],
            {"id": 2, "name": "В работе (переименован)", "is_closed": False},
            {"id": 8, "name": "На согласовании", "is_closed": False},
        ]
    }

    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/issue_statuses.json",
        json=payload,
        status_code=200,
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/issue_statuses.json",
        json=updated,
        status_code=200,
    )
    resolver = StatusResolver(client, ttl=SHORT_TTL)

    assert await resolver.resolve(2) == "В работе"
    assert await resolver.resolve(8) is None

    await asyncio.sleep(SHORT_TTL_ELAPSED)

    assert await resolver.resolve(2) == "В работе (переименован)"
    assert await resolver.resolve(8) == "На согласовании"

    # Ровно два запроса: по одному на каждое окно TTL.
    assert len(httpx_mock.get_requests()) == 2


async def test_concurrent_misses_collapse_into_one_request(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Пять корутин, промахнувшихся одновременно, делают один запрос.

    Это тест на lock + двойную проверку внутри него. Без lock'а было бы
    пять параллельных запросов; с lock'ом, но без двойной проверки —
    пять последовательных.

    Мок отвечает через callback с asyncio.sleep, а не мгновенно, и это
    принципиально: на мгновенном ответе первая корутина проходит весь
    _refresh, ни разу не отдав управление event loop'у, остальные четыре
    стартуют уже на заполненном кэше и до lock'а вообще не доходят.
    Запрос тогда ровно один — но по совершенно другой причине, и тест
    зеленеет даже с выломанным lock'ом.
    """

    async def slow_response(request: httpx.Request) -> httpx.Response:
        # Задержка = гарантированная точка переключения: пока первая
        # корутина висит здесь, остальные успевают дойти до lock'а.
        await asyncio.sleep(0.02)
        return httpx.Response(200, json=load_fixture("issue_statuses.json"))

    httpx_mock.add_callback(
        slow_response,
        method="GET",
        url=f"{base_url}/issue_statuses.json",
        is_reusable=True,  # иначе повторный запрос упадёт раньше, чем assert
    )
    resolver = StatusResolver(client, ttl=LONG_TTL)

    results = await asyncio.gather(*(resolver.resolve(3) for _ in range(5)))

    assert results == ["Решена"] * 5
    assert len(httpx_mock.get_requests()) == 1


async def test_client_error_propagates_and_stale_cache_is_not_served(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Если Redmine недоступен при обновлении протухшего кэша — резолвер
    бросает исключение клиента, а не подсовывает старое имя.

    Молча отданное протухшее значение маскировало бы недоступность
    Redmine; решение «ловить или падать» принимает поллер.
    """
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/issue_statuses.json",
        json=load_fixture("issue_statuses.json"),
        status_code=200,
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/issue_statuses.json",
        status_code=401,
        text="Unauthorized",
    )
    resolver = StatusResolver(client, ttl=SHORT_TTL)

    # Кэш успешно заполнен — значение для id=1 у резолвера есть.
    assert await resolver.resolve(1) == "Новая"

    await asyncio.sleep(SHORT_TTL_ELAPSED)

    # ...но после протухания оно недоступно, раз обновиться не вышло.
    with pytest.raises(RedmineAuthError):
        await resolver.resolve(1)
