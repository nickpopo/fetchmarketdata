import argparse
import os
import re
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("API_KEY")
SECRET_KEY = os.getenv("SECRET_KEY")

BINGX_API_BASE_URL = "https://open-api.bingx.com/openApi"
SPOT_MAX_QUERY_RANGE_MS = 7 * 24 * 60 * 60 * 1000

HEADERS = {
    "X-BX-APIKEY": API_KEY,
}


@dataclass(frozen=True)
class SourceConfig:
    name: str
    market_urls: dict[str, str]
    default_market: str = "swap"
    default_limit: int = 1000
    market_max_query_range_ms: dict[str, int] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    symbol_transform: Callable[[str], str] = lambda symbol: symbol
    response_normalizer: Callable[[str, object], list[dict]] | None = None
    error_message_parser: Callable[[object], Optional[str]] | None = None

    def get_api_url(self, market: str) -> str:
        try:
            return self.market_urls[market]
        except KeyError as exc:
            raise ValueError(f"Unknown market for source {self.name}: {market}") from exc

    def supports_market(self, market: str) -> bool:
        return market in self.market_urls


def normalize_bingx_kline(market: str, raw_kline):
    if isinstance(raw_kline, dict):
        return raw_kline

    if market == "spot" and isinstance(raw_kline, (list, tuple)) and len(raw_kline) >= 6:
        return {
            "time": raw_kline[0],
            "open": raw_kline[1],
            "high": raw_kline[2],
            "low": raw_kline[3],
            "close": raw_kline[4],
            "volume": raw_kline[5],
            "closeTime": raw_kline[6] if len(raw_kline) > 6 else None,
            "quoteVolume": raw_kline[7] if len(raw_kline) > 7 else None,
        }

    raise ValueError(f"Unsupported BingX kline format for market {market}: {raw_kline}")


def normalize_binance_kline(raw_kline):
    if isinstance(raw_kline, dict):
        return raw_kline

    if isinstance(raw_kline, (list, tuple)) and len(raw_kline) >= 6:
        return {
            "time": raw_kline[0],
            "open": raw_kline[1],
            "high": raw_kline[2],
            "low": raw_kline[3],
            "close": raw_kline[4],
            "volume": raw_kline[5],
            "closeTime": raw_kline[6] if len(raw_kline) > 6 else None,
            "quoteVolume": raw_kline[7] if len(raw_kline) > 7 else None,
            "trades": raw_kline[8] if len(raw_kline) > 8 else None,
            "takerBaseVolume": raw_kline[9] if len(raw_kline) > 9 else None,
            "takerQuoteVolume": raw_kline[10] if len(raw_kline) > 10 else None,
        }

    raise ValueError(f"Unsupported Binance kline format: {raw_kline}")


def normalize_klines(raw_klines, normalizer: Callable[[object], dict]):
    normalized = [normalizer(item) for item in raw_klines]
    normalized.sort(key=lambda item: int(item["time"]))
    return normalized


def normalize_bingx_response(market: str, data):
    if data.get("code") and data["code"] != 0:
        raise ValueError(f"API error: {data.get('msg')}")

    return normalize_klines(data["data"], lambda item: normalize_bingx_kline(market, item))


def normalize_binance_response(_market: str, data):
    if isinstance(data, dict) and "msg" in data:
        raise ValueError(f"API error: {data['msg']}")

    return normalize_klines(data, normalize_binance_kline)


def extract_bingx_error_message(data) -> Optional[str]:
    if isinstance(data, dict) and data.get("code") and data["code"] != 0:
        return data.get("msg")
    return None


def extract_binance_error_message(data) -> Optional[str]:
    if isinstance(data, dict) and data.get("msg"):
        return data["msg"]
    return None


def normalize_binance_symbol(symbol: str) -> str:
    return symbol.replace("-", "").replace("/", "").upper()


SOURCES: dict[str, SourceConfig] = {
    "bingx": SourceConfig(
        name="bingx",
        market_urls={
            "swap": f"{BINGX_API_BASE_URL}/swap/v3/quote/klines",
            "spot": f"{BINGX_API_BASE_URL}/spot/v2/market/kline",
        },
        default_market="swap",
        default_limit=1400,
        market_max_query_range_ms={"spot": SPOT_MAX_QUERY_RANGE_MS},
        headers=HEADERS,
        symbol_transform=lambda symbol: symbol,
        response_normalizer=normalize_bingx_response,
        error_message_parser=extract_bingx_error_message,
    ),
    "binance": SourceConfig(
        name="binance",
        market_urls={
            "spot": "https://api.binance.com/api/v3/klines",
            "swap": "https://fapi.binance.com/fapi/v1/klines",
        },
        default_market="spot",
        default_limit=1000,
        symbol_transform=normalize_binance_symbol,
        response_normalizer=normalize_binance_response,
        error_message_parser=extract_binance_error_message,
    ),
}

# Backward-compatible aliases used by existing tests and callers.
API_URLS = SOURCES["bingx"].market_urls


def get_source_config(source: str) -> SourceConfig:
    try:
        return SOURCES[source]
    except KeyError as exc:
        raise ValueError(f"Unknown source: {source}") from exc


def supported_markets_for_source(source: str) -> list[str]:
    return sorted(get_source_config(source).market_urls.keys())


# ======== HELPERS =======
def get_downloads_path(market: str, source: str = "bingx"):
    modpath = os.path.dirname(os.path.abspath(__file__))
    downloads_path = os.path.join(modpath, "downloads", source, market)
    os.makedirs(downloads_path, exist_ok=True)
    return downloads_path


def get_filepath(filename: str, market: str, source: str = "bingx"):
    return os.path.join(get_downloads_path(market, source), filename)


def build_data_filename(
    symbol: str,
    interval: str,
    fromdate: datetime,
    todate: datetime,
    market: str,
    source: str,
):
    return (
        f"{symbol}_{interval}_{fromdate.strftime('%Y-%m-%d')}_{todate.strftime('%Y-%m-%d')}_"
        f"{source}_{market}.csv"
    )


def build_part_filename(symbol: str, interval: str, end_date: str, market: str, source: str):
    return f"{symbol}_{interval}_{end_date}_update_{source}_{market}.part.csv"


def get_api_url(source: str, market: str) -> str:
    return get_source_config(source).get_api_url(market)


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
write_log_enabled = False


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


def get_request_end_ms(
    source: str,
    market: str,
    interval: str,
    start_ms: int,
    final_end_ms: int,
    limit: int,
) -> int:
    config = get_source_config(source)
    interval_ms = interval_to_ms(interval)
    max_by_limit = start_ms + (limit * interval_ms) - interval_ms
    end_candidates = [final_end_ms, max_by_limit]

    max_by_market = config.market_max_query_range_ms.get(market)
    if max_by_market is not None:
        end_candidates.append(start_ms + max_by_market - interval_ms)

    return min(end_candidates)


def is_spot_range_error(error: Exception) -> bool:
    return "maximum query range" in str(error).lower()


def fetch_klines_with_adaptive_range(
    source: str,
    market: str,
    symbol: str,
    interval: str,
    start_ms: int,
    final_end_ms: int,
    limit: Optional[int] = None,
):
    config = get_source_config(source)
    request_limit = limit or config.default_limit
    request_end_ms = get_request_end_ms(
        source=source,
        market=market,
        interval=interval,
        start_ms=start_ms,
        final_end_ms=final_end_ms,
        limit=request_limit,
    )
    interval_ms = interval_to_ms(interval)

    while True:
        try:
            return (
                fetch_klines(source, market, symbol, interval, start_ms, request_end_ms, request_limit),
                request_end_ms,
            )
        except ValueError as exc:
            if source != "bingx" or market != "spot" or not is_spot_range_error(exc):
                raise

            next_end_ms = start_ms + max(((request_end_ms - start_ms) // 2), interval_ms)
            next_end_ms = max(start_ms + interval_ms, next_end_ms)

            if next_end_ms >= request_end_ms:
                raise

            request_end_ms = next_end_ms


def build_request_params(
    source: str,
    symbol: str,
    interval: str,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    limit: Optional[int] = None,
):
    config = get_source_config(source)
    params = {
        "symbol": config.symbol_transform(symbol),
        "interval": interval,
        "limit": limit or config.default_limit,
    }

    if start_ms is not None:
        params["startTime"] = start_ms

    if end_ms is not None:
        params["endTime"] = end_ms

    return params


def fetch_klines(
    source: str,
    market: str,
    symbol: str,
    interval: str,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    limit: Optional[int] = None,
):
    config = get_source_config(source)
    params = build_request_params(source, symbol, interval, start_ms, end_ms, limit)
    resp = requests.get(
        get_api_url(source, market),
        params=params,
        headers=config.headers,
        timeout=10,
    )
    resp.raise_for_status()

    data = resp.json()
    if config.response_normalizer is None:
        raise ValueError(f"Source {source} does not have a response normalizer configured.")

    return config.response_normalizer(market, data)


def write_log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if not write_log_enabled or not logfilepath:
        return
    with open(logfilepath, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


def write_log_to(msg: str, target_logfilepath: Optional[str]):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if not write_log_enabled or not target_logfilepath:
        return
    with open(target_logfilepath, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


def save_klines_progress(klines, target_filepath: Optional[str], target_logfilepath: Optional[str]):
    if not klines or not target_filepath:
        return

    df = pd.DataFrame(klines)
    df.sort_values(by="time", inplace=True)
    df.drop_duplicates(subset="time", inplace=True)

    df["time"] = pd.to_datetime(df["time"], unit="ms")
    df.to_csv(target_filepath, index=False)

    last_time = df["time"].max()
    msg = f"Сохранено {len(df)} свечей, последняя свеча: {last_time}"

    print(f"{Color.GREEN}{msg}{Color.RESET}")
    write_log_to(msg, target_logfilepath)


def save_progress():
    global all_klines, filepath

    save_klines_progress(all_klines, filepath, logfilepath)


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


def validate_market_for_source(source: str, market: str):
    if not get_source_config(source).supports_market(market):
        supported = ", ".join(supported_markets_for_source(source))
        raise ValueError(f"Source '{source}' does not support market '{market}'. Supported: {supported}")


def fetch_and_save(
    source="bingx",
    market: Optional[str] = None,
    symbol="ETH-USDT",
    interval="5m",
    fromdate=None,
    todate=None,
):
    global all_klines, filepath, logfilepath

    config = get_source_config(source)
    market = market or config.default_market
    validate_market_for_source(source, market)

    if todate is None:
        todate = datetime.now()

    if fromdate is None:
        fromdate = todate - timedelta(days=60)

    filename = build_data_filename(symbol, interval, fromdate, todate, market, source)
    logfilename = filename.replace(".csv", ".log")
    filepath = get_filepath(filename, market, source)
    logfilepath = get_filepath(logfilename, market, source)

    log_start_msg = (
        f"[START] Запуск скрипта с параметрами:\n"
        f"  source = {source}\n"
        f"  market = {market}\n"
        f"  symbol = {symbol}\n"
        f"  interval = {interval}\n"
        f"  from_date = {fromdate.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"  to_date   = {todate.strftime('%Y-%m-%d %H:%M:%S')}\n"
    )
    print(f"{Color.CYAN}{log_start_msg}{Color.RESET}")
    write_log(log_start_msg)

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
    request_limit = config.default_limit

    while current_time_ms < end_ms and not interrupted:
        try:
            chunk, request_end_ms = fetch_klines_with_adaptive_range(
                source=source,
                market=market,
                symbol=symbol,
                interval=interval,
                start_ms=current_time_ms,
                final_end_ms=end_ms,
                limit=request_limit,
            )
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

        last_open_ms = chunk[-1]["time"]
        current_time_ms = int(last_open_ms) + interval_to_ms(interval)

        print(
            f"{Color.CYAN}Загружено {len(all_klines)} свечей, до {ms_to_datetime(last_open_ms)}{Color.RESET}"
        )

        if len(chunk) < request_limit:
            break

        time.sleep(0.5)

    save_progress()
    write_log("[END] Загрузка завершена")
    print(f"{Color.BOLD}{Color.GREEN}✅ [END] Загрузка завершена.{Color.RESET}")


def valid_date(date_str):
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Неверный формат даты '{date_str}'. Используйте YYYY-MM-DD, например: 2025-08-01"
        )


def parse_download_filename(filename: str):
    pattern = (
        r"^(?P<symbol>[^_]+)_(?P<interval>[^_]+)_(?P<start>\d{4}-\d{2}-\d{2})_"
        r"(?P<end>\d{4}-\d{2}-\d{2})_(?P<source>[^_]+)_(?P<market>swap|spot)\.csv$"
    )
    match = re.match(pattern, filename)
    return match.groupdict() if match else None


def fetch_to_file_with_resume(
    source: str,
    market: str,
    symbol: str,
    interval: str,
    fromdate: datetime,
    todate: datetime,
    target_path: str,
):
    local_klines = []
    local_log_path = (
        target_path.replace(".csv", ".log")
        if target_path.lower().endswith(".csv")
        else f"{target_path}.log"
    )
    end_ms = datetime_to_ms(todate)
    request_limit = get_source_config(source).default_limit

    if os.path.exists(target_path):
        df = pd.read_csv(target_path)
        if not df.empty and "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"])
            last_time_dt = df["time"].max()
            current_time_ms = datetime_to_ms(last_time_dt) + interval_to_ms(interval)
            df["time"] = datetime_series_to_ms(df["time"])
            local_klines = df.to_dict("records")
            msg = (
                f"[RESUME PART] Продолжаем докачку из {os.path.basename(target_path)} "
                f"с {last_time_dt}"
            )
            print(f"{Color.CYAN}{msg}{Color.RESET}")
            write_log_to(msg, local_log_path)
        else:
            current_time_ms = datetime_to_ms(fromdate)
    else:
        current_time_ms = datetime_to_ms(fromdate)

    while current_time_ms < end_ms and not interrupted:
        try:
            chunk, request_end_ms = fetch_klines_with_adaptive_range(
                source=source,
                market=market,
                symbol=symbol,
                interval=interval,
                start_ms=current_time_ms,
                final_end_ms=end_ms,
                limit=request_limit,
            )
        except Exception as e:
            msg = f"[ERROR] Ошибка сети: {e}, жду 10 сек..."
            print(f"{Color.YELLOW}{msg}{Color.RESET}")
            write_log(msg)
            time.sleep(10)
            continue

        if not chunk:
            break

        local_klines.extend(chunk)

        if len(local_klines) % 5000 < 1000:
            save_klines_progress(local_klines, target_path, local_log_path)

        prev_time_ms = current_time_ms
        last_open_ms = int(chunk[-1]["time"])
        current_time_ms = last_open_ms + interval_to_ms(interval)
        if current_time_ms <= prev_time_ms:
            write_log_to(
                "[ERROR] Некорректный шаг времени от API, останавливаю докачку.",
                local_log_path,
            )
            break

        print(
            f"{Color.CYAN}Докачано {len(local_klines)} свечей в {os.path.basename(target_path)}, до {ms_to_datetime(last_open_ms)}{Color.RESET}"
        )

        if len(chunk) < request_limit:
            break

        time.sleep(0.5)

    save_klines_progress(local_klines, target_path, local_log_path)
    return not interrupted


def _build_update_task(downloads_path: str, filename: str, today):
    parsed = parse_download_filename(filename)
    if not parsed:
        return None

    source = parsed["source"]
    market = parsed["market"]
    symbol = parsed["symbol"]
    interval = parsed["interval"]
    csv_path = os.path.join(downloads_path, filename)

    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        print(f"{Color.RED}Не удалось прочитать {filename}: {e}{Color.RESET}")
        return None

    if df.empty or "time" not in df.columns:
        print(
            f"{Color.YELLOW}Пропускаю {filename}: файл пустой или нет колонки time.{Color.RESET}"
        )
        return None

    df["time"] = pd.to_datetime(df["time"])
    last_time = df["time"].max()
    last_date = last_time.date()
    if last_date >= today:
        print(f"{Color.GREEN}{filename}: уже актуален ({last_date}).{Color.RESET}")
        return None

    from_dt = last_time + timedelta(milliseconds=interval_to_ms(interval))
    end_dt = datetime.now()
    if from_dt >= end_dt:
        print(f"{Color.GREEN}{filename}: уже актуален ({last_date}).{Color.RESET}")
        return None

    return {
        "downloads_path": downloads_path,
        "filename": filename,
        "parsed": parsed,
        "source": source,
        "market": market,
        "symbol": symbol,
        "interval": interval,
        "file_start": parsed["start"],
        "file_end": parsed["end"],
        "csv_path": csv_path,
        "from_dt": from_dt,
        "end_dt": end_dt,
    }


def _update_existing_file_task(task):
    filename = task["filename"]
    source = task["source"]
    market = task["market"]
    symbol = task["symbol"]
    interval = task["interval"]
    file_start = task["file_start"]
    csv_path = task["csv_path"]
    from_dt = task["from_dt"]
    end_dt = task["end_dt"]
    parsed = task["parsed"]
    downloads_path = task["downloads_path"]

    part_filename = build_part_filename(symbol, interval, parsed["end"], market, source)
    part_csv_path = os.path.join(downloads_path, part_filename)
    part_log_path = os.path.join(downloads_path, part_filename.replace(".csv", ".log"))
    msg = (
        f"[UPDATE START] {filename}: отдельная докачка в {part_filename} "
        f"с {from_dt.strftime('%Y-%m-%d %H:%M:%S')} до {end_dt.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    print(f"{Color.CYAN}{msg}{Color.RESET}")
    write_log_to(msg, part_log_path)

    completed = fetch_to_file_with_resume(
        source=source,
        symbol=symbol,
        interval=interval,
        market=market,
        fromdate=from_dt,
        todate=end_dt,
        target_path=part_csv_path,
    )
    if not completed:
        warning_msg = (
            f"Докачка прервана, прогресс сохранён в {part_filename}. "
            "Повторный запуск продолжит с этого места."
        )
        print(f"{Color.YELLOW}{warning_msg}{Color.RESET}")
        return {"status": "interrupted", "filename": filename}

    if not os.path.exists(part_csv_path):
        print(f"{Color.YELLOW}{filename}: файл докачки не создан, пропускаю.{Color.RESET}")
        return {"status": "skipped", "filename": filename}

    base_df = pd.read_csv(csv_path)
    part_df = pd.read_csv(part_csv_path)
    if part_df.empty or "time" not in part_df.columns:
        print(f"{Color.YELLOW}{filename}: новых данных не получено.{Color.RESET}")
        return {"status": "skipped", "filename": filename}

    base_df["time"] = pd.to_datetime(base_df["time"])
    part_df["time"] = pd.to_datetime(part_df["time"])
    merged = pd.concat([base_df, part_df], ignore_index=True)
    merged.sort_values(by="time", inplace=True)
    merged.drop_duplicates(subset="time", inplace=True)

    new_last_time = merged["time"].max()
    new_end = new_last_time.strftime("%Y-%m-%d")
    new_filename = build_data_filename(
        symbol=symbol,
        interval=interval,
        fromdate=datetime.strptime(file_start, "%Y-%m-%d"),
        todate=datetime.strptime(new_end, "%Y-%m-%d"),
        market=market,
        source=source,
    )
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

    write_log_to(
        f"[UPDATE END] Обновлён файл {new_filename}. Всего свечей: {len(merged)}, последняя: {new_last_time}.",
        new_log_path,
    )
    print(
        f"{Color.GREEN}✅ Обновлён: {new_filename}, свечей: {len(merged)}, до {new_last_time}{Color.RESET}"
    )
    return {"status": "updated", "filename": filename}


def _update_existing_files_in_dir(downloads_path: str, multithreading: bool = True):
    today = datetime.now().date()

    if not os.path.exists(downloads_path):
        print(f"{Color.YELLOW}Папка не найдена: {downloads_path}{Color.RESET}")
        return

    csv_files = sorted(f for f in os.listdir(downloads_path) if f.lower().endswith(".csv"))
    if not csv_files:
        print(f"{Color.YELLOW}В downloads нет CSV файлов для обновления.{Color.RESET}")
        return

    tasks = []
    for filename in csv_files:
        task = _build_update_task(downloads_path, filename, today)
        if task:
            tasks.append(task)

    if not tasks:
        print(f"{Color.GREEN}Нет файлов, требующих обновления.{Color.RESET}")
        return

    if not multithreading or len(tasks) == 1:
        for task in tasks:
            result = _update_existing_file_task(task)
            if result["status"] == "interrupted":
                return
        return

    workers = min(os.cpu_count() or 1, len(tasks))
    print(
        f"{Color.CYAN}Многопоточный режим: {workers} поток(ов), файлов к обновлению: {len(tasks)}.{Color.RESET}"
    )
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_update_existing_file_task, task) for task in tasks]
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as e:
                print(f"{Color.RED}Ошибка в потоке обновления: {e}{Color.RESET}")
                continue
            if result.get("status") == "interrupted":
                return


def update_existing_files(
    multithreading: bool = True,
    market: Optional[str] = None,
    source: Optional[str] = None,
):
    if source:
        markets = [market] if market else supported_markets_for_source(source)
        for market_name in markets:
            validate_market_for_source(source, market_name)
            _update_existing_files_in_dir(
                get_downloads_path(market_name, source),
                multithreading=multithreading,
            )
        return

    for source_name, config in SOURCES.items():
        markets = [market] if market else sorted(config.market_urls.keys())
        for market_name in markets:
            validate_market_for_source(source_name, market_name)
            _update_existing_files_in_dir(
                get_downloads_path(market_name, source_name),
                multithreading=multithreading,
            )


def main():
    global write_log_enabled

    source_names = sorted(SOURCES.keys())
    all_markets = sorted({market for config in SOURCES.values() for market in config.market_urls})
    source_default_text = ", ".join(
        f"{name}: {config.default_market}" for name, config in sorted(SOURCES.items())
    )

    parser = argparse.ArgumentParser(
        description="""Скачивание исторических данных свечей из разных источников.
Примеры использования:
1. По умолчанию (bingx, ETH-USDT, 5m, 60 дней):
   python bingx_fetch.py
2. С конкретной парой и интервалом:
   python bingx_fetch.py --symbol BTC-USDT --interval 1h
3. Скачать из Binance:
   python bingx_fetch.py --source binance --market spot --symbol BTC-USDT --interval 1h
4. Обновить все скачанные рынки из всех источников:
   python bingx_fetch.py --update-existing
5. За конкретный период:
   python bingx_fetch.py --symbol ETH-USDT --interval 5m --fromdate 2025-07-01 --todate 2025-08-01
"""
    )
    parser.add_argument(
        "--source",
        type=str,
        choices=source_names,
        default="bingx",
        help="Источник данных. Сейчас поддерживаются: %(choices)s",
    )
    parser.add_argument(
        "--market",
        type=str,
        choices=all_markets,
        default=None,
        help=(
            "Рынок для загрузки. Если не указан, используется дефолтный рынок источника "
            f"({source_default_text}). Для --update-existing без --source обновляются все источники и рынки."
        ),
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default="ETH-USDT",
        help="Торговая пара (например ETH-USDT; для Binance будет автоматически преобразована в BTCUSDT-формат)",
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
    parser.add_argument(
        "--write-log",
        action="store_true",
        help="Записывать лог в .log файл (по умолчанию лог пишется только в консоль)",
    )
    parser.add_argument(
        "--no-multithreading",
        action="store_true",
        help="Для --update-existing: отключить многопоточность и обновлять файлы последовательно",
    )
    args = parser.parse_args()
    write_log_enabled = args.write_log

    if args.market:
        validate_market_for_source(args.source, args.market)

    source_was_explicit = any(
        arg == "--source" or arg.startswith("--source=") for arg in sys.argv[1:]
    )

    if args.update_existing:
        if args.lastdays or args.fromdate or args.todate:
            raise ValueError(
                "С флагом --update-existing нельзя использовать --lastdays, --fromdate и --todate."
            )
        update_existing_files(
            multithreading=not args.no_multithreading,
            market=args.market,
            source=args.source if source_was_explicit else None,
        )
        return

    if args.lastdays and (args.fromdate or args.todate):
        raise ValueError(
            "Если указан --lastdays, то нельзя задавать --fromdate или --todate. --lastdays имеет приоритет."
        )

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
        todate = datetime.now()
        fromdate = args.fromdate
        if fromdate >= todate:
            raise ValueError(
                f"Дата начала {fromdate.strftime('%Y-%m-%d')} должна быть меньше даты конца {todate.strftime('%Y-%m-%d')}."
            )
    else:
        todate = datetime.now()
        fromdate = todate - timedelta(days=60)

    fetch_and_save(
        source=args.source,
        market=args.market,
        symbol=args.symbol,
        interval=args.interval,
        fromdate=fromdate,
        todate=todate,
    )


if __name__ == "__main__":
    main()
