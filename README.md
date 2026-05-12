# fetchmarketdata

CLI utility for downloading historical candlestick (kline) data from BingX and saving it as CSV files.

## Features

- Downloads historical candles for a selected trading pair, interval, and market (`swap` or `spot`) through the BingX API.
- Supports flexible date ranges:
  - recent period via `--lastdays`
  - explicit range via `--fromdate` and `--todate`
  - default mode: last 60 days for `ETH-USDT` on `5m`
- Resumes downloads into an existing CSV file instead of starting from scratch.
- Saves output into market-specific directories under `downloads/` next to the script.
- Optionally writes execution logs to a `.log` file with `--write-log`.
- Handles interruption (`Ctrl+C`) and saves progress before exit.
- Can update all existing CSV files in `downloads/swap/` or `downloads/spot/` up to the current time with `--update-existing`.
- Uses multithreading by default when updating multiple existing files.
- Stores partial update progress in `*.part.csv` files so interrupted update jobs can continue later.
- Merges newly downloaded data with existing files, sorts by timestamp, and removes duplicate candles.

## Requirements

- Python 3.10+
- Network access to `https://open-api.bingx.com`

## Installation

Choose the setup that matches your workflow.

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

The development setup includes test and build dependencies such as `pytest`, `setuptools`, and `wheel`.

Optional `.env` file:

```env
API_KEY=your_api_key
SECRET_KEY=your_secret_key
```

## Usage

After installation, you can run the tool either through the installed CLI entrypoint or directly:

```bash
bingx-fetch
```

```bash
python bingx_fetch.py
```

## CLI Options

- `--symbol` trading pair, for example `BTC-USDT`
- `--interval` candle interval
- `--market` market type: `swap` or `spot`
  For regular downloads the default is `swap`; with `--update-existing` and no `--market`, both markets are updated
- `--lastdays` download the last N days
- `--fromdate` start date in `YYYY-MM-DD`
- `--todate` end date in `YYYY-MM-DD`
- `--update-existing` update all existing CSV files in `downloads/`
- `--write-log` write logs to a `.log` file
- `--no-multithreading` disable multithreaded updates for `--update-existing`

Supported intervals in the current implementation:

- `1m`, `3m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `1d`, `3d`

## Examples

```bash
# Default: ETH-USDT, 5m, last 60 days
bingx-fetch

# Custom symbol and interval
bingx-fetch --symbol BTC-USDT --interval 1h

# Download spot market data
bingx-fetch --market spot --symbol BTC-USDT --interval 1h

# Download the last 90 days
bingx-fetch --symbol ETH-USDT --interval 5m --lastdays 90

# Download a fixed date range
bingx-fetch --symbol ETH-USDT --interval 5m --fromdate 2025-07-01 --todate 2025-08-01

# Write a log file alongside the CSV
bingx-fetch --symbol BTC-USDT --interval 1m --lastdays 7 --write-log

# Update all previously downloaded CSV files for both markets
bingx-fetch --update-existing --write-log

# Update all previously downloaded spot CSV files
bingx-fetch --market spot --update-existing --write-log

# Update existing files sequentially
bingx-fetch --update-existing --no-multithreading
```

## Output

Regular downloads produce:

- `downloads/<market>/<symbol>_<interval>_<from>_<to>_<market>.csv`
- `downloads/<market>/<symbol>_<interval>_<from>_<to>_<market>.log` when `--write-log` is enabled

Update mode may also create temporary files during incremental refresh:

- `downloads/<market>/<symbol>_<interval>_<old_end>_update_<market>.part.csv`
- `downloads/<market>/<symbol>_<interval>_<old_end>_update_<market>.part.log`

If an update completes successfully, the tool merges the old and new data into a refreshed CSV whose end date matches the latest candle, then removes the temporary part files.

## Notes

- `--lastdays` cannot be combined with `--fromdate` or `--todate`.
- `--update-existing` cannot be combined with `--lastdays`, `--fromdate`, or `--todate`.
- Existing and merged files are normalized by candle time, sorted in ascending order, and deduplicated on the `time` column.
