"""Собрать расширенный CA-bundle: стандартный certifi + российские корневые CA.

Нужен для работы с сервисами, использующими сертификаты Национального
удостоверяющего центра Минцифры РФ (например, platform-api2.max.ru).
Стандартный certifi этих CA не содержит — приходится расширять вручную.

Запуск:
    uv run python scripts/build_ca_bundle.py

Результат: certs/ca_bundle.pem — путь, который передаётся в httpx.AsyncClient
через параметр verify.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import certifi

# Пути относительно корня репо. Скрипт умеет запускаться из любой директории.
REPO_ROOT = Path(__file__).resolve().parent.parent
CERTS_DIR = REPO_ROOT / "certs"

# Российские CA — их надо предварительно скачать вручную с gosuslugi.ru
# и положить в папку certs/ под этими именами.
RUSSIAN_CAS = [
    CERTS_DIR / "russian_trusted_root_ca.pem",
    CERTS_DIR / "russian_trusted_sub_ca.pem",
]

# Итоговый bundle, который будет использовать наш MaxClient.
OUTPUT_BUNDLE = CERTS_DIR / "ca_bundle.pem"


def main() -> int:
    CERTS_DIR.mkdir(exist_ok=True)

    # Проверка: все ли исходники на месте.
    missing = [p for p in RUSSIAN_CAS if not p.exists()]
    if missing:
        print("ERROR: не найдены исходные сертификаты:", file=sys.stderr)
        for path in missing:
            print(f"  {path}", file=sys.stderr)
        print(
            "\nСкачай их с https://www.gosuslugi.ru/crt и положи в папку certs/.",
            file=sys.stderr,
        )
        return 1

    certifi_bundle = Path(certifi.where())
    print(f"Стандартный CA-bundle: {certifi_bundle}")
    print(f"Собираем расширенный bundle: {OUTPUT_BUNDLE}")

    # 1. Копируем стандартный certifi bundle как основу.
    shutil.copy(certifi_bundle, OUTPUT_BUNDLE)

    # 2. Дописываем русские сертификаты в конец.
    # PEM-формат — обычный текст; несколько сертификатов в одном файле —
    # это просто конкатенация, никаких разделителей не нужно.
    with OUTPUT_BUNDLE.open("a", encoding="utf-8") as out:
        for ca_path in RUSSIAN_CAS:
            print(f"  + добавляем {ca_path.name}")
            out.write("\n")  # для читаемости — пустая строка между сертами
            out.write(ca_path.read_text(encoding="utf-8"))

    print(f"\nГотово: {OUTPUT_BUNDLE} ({OUTPUT_BUNDLE.stat().st_size} байт)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
