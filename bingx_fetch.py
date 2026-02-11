import os
import time
import signal
import argparse
import re
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
    return int(pd.Timestamp(foo).value // 1_000_000)


def ms_to_datetime(foo):
    return datetime.fromtimestamp(foo / 1000)


def datetime_series_to_ms(series):
    dt = pd.to_datetime(series)
    return dt.map(datetime_to_ms)
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
    if not logfilepath:
        return
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

    last_time = df["time"].max()
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
            df["time"] = datetime_series_to_ms(df["time"])
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


def parse_download_filename(filename: str):
    pattern = r"^(?P<symbol>[^_]+)_(?P<interval>[^_]+)_(?P<start>\d{4}-\d{2}-\d{2})_(?P<end>\d{4}-\d{2}-\d{2})\.csv$"
    match = re.match(pattern, filename)
    return match.groupdict() if match else None


def fetch_to_file_with_resume(
    symbol: str, interval: str, fromdate: datetime, todate: datetime, target_path: str
):
    global all_klines, filepath

    all_klines = []
    filepath = target_path
    end_ms = datetime_to_ms(todate)

    if os.path.exists(target_path):
        df = pd.read_csv(target_path)
        if not df.empty and "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"])
            last_time_dt = df["time"].max()
            current_time_ms = datetime_to_ms(last_time_dt) + interval_to_ms(interval)
            df["time"] = datetime_series_to_ms(df["time"])
            all_klines = df.to_dict("records")
            msg = (
                f"[RESUME PART] Продолжаем докачку из {os.path.basename(target_path)} "
                f"с {last_time_dt}"
            )
            print(f"{Color.CYAN}{msg}{Color.RESET}")
            write_log(msg)
        else:
            current_time_ms = datetime_to_ms(fromdate)
    else:
        current_time_ms = datetime_to_ms(fromdate)

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

        prev_time_ms = current_time_ms
        last_open_ms = int(chunk[0]["time"])
        current_time_ms = last_open_ms + interval_to_ms(interval)
        if current_time_ms <= prev_time_ms:
            write_log("[ERROR] Некорректный шаг времени от API, останавливаю докачку.")
            break

        print(
            f"{Color.CYAN}Докачано {len(all_klines)} свечей в {os.path.basename(target_path)}, до {ms_to_datetime(last_open_ms)}{Color.RESET}"
        )

        if len(chunk) < 1000:
            break

        time.sleep(0.5)

    save_progress()
    return not interrupted


def update_existing_files():
    global logfilepath

    modpath = os.path.dirname(os.path.abspath(__file__))
    downloads_path = os.path.join(modpath, "downloads")
    today = datetime.now().date()

    if not os.path.exists(downloads_path):
        print(f"{Color.YELLOW}Папка downloads не найдена: {downloads_path}{Color.RESET}")
        return

    csv_files = sorted(f for f in os.listdir(downloads_path) if f.lower().endswith(".csv"))
    if not csv_files:
        print(f"{Color.YELLOW}В downloads нет CSV файлов для обновления.{Color.RESET}")
        return

    for filename in csv_files:
        parsed = parse_download_filename(filename)
        if not parsed:
            continue

        symbol = parsed["symbol"]
        interval = parsed["interval"]
        file_start = parsed["start"]
        csv_path = os.path.join(downloads_path, filename)
        logfilepath = get_filepath(filename.replace(".csv", ".log"))

        try:
            df = pd.read_csv(csv_path)
        except Exception as e:
            print(f"{Color.RED}Не удалось прочитать {filename}: {e}{Color.RESET}")
            continue

        if df.empty or "time" not in df.columns:
            print(
                f"{Color.YELLOW}Пропускаю {filename}: файл пустой или нет колонки time.{Color.RESET}"
            )
            continue

        df["time"] = pd.to_datetime(df["time"])
        last_time = df["time"].max()
        last_date = last_time.date()

        if last_date >= today:
            print(f"{Color.GREEN}{filename}: уже актуален ({last_date}).{Color.RESET}")
            continue

        from_dt = last_time + timedelta(milliseconds=interval_to_ms(interval))
        end_dt = datetime.now()
        if from_dt >= end_dt:
            print(f"{Color.GREEN}{filename}: уже актуален ({last_date}).{Color.RESET}")
            continue

        part_filename = f"{symbol}_{interval}_{parsed['end']}_update.part.csv"
        part_csv_path = os.path.join(downloads_path, part_filename)
        part_log_path = os.path.join(downloads_path, part_filename.replace(".csv", ".log"))
        logfilepath = part_log_path
        msg = (
            f"[UPDATE START] {filename}: отдельная докачка в {part_filename} "
            f"с {from_dt.strftime('%Y-%m-%d %H:%M:%S')} до {end_dt.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        print(f"{Color.CYAN}{msg}{Color.RESET}")
        write_log(msg)

        completed = fetch_to_file_with_resume(
            symbol=symbol,
            interval=interval,
            fromdate=from_dt,
            todate=end_dt,
            target_path=part_csv_path,
        )
        if not completed:
            print(
                f"{Color.YELLOW}Докачка прервана, прогресс сохранён в {part_filename}. Повторный запуск продолжит с этого места.{Color.RESET}"
            )
            return

        if not os.path.exists(part_csv_path):
            print(f"{Color.YELLOW}{filename}: файл докачки не создан, пропускаю.{Color.RESET}")
            continue

        part_df = pd.read_csv(part_csv_path)
        if part_df.empty or "time" not in part_df.columns:
            print(f"{Color.YELLOW}{filename}: новых данных не получено.{Color.RESET}")
            continue

        part_df["time"] = pd.to_datetime(part_df["time"])
        merged = pd.concat([df, part_df], ignore_index=True)
        merged.sort_values(by="time", inplace=True)
        merged.drop_duplicates(subset="time", inplace=True)

        new_last_time = merged["time"].max()
        new_end = new_last_time.strftime("%Y-%m-%d")
        new_filename = f"{symbol}_{interval}_{file_start}_{new_end}.csv"
        new_logfilename = new_filename.replace(".csv", ".log")
        new_csv_path = os.path.join(downloads_path, new_filename)
        new_log_path = os.path.join(downloads_path, new_logfilename)

        merged.to_csv(new_csv_path, index=False)

        if filename != new_filename and os.path.exists(csv_path):
            os.remove(csv_path)

        if os.path.exists(part_log_path):
            if os.path.exists(new_log_path):
                with open(part_log_path, "r", encoding="utf-8") as src, open(
                    new_log_path, "a", encoding="utf-8"
                ) as dst:
                    dst.write(src.read())
                os.remove(part_log_path)
            else:
                os.replace(part_log_path, new_log_path)
        if os.path.exists(part_csv_path):
            os.remove(part_csv_path)

        logfilepath = new_log_path
        write_log(
            f"[UPDATE END] Обновлён файл {new_filename}. Всего свечей: {len(merged)}, последняя: {new_last_time}."
        )
        print(
            f"{Color.GREEN}✅ Обновлён: {new_filename}, свечей: {len(merged)}, до {new_last_time}{Color.RESET}"
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
    parser.add_argument(
        "--update-existing",
        action="store_true",
        help="Обновить все существующие CSV в downloads до текущей даты",
    )
    args = parser.parse_args()

    if args.update_existing:
        if args.lastdays or args.fromdate or args.todate:
            raise ValueError(
                "С флагом --update-existing нельзя использовать --lastdays, --fromdate и --todate."
            )
        update_existing_files()
        return

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
