import os
import time
import signal
import argparse
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from typing import Optional

load_dotenv()

API_URL = "https://open-api.bingx.com/openApi/swap/v3/quote/klines"
API_KEY = os.getenv("API_KEY")
SECRET_KEY = os.getenv("SECRET_KEY")

HEADERS = {
    "X-BX-APIKEY": API_KEY,
}

# ======== HELPERS =======
def get_filepath(filename):
    MODPATH = os.path.dirname(os.path.abspath(__file__))
    downloads_path = os.path.join(MODPATH, "downloads")
    os.makedirs(downloads_path, exist_ok=True)
    return os.path.join(downloads_path, filename)


def datetime_to_ms(foo):
    return int(foo.timestamp() * 1000)


def ms_to_datetime(foo):
    return datetime.fromtimestamp(foo / 1000)
# ====== END HELPERS ======

# глобальные переменные
all_klines = []
filepath = None
logfilepath = None
interrupted = False


# --- Цвета для консоли ---
class Color:
    RESET = "\033[0m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"


# --- Мапа интервалов ---
interval_to_minutes = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "4h": 240,
    "1d": 1440,
    "3d": 4320,
}


def interval_to_ms(interval: str) -> int:
    minutes = interval_to_minutes.get(interval)
    if minutes is not None:
        return minutes * 60 * 1000
    raise ValueError(f"Unknown interval: {interval}")


def fetch_klines(
    symbol: str,
    interval: str,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    limit: int = 1400,
):
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    }

    if start_ms is not None:
        params["startTime"] = start_ms

    if end_ms is not None:
        params["endTime"] = end_ms

    resp = requests.get(API_URL, params=params, headers=HEADERS, timeout=10)
    resp.raise_for_status()

    data = resp.json()
    if data.get("code") and data["code"] != 0:
        raise ValueError(f"API error: {data.get('msg')}")

    return data["data"]


def write_log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with open(logfilepath, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


def save_progress():
    global all_klines, filepath

    if not all_klines or not filepath:
        return

    df = pd.DataFrame(all_klines)

    # сортируем и удаляем дубликаты
    df.sort_values(by="time", inplace=True)
    df.drop_duplicates(subset="time", inplace=True)

    df["time"] = pd.to_datetime(df["time"], unit="ms")
    df.to_csv(filepath, index=False)

    last_time = pd.to_datetime(df["time"].max(), unit="ms")
    msg = f"Сохранено {len(df)} свечей, последняя свеча: {last_time}"

    print(f"{Color.GREEN}{msg}{Color.RESET}")
    write_log(msg)


def signal_handler(sig, frame):
    global interrupted
    interrupted = True
    print(
        f"\n{Color.RED}{Color.BOLD}⚠️ Программа остановлена пользователем (Ctrl+C). Сохраняю прогресс...{Color.RESET}"
    )
    save_progress()
    write_log("Программа остановлена пользователем.")
    exit(0)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def fetch_and_save(symbol="ETH-USDT", interval="5m", fromdate=None, todate=None):
    global all_klines, filepath, logfilepath

    if todate is None:
        todate = datetime.now()

    if fromdate is None:
        fromdate = todate - timedelta(days=60)

    filename = f"{symbol}_{interval}_{fromdate.strftime('%Y-%m-%d')}_{todate.strftime('%Y-%m-%d')}.csv"
    logfilename = filename.replace(".csv", ".log")
    filepath = get_filepath(filename)
    logfilepath = get_filepath(logfilename)

    # Логируем старт
    log_start_msg = (
        f"[START] Запуск скрипта с параметрами:\n"
        f"  symbol = {symbol}\n"
        f"  interval = {interval}\n"
        f"  from_date = {fromdate.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"  to_date   = {todate.strftime('%Y-%m-%d %H:%M:%S')}\n"
    )
    print(f"{Color.CYAN}{log_start_msg}{Color.RESET}")
    write_log(log_start_msg)

    # Проверка существующего CSV
    if os.path.exists(filepath):
        df = pd.read_csv(filepath)
        if not df.empty:
            df["time"] = pd.to_datetime(df["time"])
            last_time_dt = df["time"].max()
            msg = f"[RESUME] Продолжаем загрузку с {last_time_dt}"
            print(f"{Color.CYAN}{msg}{Color.RESET}")
            write_log(msg)
            current_time_ms = datetime_to_ms(last_time_dt) + interval_to_ms(interval)
            df['time'] = df["time"].astype("int64") // 10**6
            all_klines = df.to_dict("records")
        else:
            all_klines = []
            current_time_ms = datetime_to_ms(fromdate)
    else:
        write_log(f"[NEW FILE] Новый файл: начало {fromdate}, конец {todate}")
        print(
            f"{Color.CYAN}[NEW FILE] Новый файл: начало {fromdate}, конец {todate}{Color.RESET}"
        )
        all_klines = []
        current_time_ms = datetime_to_ms(fromdate)

    end_ms = datetime_to_ms(todate)

    # Основной цикл загрузки
    while current_time_ms < end_ms and not interrupted:
        try:
            chunk = fetch_klines(symbol, interval, current_time_ms)
        except Exception as e:
            msg = f"[ERROR] Ошибка сети: {e}, жду 10 сек..."
            print(f"{Color.YELLOW}{msg}{Color.RESET}")
            write_log(msg)
            time.sleep(10)
            continue

        if not chunk:
            break

        all_klines.extend(chunk)

        if len(all_klines) % 5000 < 1000:
            save_progress()

        last_open_ms = chunk[0]["time"]
        current_time_ms = int(last_open_ms) + interval_to_ms(interval)

        print(
            f"{Color.CYAN}Загружено {len(all_klines)} свечей, до {ms_to_datetime(last_open_ms)}{Color.RESET}"
        )

        if len(chunk) < 1000:
            break

        time.sleep(0.5)  # защита от rate limit

    # Логируем завершение
    save_progress()
    write_log("[END] Загрузка завершена")
    print(f"{Color.BOLD}{Color.GREEN}✅ [END] Загрузка завершена.{Color.RESET}")


def valid_date(date_str):
    """Проверка формата даты YYYY-MM-DD"""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Неверный формат даты '{date_str}'. Используйте YYYY-MM-DD, например: 2025-08-01"
        )


def main():
    parser = argparse.ArgumentParser(
        description="""Скачивание исторических данных свечей с BingX API.
Примеры использования:
1. По умолчанию (ETH-USDT, 5m, 60 дней):
   python bingx_fetch.py
2. С конкретной парой и интервалом:
   python bingx_fetch.py --symbol BTC-USDT --interval 1h
3. За последние N дней:
   python bingx_fetch.py --symbol ETH-USDT --interval 5m --lastdays 90
4. За конкретный период:
   python bingx_fetch.py --symbol ETH-USDT --interval 5m --fromdate 2025-07-01 --todate 2025-08-01
"""
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default="ETH-USDT",
        help="Торговая пара (например ETH-USDT)",
    )
    parser.add_argument(
        "--interval",
        type=str,
        default="5m",
        help="Интервал свечей (например 1m, 5m, 15m, 1h, 1d)",
    )
    parser.add_argument(
        "--lastdays", type=int, help="За сколько последних дней загрузить данные"
    )
    parser.add_argument(
        "--fromdate", type=valid_date, help="Дата начала периода YYYY-MM-DD"
    )
    parser.add_argument(
        "--todate", type=valid_date, help="Дата конца периода YYYY-MM-DD"
    )
    args = parser.parse_args()

    # Проверка конфликтов
    if args.lastdays and (args.fromdate or args.todate):
        raise ValueError(
            "Если указан --lastdays, то нельзя задавать --fromdate или --todate. --lastdays имеет приоритет."
        )

    # Определяем период
    if args.lastdays:
        todate = datetime.now()
        fromdate = todate - timedelta(days=args.lastdays)
    elif args.fromdate and args.todate:
        todate = args.todate
        fromdate = args.fromdate
        if fromdate >= todate:
            raise ValueError(
                f"Дата начала {fromdate.strftime('%Y-%m-%d')} должна быть меньше даты конца {todate.strftime('%Y-%m-%d')}."
            )
    elif args.fromdate:
        # По умолчанию 60 дней
        todate = datetime.now()
        fromdate = args.fromdate
        if fromdate >= todate:
            raise ValueError(
                f"Дата начала {fromdate.strftime('%Y-%m-%d')} должна быть меньше даты конца {todate.strftime('%Y-%m-%d')}."
            )
    else:
        # По умолчанию 60 дней
        todate = datetime.now()
        fromdate = todate - timedelta(days=60)

    fetch_and_save(
        symbol=args.symbol, interval=args.interval, fromdate=fromdate, todate=todate
    )


if __name__ == "__main__":
    main()
