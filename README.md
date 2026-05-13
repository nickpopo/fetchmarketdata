# fetchmarketdata

CLI utility for downloading historical candlestick (kline) data and saving it as CSV files.

## Features

- Downloads historical candles from multiple sources through a shared abstraction layer.
- Supports `bingx` and `binance` today, with source-specific logic isolated behind a common interface.
- Supports `spot` and `swap` markets where the selected source provides them.
- Supports flexible date ranges:
  - recent period via `--lastdays`
  - explicit range via `--fromdate` and `--todate`
  - default mode: last 60 days for `ETH-USDT` on `5m`
- Resumes downloads into an existing CSV file instead of starting from scratch.
- Saves output into source- and market-specific directories under `downloads/`.
- Optionally writes execution logs to a `.log` file with `--write-log`.
- Handles interruption (`Ctrl+C`) and saves progress before exit.
- Can update all existing CSV files up to the current time with `--update-existing`.
- Uses multithreading by default when updating multiple existing files.
- Stores partial update progress in `*.part.csv` files so interrupted update jobs can continue later.
- Merges newly downloaded data with existing files, sorts by timestamp, and removes duplicate candles.

## Requirements

- Python 3.10+
- Network access to:
  - `https://open-api.bingx.com`
  - `https://api.binance.com`
  - `https://fapi.binance.com`

## Installation

Regular installation:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

Development installation:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e '.[dev]'
```

Optional `.env` file for BingX:

```env
API_KEY=your_api_key
SECRET_KEY=your_secret_key
```

## Usage

After installation, you can run the tool through either CLI entrypoint or directly:

```bash
fetchmarketdata
```

```bash
bingx-fetch
```

```bash
python bingx_fetch.py
```

## CLI Options

- `--source` data source: `bingx` or `binance`
- `--symbol` trading pair, for example `BTC-USDT`
- `--interval` candle interval
- `--market` market type: `spot` or `swap`
  If omitted, the source default is used:
  - `bingx`: `swap`
  - `binance`: `spot`
- `--lastdays` download the last N days
- `--fromdate` start date in `YYYY-MM-DD`
- `--todate` end date in `YYYY-MM-DD`
- `--update-existing` update all existing CSV files in `downloads/`
- `--write-log` write logs to a `.log` file
- `--no-multithreading` disable multithreaded updates for `--update-existing`

When `--update-existing` is used:

- with `--source`, only that source is updated
- with `--source` and `--market`, only that source/market is updated
- without `--source`, all configured sources and their markets are updated

For Binance, symbols such as `BTC-USDT` are automatically converted to `BTCUSDT` for API requests.

Supported intervals in the current implementation:

- `1m`, `3m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `1d`, `3d`

## Examples

```bash
# Default: BingX, ETH-USDT, 5m, last 60 days
fetchmarketdata

# BingX swap data
fetchmarketdata --source bingx --market swap --symbol BTC-USDT --interval 1h

# Binance spot data
fetchmarketdata --source binance --market spot --symbol BTC-USDT --interval 1h

# Binance futures/swap data
fetchmarketdata --source binance --market swap --symbol BTC-USDT --interval 15m

# Download the last 90 days
fetchmarketdata --source binance --symbol ETH-USDT --interval 5m --lastdays 90

# Download a fixed date range
fetchmarketdata --source bingx --symbol ETH-USDT --interval 5m --fromdate 2025-07-01 --todate 2025-08-01

# Write a log file alongside the CSV
fetchmarketdata --source binance --symbol BTC-USDT --interval 1m --lastdays 7 --write-log

# Update all previously downloaded CSV files for all configured sources and markets
fetchmarketdata --update-existing --write-log

# Update only Binance spot CSV files
fetchmarketdata --source binance --market spot --update-existing --write-log

# Update existing files sequentially
fetchmarketdata --update-existing --no-multithreading
```

## Output

Regular downloads produce:

- `downloads/<source>/<market>/<symbol>_<interval>_<from>_<to>_<source>_<market>.csv`
- `downloads/<source>/<market>/<symbol>_<interval>_<from>_<to>_<source>_<market>.log` when `--write-log` is enabled

Update mode may also create temporary files during incremental refresh:

- `downloads/<source>/<market>/<symbol>_<interval>_<old_end>_update_<source>_<market>.part.csv`
- `downloads/<source>/<market>/<symbol>_<interval>_<old_end>_update_<source>_<market>.part.log`

If an update completes successfully, the tool merges the old and new data into a refreshed CSV whose end date matches the latest candle, then removes the temporary part files.

## Notes

- `--lastdays` cannot be combined with `--fromdate` or `--todate`.
- `--update-existing` cannot be combined with `--lastdays`, `--fromdate`, or `--todate`.
- Existing and merged files are normalized by candle time, sorted in ascending order, and deduplicated on the `time` column.
