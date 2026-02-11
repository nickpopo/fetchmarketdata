# fetchmarketdata

Утилита для загрузки исторических свечей (klines) с BingX и сохранения в CSV.

## Что делает проект
- Загружает свечи по торговой паре и интервалу через BingX API.
- Поддерживает период через `--lastdays` или `--fromdate/--todate`.
- Умеет продолжать загрузку в существующий CSV.
- Пишет лог в `.log` файл рядом с CSV.
- Сохраняет файлы в папку `downloads/`.

## Требования
- Python 3.10+
- Доступ в интернет к `open-api.bingx.com`

## Разворачивание
1. Клонировать репозиторий и перейти в папку проекта.
2. Создать и активировать виртуальное окружение.
3. Установить проект.
4. (Опционально) Создать `.env` с API-ключами.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

Пример `.env`:

```env
API_KEY=your_api_key
SECRET_KEY=your_secret_key
```

## Запуск
После установки доступны два варианта:

```bash
# через entrypoint из pyproject.toml
bingx-fetch

# напрямую скриптом
python bingx_fetch.py
```

## Примеры
```bash
# По умолчанию: ETH-USDT, 5m, последние 60 дней
bingx-fetch

# Пара и интервал
bingx-fetch --symbol BTC-USDT --interval 1h

# Последние N дней
bingx-fetch --symbol ETH-USDT --interval 5m --lastdays 90

# Конкретный диапазон дат
bingx-fetch --symbol ETH-USDT --interval 5m --fromdate 2025-07-01 --todate 2025-08-01
```

## Результаты
- CSV со свечами: `downloads/<symbol>_<interval>_<from>_<to>.csv`
- Лог выполнения: `downloads/<symbol>_<interval>_<from>_<to>.log`
