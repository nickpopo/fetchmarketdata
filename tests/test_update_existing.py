from datetime import datetime

import pandas as pd

import bingx_fetch


def _write_csv(path, rows):
    df = pd.DataFrame(rows, columns=["open", "close", "high", "low", "volume", "time"])
    df.to_csv(path, index=False)


def _prepare_downloads(monkeypatch, tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bingx_fetch, "__file__", str(tmp_path / "bingx_fetch.py"), raising=False)
    monkeypatch.setattr(bingx_fetch, "interrupted", False)
    monkeypatch.setattr(bingx_fetch, "all_klines", [])
    monkeypatch.setattr(bingx_fetch, "filepath", None)
    monkeypatch.setattr(bingx_fetch, "logfilepath", None)
    return downloads


def test_update_existing_merges_into_new_filename(monkeypatch, tmp_path):
    downloads = _prepare_downloads(monkeypatch, tmp_path)

    old_name = "BTC-USDT_1m_2020-01-01_2020-01-01.csv"
    old_path = downloads / old_name
    _write_csv(
        old_path,
        [
            [1, 1, 1, 1, 10, "2020-01-01 00:00:00"],
            [1, 1, 1, 1, 10, "2020-01-01 00:01:00"],
        ],
    )

    def fake_fetch_to_file_with_resume(symbol, interval, fromdate, todate, target_path):
        assert symbol == "BTC-USDT"
        assert interval == "1m"
        _write_csv(
            target_path,
            [
                [2, 2, 2, 2, 20, "2020-01-01 00:01:00"],  # duplicate for dedup check
                [2, 2, 2, 2, 20, "2020-01-02 00:00:00"],
            ],
        )
        return True

    monkeypatch.setattr(bingx_fetch, "fetch_to_file_with_resume", fake_fetch_to_file_with_resume)

    bingx_fetch.update_existing_files()

    new_path = downloads / "BTC-USDT_1m_2020-01-01_2020-01-02.csv"
    assert new_path.exists()
    assert not old_path.exists()

    merged = pd.read_csv(new_path)
    assert len(merged) == 3
    assert merged["time"].iloc[-1] == "2020-01-02 00:00:00"

    # part file should be deleted after successful merge
    assert not (downloads / "BTC-USDT_1m_2020-01-01_update.part.csv").exists()


def test_update_existing_keeps_part_on_interruption(monkeypatch, tmp_path):
    downloads = _prepare_downloads(monkeypatch, tmp_path)

    old_name = "ETH-USDT_1m_2020-01-01_2020-01-01.csv"
    old_path = downloads / old_name
    _write_csv(old_path, [[1, 1, 1, 1, 10, "2020-01-01 00:00:00"]])

    def fake_fetch_to_file_with_resume(symbol, interval, fromdate, todate, target_path):
        _write_csv(target_path, [[2, 2, 2, 2, 20, "2020-01-01 00:01:00"]])
        return False

    monkeypatch.setattr(bingx_fetch, "fetch_to_file_with_resume", fake_fetch_to_file_with_resume)

    bingx_fetch.update_existing_files()

    # Original file must remain unchanged when update was interrupted.
    assert old_path.exists()
    assert not (downloads / "ETH-USDT_1m_2020-01-01_2020-01-02.csv").exists()
    assert (downloads / "ETH-USDT_1m_2020-01-01_update.part.csv").exists()


def test_fetch_to_file_with_resume_continues_from_existing_part(monkeypatch, tmp_path):
    downloads = _prepare_downloads(monkeypatch, tmp_path)

    part_path = downloads / "XRP-USDT_1m_2020-01-01_update.part.csv"
    _write_csv(part_path, [[1, 1, 1, 1, 10, "2020-01-01 00:00:00"]])

    calls = []

    def fake_fetch_klines(symbol, interval, start_ms, end_ms=None, limit=1400):
        calls.append(start_ms)
        # One candle is enough to finish because len(chunk) < 1000.
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

    monkeypatch.setattr(bingx_fetch, "fetch_klines", fake_fetch_klines)

    from_dt = datetime(2020, 1, 1, 0, 0, 0)
    to_dt = datetime(2020, 1, 1, 0, 3, 0)

    completed = bingx_fetch.fetch_to_file_with_resume(
        symbol="XRP-USDT",
        interval="1m",
        fromdate=from_dt,
        todate=to_dt,
        target_path=str(part_path),
    )

    assert completed is True

    # Resume must start from the next candle after existing part row.
    expected_start = bingx_fetch.datetime_to_ms(datetime(2020, 1, 1, 0, 1, 0))
    assert calls[0] == expected_start

    out = pd.read_csv(part_path)
    assert len(out) == 2
    assert out["time"].iloc[0] == "2020-01-01 00:00:00"


def test_update_existing_uses_multithreading_by_default(monkeypatch, tmp_path):
    downloads = _prepare_downloads(monkeypatch, tmp_path)
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

    processed = []

    def fake_update_task(task):
        processed.append(task["filename"])
        return {"status": "updated", "filename": task["filename"]}

    monkeypatch.setattr(bingx_fetch, "_update_existing_file_task", fake_update_task)
    monkeypatch.setattr(bingx_fetch.os, "cpu_count", lambda: 8)

    class FakeFuture:
        def __init__(self, result):
            self._result = result

        def result(self):
            return self._result

    captured = {"workers": None}

    class FakeExecutor:
        def __init__(self, max_workers):
            captured["workers"] = max_workers

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def submit(self, fn, task):
            return FakeFuture(fn(task))

    monkeypatch.setattr(bingx_fetch, "ThreadPoolExecutor", FakeExecutor)
    monkeypatch.setattr(bingx_fetch, "as_completed", lambda futures: futures)

    bingx_fetch.update_existing_files()

    assert captured["workers"] == 2
    assert sorted(processed) == sorted(
        [
            "BTC-USDT_1m_2020-01-01_2020-01-01.csv",
            "ETH-USDT_1m_2020-01-01_2020-01-01.csv",
        ]
    )


def test_update_existing_can_run_sequentially(monkeypatch, tmp_path):
    downloads = _prepare_downloads(monkeypatch, tmp_path)
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

    processed = []

    def fake_update_task(task):
        processed.append(task["filename"])
        return {"status": "updated", "filename": task["filename"]}

    monkeypatch.setattr(bingx_fetch, "_update_existing_file_task", fake_update_task)

    class MustNotBeCalled:
        def __init__(self, *args, **kwargs):
            raise AssertionError("ThreadPoolExecutor should not be used in sequential mode")

    monkeypatch.setattr(bingx_fetch, "ThreadPoolExecutor", MustNotBeCalled)

    bingx_fetch.update_existing_files(multithreading=False)

    assert processed == [
        "BTC-USDT_1m_2020-01-01_2020-01-01.csv",
        "ETH-USDT_1m_2020-01-01_2020-01-01.csv",
    ]


def test_write_log_to_skips_file_when_logging_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(bingx_fetch, "write_log_enabled", False)
    log_path = tmp_path / "disabled.log"

    bingx_fetch.write_log_to("test message", str(log_path))

    assert not log_path.exists()
