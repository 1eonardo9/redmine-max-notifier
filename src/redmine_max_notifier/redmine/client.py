import httpx


class RedmineClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "X-Redmine-API-Key": self._api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=self._timeout,
            trust_env=False,
        )

    async def aclose(self) -> None:
        """Закрыть внутренний HTTP-клиент и освободить пул соединений."""
        await self._client.aclose()

    async def __aenter__(self) -> "RedmineClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        await self.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int | bool | None] | None = None,
        json: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Отправить HTTP-запрос к Redmine и вернуть распарсенный JSON.
        на этапе 1a — минимальная реализация. Обработка ошибок и ретраи — в 1d.
        """
        response = await self._client.request(
            method=method,
            url=path,
            params=params,
            json=json,
        )
        response.raise_for_status()
        data: dict[str, object] = response.json()
        return data
