import sys
from datetime import datetime, timezone

import pandas as pd
import pytest

import bingx_fetch


def _prepare_module_state(monkeypatch, tmp_path):
    for source in ("bingx", "binance"):
        for market in ("swap", "spot"):
            (tmp_path / "downloads" / source / market).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(bingx_fetch, "__file__", str(tmp_path / "bingx_fetch.py"), raising=False)
    monkeypatch.setattr(bingx_fetch, "all_klines", [])
    monkeypatch.setattr(bingx_fetch, "filepath", None)
    monkeypatch.setattr(bingx_fetch, "logfilepath", None)
    monkeypatch.setattr(bingx_fetch, "interrupted", False)
    monkeypatch.setattr(bingx_fetch, "write_log_enabled", False)


def test_get_filepath_creates_source_and_market_downloads_directory(monkeypatch, tmp_path):
    _prepare_module_state(monkeypatch, tmp_path)

    path = bingx_fetch.get_filepath("sample.csv", "swap", "bingx")

    assert path == str(tmp_path / "downloads" / "bingx" / "swap" / "sample.csv")
    assert (tmp_path / "downloads" / "bingx" / "swap").is_dir()


def test_build_data_filename_includes_source():
    filename = bingx_fetch.build_data_filename(
        symbol="BTC-USDT",
        interval="1m",
        fromdate=datetime(2020, 1, 1),
        todate=datetime(2020, 1, 2),
        market="swap",
        source="binance",
    )

    assert filename == "BTC-USDT_1m_2020-01-01_2020-01-02_binance_swap.csv"


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


def test_get_request_end_ms_respects_limit_and_source_specific_window():
    start_ms = bingx_fetch.datetime_to_ms(datetime(2025, 1, 1, 0, 0, 0))
    final_end_ms = bingx_fetch.datetime_to_ms(datetime(2025, 2, 1, 0, 0, 0))

    bingx_swap_end_ms = bingx_fetch.get_request_end_ms(
        "bingx", "swap", "1m", start_ms, final_end_ms, 1400
    )
    bingx_spot_end_ms = bingx_fetch.get_request_end_ms(
        "bingx", "spot", "1m", start_ms, final_end_ms, 20_000
    )
    binance_spot_end_ms = bingx_fetch.get_request_end_ms(
        "binance", "spot", "1m", start_ms, final_end_ms, 1000
    )

    assert bingx_swap_end_ms == start_ms + (1400 * 60_000) - 60_000
    assert bingx_spot_end_ms == start_ms + bingx_fetch.SPOT_MAX_QUERY_RANGE_MS - 60_000
    assert binance_spot_end_ms == start_ms + (1000 * 60_000) - 60_000


def test_fetch_klines_with_adaptive_range_retries_bingx_spot_with_smaller_window(monkeypatch):
    start_ms = bingx_fetch.datetime_to_ms(datetime(2025, 1, 1, 0, 0, 0))
    final_end_ms = bingx_fetch.datetime_to_ms(datetime(2025, 1, 10, 0, 0, 0))
    seen_end_times = []

    def fake_fetch_klines(source, market, symbol, interval, passed_start_ms, end_ms=None, limit=None):
        seen_end_times.append(end_ms)
        if len(seen_end_times) == 1:
            raise ValueError("API error: The maximum query range for FARTCOIN_USDT K-lines is 7 days and 0 hours.")
        return [{"time": passed_start_ms}]

    monkeypatch.setattr(bingx_fetch, "fetch_klines", fake_fetch_klines)

    chunk, used_end_ms = bingx_fetch.fetch_klines_with_adaptive_range(
        source="bingx",
        market="spot",
        symbol="FARTCOIN-USDT",
        interval="1d",
        start_ms=start_ms,
        final_end_ms=final_end_ms,
        limit=1400,
    )

    assert chunk == [{"time": start_ms}]
    assert len(seen_end_times) == 2
    assert used_end_ms == seen_end_times[-1]
    assert seen_end_times[-1] < seen_end_times[0]


def test_fetch_klines_sends_expected_bingx_request(monkeypatch):
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

    data = bingx_fetch.fetch_klines(
        "bingx", "swap", "BTC-USDT", "1m", start_ms=1000, end_ms=2000, limit=500
    )

    assert data == [{"time": 1}]
    assert captured == {
        "url": bingx_fetch.API_URLS["swap"],
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


def test_fetch_klines_sends_expected_binance_request(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [[1702720560000, "42220.61", "42221.1", "42215.56", "42216.63", "2.93"]]

    def fake_get(url, params, headers, timeout):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(bingx_fetch.requests, "get", fake_get)

    data = bingx_fetch.fetch_klines(
        "binance", "spot", "BTC-USDT", "1m", start_ms=1000, end_ms=2000, limit=500
    )

    assert data == [
        {
            "time": 1702720560000,
            "open": "42220.61",
            "high": "42221.1",
            "low": "42215.56",
            "close": "42216.63",
            "volume": "2.93",
            "closeTime": None,
            "quoteVolume": None,
            "trades": None,
            "takerBaseVolume": None,
            "takerQuoteVolume": None,
        }
    ]
    assert captured == {
        "url": "https://api.binance.com/api/v3/klines",
        "params": {
            "symbol": "BTCUSDT",
            "interval": "1m",
            "limit": 500,
            "startTime": 1000,
            "endTime": 2000,
        },
        "headers": {},
        "timeout": 10,
    }


def test_fetch_klines_normalizes_bingx_spot_array_response(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "code": 0,
                "data": [
                    [1702720620000, 42216.29, 42216.94, 42216.29, 42216.72, 0.2, 1702720679999, 8548.46],
                    [1702720560000, 42220.61, 42221.1, 42215.56, 42216.63, 2.93, 1702720619999, 123968.7],
                ],
            }

    monkeypatch.setattr(
        bingx_fetch.requests,
        "get",
        lambda url, params, headers, timeout: FakeResponse(),
    )

    data = bingx_fetch.fetch_klines("bingx", "spot", "BTC-USDT", "1m", start_ms=1000, end_ms=2000, limit=500)

    assert data == [
        {
            "time": 1702720560000,
            "open": 42220.61,
            "high": 42221.1,
            "low": 42215.56,
            "close": 42216.63,
            "volume": 2.93,
            "closeTime": 1702720619999,
            "quoteVolume": 123968.7,
        },
        {
            "time": 1702720620000,
            "open": 42216.29,
            "high": 42216.94,
            "low": 42216.29,
            "close": 42216.72,
            "volume": 0.2,
            "closeTime": 1702720679999,
            "quoteVolume": 8548.46,
        },
    ]


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
        bingx_fetch.fetch_klines("bingx", "swap", "BTC-USDT", "1m")


def test_get_api_url_supports_known_sources_and_markets():
    assert bingx_fetch.get_api_url("bingx", "swap") == bingx_fetch.API_URLS["swap"]
    assert bingx_fetch.get_api_url("bingx", "spot") == bingx_fetch.API_URLS["spot"]
    assert bingx_fetch.get_api_url("binance", "spot") == "https://api.binance.com/api/v3/klines"

    with pytest.raises(ValueError, match="Unknown source: kraken"):
        bingx_fetch.get_source_config("kraken")

    with pytest.raises(ValueError, match="Unknown market for source binance: options"):
        bingx_fetch.get_api_url("binance", "options")


def test_parse_download_filename_includes_source():
    parsed = bingx_fetch.parse_download_filename("BTC-USDT_1m_2020-01-01_2020-01-02_binance_spot.csv")

    assert parsed == {
        "symbol": "BTC-USDT",
        "interval": "1m",
        "start": "2020-01-01",
        "end": "2020-01-02",
        "source": "binance",
        "market": "spot",
    }


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


def test_main_passes_selected_source_to_fetch(monkeypatch):
    called = {}

    monkeypatch.setattr(
        sys,
        "argv",
        ["bingx_fetch.py", "--source", "binance", "--market", "spot", "--symbol", "BTC-USDT"],
    )
    monkeypatch.setattr(
        bingx_fetch,
        "fetch_and_save",
        lambda **kwargs: called.update(kwargs),
    )

    bingx_fetch.main()

    assert called["source"] == "binance"
    assert called["market"] == "spot"
    assert called["symbol"] == "BTC-USDT"
