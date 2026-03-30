import argparse
import builtins
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import bingx_fetch


def _write_csv(path, rows):
    df = pd.DataFrame(rows, columns=["open", "close", "high", "low", "volume", "time"])
    df.to_csv(path, index=False)


def _prepare_module_state(monkeypatch, tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bingx_fetch, "__file__", str(tmp_path / "bingx_fetch.py"), raising=False)
    monkeypatch.setattr(bingx_fetch, "all_klines", [])
    monkeypatch.setattr(bingx_fetch, "filepath", None)
    monkeypatch.setattr(bingx_fetch, "logfilepath", None)
    monkeypatch.setattr(bingx_fetch, "interrupted", False)
    monkeypatch.setattr(bingx_fetch, "write_log_enabled", False)
    return downloads


def test_get_filepath_creates_downloads_directory(monkeypatch, tmp_path):
    _prepare_module_state(monkeypatch, tmp_path)

    path = bingx_fetch.get_filepath("sample.csv")

    assert path == str(tmp_path / "downloads" / "sample.csv")
    assert (tmp_path / "downloads").is_dir()


def test_datetime_helpers_convert_values():
    dt = datetime(2020, 1, 1, 0, 0, 0)
    ms = bingx_fetch.datetime_to_ms(dt)

    assert ms == 1577836800000
    assert bingx_fetch.ms_to_datetime(ms).timestamp() == pytest.approx(ms / 1000)

    series = pd.Series(["2020-01-01 00:00:00", "2020-01-01 00:01:00"])
    converted = list(bingx_fetch.datetime_series_to_ms(series))
    assert converted == [1577836800000, 1577836860000]


def test_interval_to_ms_supports_known_values_and_rejects_unknown():
    assert bingx_fetch.interval_to_ms("1h") == 3_600_000

    with pytest.raises(ValueError, match="Unknown interval: 10m"):
        bingx_fetch.interval_to_ms("10m")


def test_fetch_klines_sends_expected_request(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 0, "data": [{"time": 1}]}

    def fake_get(url, params, headers, timeout):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(bingx_fetch.requests, "get", fake_get)

    data = bingx_fetch.fetch_klines("BTC-USDT", "1m", start_ms=1000, end_ms=2000, limit=500)

    assert data == [{"time": 1}]
    assert captured == {
        "url": bingx_fetch.API_URL,
        "params": {
            "symbol": "BTC-USDT",
            "interval": "1m",
            "limit": 500,
            "startTime": 1000,
            "endTime": 2000,
        },
        "headers": bingx_fetch.HEADERS,
        "timeout": 10,
    }


def test_fetch_klines_raises_on_api_error(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 1001, "msg": "boom", "data": []}

    monkeypatch.setattr(
        bingx_fetch.requests,
        "get",
        lambda url, params, headers, timeout: FakeResponse(),
    )

    with pytest.raises(ValueError, match="API error: boom"):
        bingx_fetch.fetch_klines("BTC-USDT", "1m")


def test_write_log_appends_when_enabled(monkeypatch, tmp_path):
    log_path = tmp_path / "run.log"
    frozen_now = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_now if tz else frozen_now.replace(tzinfo=None)

    monkeypatch.setattr(bingx_fetch, "datetime", FrozenDateTime)
    monkeypatch.setattr(bingx_fetch, "write_log_enabled", True)
    monkeypatch.setattr(bingx_fetch, "logfilepath", str(log_path))

    bingx_fetch.write_log("hello")

    assert log_path.read_text(encoding="utf-8") == "[2024-01-02 03:04:05] hello\n"


def test_write_log_to_appends_when_enabled(monkeypatch, tmp_path):
    log_path = tmp_path / "target.log"
    frozen_now = datetime(2024, 2, 3, 4, 5, 6, tzinfo=timezone.utc)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_now if tz else frozen_now.replace(tzinfo=None)

    monkeypatch.setattr(bingx_fetch, "datetime", FrozenDateTime)
    monkeypatch.setattr(bingx_fetch, "write_log_enabled", True)

    bingx_fetch.write_log_to("targeted", str(log_path))

    assert log_path.read_text(encoding="utf-8") == "[2024-02-03 04:05:06] targeted\n"


def test_save_klines_progress_sorts_and_deduplicates(monkeypatch, tmp_path):
    csv_path = tmp_path / "out.csv"
    logged = []

    monkeypatch.setattr(bingx_fetch, "write_log_to", lambda msg, path: logged.append((msg, path)))

    bingx_fetch.save_klines_progress(
        [
            {"open": 1, "close": 1, "high": 1, "low": 1, "volume": 10, "time": 120000},
            {"open": 2, "close": 2, "high": 2, "low": 2, "volume": 20, "time": 60000},
            {"open": 3, "close": 3, "high": 3, "low": 3, "volume": 30, "time": 120000},
        ],
        str(csv_path),
        str(tmp_path / "out.log"),
    )

    out = pd.read_csv(csv_path)
    assert list(out["time"]) == ["1970-01-01 00:01:00", "1970-01-01 00:02:00"]
    assert len(out) == 2
    assert logged[0][0].startswith("Сохранено 2 свечей")


def test_save_progress_uses_global_paths(monkeypatch):
    called = {}
    monkeypatch.setattr(bingx_fetch, "all_klines", [{"time": 1}])
    monkeypatch.setattr(bingx_fetch, "filepath", "/tmp/file.csv")
    monkeypatch.setattr(bingx_fetch, "logfilepath", "/tmp/file.log")

    def fake_save(klines, target_filepath, target_logfilepath):
        called["args"] = (klines, target_filepath, target_logfilepath)

    monkeypatch.setattr(bingx_fetch, "save_klines_progress", fake_save)

    bingx_fetch.save_progress()

    assert called["args"] == ([{"time": 1}], "/tmp/file.csv", "/tmp/file.log")


def test_signal_handler_saves_progress_and_exits(monkeypatch):
    state = {"saved": False, "logged": None, "code": None}

    monkeypatch.setattr(bingx_fetch, "interrupted", False)
    monkeypatch.setattr(bingx_fetch, "save_progress", lambda: state.__setitem__("saved", True))
    monkeypatch.setattr(bingx_fetch, "write_log", lambda msg: state.__setitem__("logged", msg))

    def fake_exit(code):
        state["code"] = code
        raise SystemExit(code)

    monkeypatch.setattr(builtins, "exit", fake_exit)

    with pytest.raises(SystemExit) as exc:
        bingx_fetch.signal_handler(None, None)

    assert exc.value.code == 0
    assert bingx_fetch.interrupted is True
    assert state == {
        "saved": True,
        "logged": "Программа остановлена пользователем.",
        "code": 0,
    }


def test_valid_date_parses_and_rejects_invalid():
    assert bingx_fetch.valid_date("2025-08-01") == datetime(2025, 8, 1)

    with pytest.raises(argparse.ArgumentTypeError, match="Неверный формат даты"):
        bingx_fetch.valid_date("08/01/2025")


def test_parse_download_filename_handles_valid_and_invalid_names():
    assert bingx_fetch.parse_download_filename("BTC-USDT_1m_2020-01-01_2020-01-02.csv") == {
        "symbol": "BTC-USDT",
        "interval": "1m",
        "start": "2020-01-01",
        "end": "2020-01-02",
    }
    assert bingx_fetch.parse_download_filename("broken.csv") is None


def test_fetch_and_save_downloads_new_file(monkeypatch, tmp_path):
    _prepare_module_state(monkeypatch, tmp_path)
    from_dt = datetime(2020, 1, 1, 0, 0, 0)
    to_dt = datetime(2020, 1, 1, 0, 2, 0)
    calls = []
    sleeps = []

    def fake_fetch(symbol, interval, start_ms, end_ms=None, limit=1400):
        calls.append((symbol, interval, start_ms))
        return [
            {
                "open": 1,
                "close": 1,
                "high": 1,
                "low": 1,
                "volume": 10,
                "time": start_ms,
            }
        ]

    monkeypatch.setattr(bingx_fetch, "fetch_klines", fake_fetch)
    monkeypatch.setattr(bingx_fetch.time, "sleep", lambda seconds: sleeps.append(seconds))

    bingx_fetch.fetch_and_save("BTC-USDT", "1m", from_dt, to_dt)

    csv_path = tmp_path / "downloads" / "BTC-USDT_1m_2020-01-01_2020-01-01.csv"
    out = pd.read_csv(csv_path)
    assert calls == [("BTC-USDT", "1m", 1577836800000)]
    assert sleeps == []
    assert len(out) == 1
    assert pd.to_datetime(out["time"].iloc[0]) == pd.Timestamp("2020-01-01 00:00:00")


def test_fetch_and_save_resumes_from_existing_csv(monkeypatch, tmp_path):
    downloads = _prepare_module_state(monkeypatch, tmp_path)
    csv_path = downloads / "BTC-USDT_1m_2020-01-01_2020-01-01.csv"
    _write_csv(csv_path, [[1, 1, 1, 1, 10, "2020-01-01 00:00:00"]])

    from_dt = datetime(2020, 1, 1, 0, 0, 0)
    to_dt = datetime(2020, 1, 1, 0, 3, 0)
    calls = []

    def fake_fetch(symbol, interval, start_ms, end_ms=None, limit=1400):
        calls.append(start_ms)
        return [
            {
                "open": 2,
                "close": 2,
                "high": 2,
                "low": 2,
                "volume": 20,
                "time": start_ms,
            }
        ]

    monkeypatch.setattr(bingx_fetch, "fetch_klines", fake_fetch)
    monkeypatch.setattr(bingx_fetch.time, "sleep", lambda seconds: None)

    bingx_fetch.fetch_and_save("BTC-USDT", "1m", from_dt, to_dt)

    assert calls == [1577836860000]
    out = pd.read_csv(csv_path)
    assert len(out) == 2
    assert list(out["time"]) == ["2020-01-01 00:00:00", "2020-01-01 00:01:00"]


def test_fetch_and_save_retries_after_network_error(monkeypatch, tmp_path):
    _prepare_module_state(monkeypatch, tmp_path)
    from_dt = datetime(2020, 1, 1, 0, 0, 0)
    to_dt = datetime(2020, 1, 1, 0, 2, 0)
    calls = {"count": 0}
    sleeps = []

    def fake_fetch(symbol, interval, start_ms, end_ms=None, limit=1400):
        calls["count"] += 1
        if calls["count"] == 1:
            raise requests.exceptions.Timeout("timeout")
        return [
            {
                "open": 1,
                "close": 1,
                "high": 1,
                "low": 1,
                "volume": 10,
                "time": start_ms,
            }
        ]

    import requests

    monkeypatch.setattr(bingx_fetch, "fetch_klines", fake_fetch)
    monkeypatch.setattr(bingx_fetch.time, "sleep", lambda seconds: sleeps.append(seconds))

    bingx_fetch.fetch_and_save("BTC-USDT", "1m", from_dt, to_dt)

    assert calls["count"] == 2
    assert sleeps == [10]


def test_fetch_to_file_with_resume_stops_on_non_advancing_api_time(monkeypatch, tmp_path):
    _prepare_module_state(monkeypatch, tmp_path)
    part_path = tmp_path / "downloads" / "BTC-USDT_1m_2020-01-01_update.part.csv"
    from_dt = datetime(2020, 1, 1, 0, 0, 0)
    to_dt = datetime(2020, 1, 1, 0, 3, 0)
    logged = []

    monkeypatch.setattr(
        bingx_fetch,
        "fetch_klines",
        lambda symbol, interval, start_ms, end_ms=None, limit=1400: [
            {
                "open": 1,
                "close": 1,
                "high": 1,
                "low": 1,
                "volume": 10,
                "time": start_ms - 60000,
            }
        ],
    )
    monkeypatch.setattr(bingx_fetch, "write_log_to", lambda msg, path: logged.append((msg, path)))
    monkeypatch.setattr(bingx_fetch.time, "sleep", lambda seconds: None)

    completed = bingx_fetch.fetch_to_file_with_resume(
        symbol="BTC-USDT",
        interval="1m",
        fromdate=from_dt,
        todate=to_dt,
        target_path=str(part_path),
    )

    assert completed is True
    assert any("Некорректный шаг времени" in msg for msg, _ in logged)
    out = pd.read_csv(part_path)
    assert len(out) == 1


def test_build_update_task_returns_expected_payload(monkeypatch, tmp_path):
    downloads = _prepare_module_state(monkeypatch, tmp_path)
    csv_path = downloads / "BTC-USDT_1m_2020-01-01_2020-01-02.csv"
    _write_csv(csv_path, [[1, 1, 1, 1, 10, "2020-01-02 00:00:00"]])

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2020, 1, 3, 12, 0, 0)

    monkeypatch.setattr(bingx_fetch, "datetime", FrozenDateTime)

    task = bingx_fetch._build_update_task(
        str(downloads), "BTC-USDT_1m_2020-01-01_2020-01-02.csv", datetime(2020, 1, 3).date()
    )

    assert task["symbol"] == "BTC-USDT"
    assert task["interval"] == "1m"
    assert task["from_dt"] == datetime(2020, 1, 2, 0, 1, 0)
    assert task["end_dt"] == datetime(2020, 1, 3, 12, 0, 0)


def test_build_update_task_skips_up_to_date_and_invalid_files(monkeypatch, tmp_path):
    downloads = _prepare_module_state(monkeypatch, tmp_path)
    valid_name = "BTC-USDT_1m_2020-01-01_2020-01-03.csv"
    _write_csv(downloads / valid_name, [[1, 1, 1, 1, 10, "2020-01-03 00:00:00"]])
    (downloads / "broken_name.csv").write_text("time\n2020-01-01 00:00:00\n", encoding="utf-8")
    (downloads / "ETH-USDT_1m_2020-01-01_2020-01-02.csv").write_text("open\n1\n", encoding="utf-8")

    today = datetime(2020, 1, 3).date()

    assert bingx_fetch._build_update_task(str(downloads), valid_name, today) is None
    assert bingx_fetch._build_update_task(str(downloads), "broken_name.csv", today) is None
    assert (
        bingx_fetch._build_update_task(
            str(downloads), "ETH-USDT_1m_2020-01-01_2020-01-02.csv", today
        )
        is None
    )


def test_update_existing_file_task_skips_when_part_file_missing(monkeypatch, tmp_path):
    downloads = _prepare_module_state(monkeypatch, tmp_path)
    base_name = "BTC-USDT_1m_2020-01-01_2020-01-01.csv"
    base_path = downloads / base_name
    _write_csv(base_path, [[1, 1, 1, 1, 10, "2020-01-01 00:00:00"]])

    monkeypatch.setattr(bingx_fetch, "fetch_to_file_with_resume", lambda **kwargs: True)

    result = bingx_fetch._update_existing_file_task(
        {
            "filename": base_name,
            "symbol": "BTC-USDT",
            "interval": "1m",
            "file_start": "2020-01-01",
            "csv_path": str(base_path),
            "from_dt": datetime(2020, 1, 1, 0, 1, 0),
            "end_dt": datetime(2020, 1, 2, 0, 0, 0),
            "parsed": {"end": "2020-01-01"},
            "downloads_path": str(downloads),
        }
    )

    assert result == {"status": "skipped", "filename": base_name}
    assert base_path.exists()


def test_update_existing_file_task_merges_part_log_into_existing_log(monkeypatch, tmp_path):
    downloads = _prepare_module_state(monkeypatch, tmp_path)
    monkeypatch.setattr(bingx_fetch, "write_log_enabled", True)

    base_name = "BTC-USDT_1m_2020-01-01_2020-01-01.csv"
    base_path = downloads / base_name
    _write_csv(base_path, [[1, 1, 1, 1, 10, "2020-01-01 00:00:00"]])

    new_log_path = downloads / "BTC-USDT_1m_2020-01-01_2020-01-02.log"
    new_log_path.write_text("existing-log\n", encoding="utf-8")

    def fake_fetch_to_file_with_resume(symbol, interval, fromdate, todate, target_path):
        _write_csv(target_path, [[2, 2, 2, 2, 20, "2020-01-02 00:00:00"]])
        part_log = target_path.replace(".csv", ".log")
        with open(part_log, "w", encoding="utf-8") as fh:
            fh.write("part-log\n")
        return True

    monkeypatch.setattr(bingx_fetch, "fetch_to_file_with_resume", fake_fetch_to_file_with_resume)

    result = bingx_fetch._update_existing_file_task(
        {
            "filename": base_name,
            "symbol": "BTC-USDT",
            "interval": "1m",
            "file_start": "2020-01-01",
            "csv_path": str(base_path),
            "from_dt": datetime(2020, 1, 1, 0, 1, 0),
            "end_dt": datetime(2020, 1, 2, 0, 0, 0),
            "parsed": {"end": "2020-01-01"},
            "downloads_path": str(downloads),
        }
    )

    merged_log = new_log_path.read_text(encoding="utf-8")
    assert result == {"status": "updated", "filename": base_name}
    assert "existing-log" in merged_log
    assert "part-log" in merged_log


def test_update_existing_files_returns_when_no_downloads_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(bingx_fetch, "__file__", str(tmp_path / "bingx_fetch.py"), raising=False)

    assert bingx_fetch.update_existing_files() is None


def test_update_existing_files_handles_worker_exception(monkeypatch, tmp_path):
    downloads = _prepare_module_state(monkeypatch, tmp_path)
    (downloads / "BTC-USDT_1m_2020-01-01_2020-01-01.csv").write_text(
        "time\n2020-01-01 00:00:00\n", encoding="utf-8"
    )
    (downloads / "ETH-USDT_1m_2020-01-01_2020-01-01.csv").write_text(
        "time\n2020-01-01 00:00:00\n", encoding="utf-8"
    )

    monkeypatch.setattr(
        bingx_fetch,
        "_build_update_task",
        lambda downloads_path, filename, today: {"filename": filename},
    )

    class FakeFuture:
        def __init__(self, result=None, error=None):
            self._result = result
            self._error = error

        def result(self):
            if self._error:
                raise self._error
            return self._result

    class FakeExecutor:
        def __init__(self, max_workers):
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def submit(self, fn, task):
            if task["filename"].startswith("BTC"):
                return FakeFuture(error=RuntimeError("worker failed"))
            return FakeFuture(result={"status": "updated", "filename": task["filename"]})

    monkeypatch.setattr(bingx_fetch, "ThreadPoolExecutor", FakeExecutor)
    monkeypatch.setattr(bingx_fetch, "as_completed", lambda futures: futures)
    monkeypatch.setattr(bingx_fetch.os, "cpu_count", lambda: 4)

    assert bingx_fetch.update_existing_files() is None


def test_main_uses_lastdays(monkeypatch):
    called = {}
    frozen_now = datetime(2025, 1, 10, 12, 0, 0)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_now

    monkeypatch.setattr(bingx_fetch, "datetime", FrozenDateTime)
    monkeypatch.setattr(
        bingx_fetch,
        "fetch_and_save",
        lambda symbol, interval, fromdate, todate: called.update(
            {
                "symbol": symbol,
                "interval": interval,
                "fromdate": fromdate,
                "todate": todate,
            }
        ),
    )
    monkeypatch.setattr(sys, "argv", ["bingx-fetch", "--symbol", "BTC-USDT", "--interval", "1h", "--lastdays", "7"])

    bingx_fetch.main()

    assert called == {
        "symbol": "BTC-USDT",
        "interval": "1h",
        "fromdate": frozen_now - timedelta(days=7),
        "todate": frozen_now,
    }


def test_main_uses_default_range(monkeypatch):
    called = {}
    frozen_now = datetime(2025, 1, 10, 12, 0, 0)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_now

    monkeypatch.setattr(bingx_fetch, "datetime", FrozenDateTime)
    monkeypatch.setattr(
        bingx_fetch,
        "fetch_and_save",
        lambda symbol, interval, fromdate, todate: called.update(
            {
                "symbol": symbol,
                "interval": interval,
                "fromdate": fromdate,
                "todate": todate,
            }
        ),
    )
    monkeypatch.setattr(sys, "argv", ["bingx-fetch"])

    bingx_fetch.main()

    assert called["symbol"] == "ETH-USDT"
    assert called["interval"] == "5m"
    assert called["todate"] == frozen_now
    assert called["fromdate"] == frozen_now - timedelta(days=60)


def test_main_calls_update_existing(monkeypatch):
    called = {}
    monkeypatch.setattr(
        bingx_fetch,
        "update_existing_files",
        lambda multithreading: called.update({"multithreading": multithreading}),
    )
    monkeypatch.setattr(sys, "argv", ["bingx-fetch", "--update-existing", "--no-multithreading"])

    bingx_fetch.main()

    assert called == {"multithreading": False}


def test_main_rejects_conflicting_arguments(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["bingx-fetch", "--lastdays", "5", "--fromdate", "2025-01-01"])

    with pytest.raises(ValueError, match="Если указан --lastdays"):
        bingx_fetch.main()


def test_main_rejects_conflicting_update_existing_arguments(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["bingx-fetch", "--update-existing", "--lastdays", "5"])

    with pytest.raises(ValueError, match="С флагом --update-existing"):
        bingx_fetch.main()


def test_main_rejects_invalid_date_order(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["bingx-fetch", "--fromdate", "2025-01-10", "--todate", "2025-01-01"],
    )

    with pytest.raises(ValueError, match="должна быть меньше даты конца"):
        bingx_fetch.main()
