"""Daily Stockton archive using the same legacy endpoint as the MATLAB script."""
import csv
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = ("https://hohonu-web.herokuapp.com/api/stations/"
            "22ae1f70-1a4c-4049-90dc-e688d6b25cf0/statistic/tide/")
ROOT = Path(__file__).resolve().parent
MASTER = ROOT / "Stockton_water_level.csv"
STATE = ROOT / "Stockton_download_state.json"
FAILED = ROOT / "Stockton_failed_intervals.csv"
START = datetime(2026, 6, 1, tzinfo=timezone.utc)


def parse_time(text):
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError(f"Timestamp has no UTC offset: {text}")
    return value.astimezone(timezone.utc)


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def write_csv(path, columns, rows):
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp, path)


def fetch(session, start, end):
    response = session.get(BASE_URL, params={
        "from": start.strftime("%Y-%m-%d %H:%M"),
        "to": end.strftime("%Y-%m-%d %H:%M"),
        "datum": "NAVD", "units": "english", "cleaned": "true",
    }, timeout=(15, 120))
    response.raise_for_status()
    data = response.json().get("data")
    if not isinstance(data, list) or len(data) < 2:
        raise ValueError("No time/value arrays in response")
    times, values = data[:2]
    if not isinstance(times, list) or not isinstance(values, list) or len(times) != len(values):
        raise ValueError("Invalid or mismatched time/value arrays")
    rows = {}
    for raw_time, raw_value in zip(times, values):
        timestamp = parse_time(raw_time)
        if not start <= timestamp <= end or raw_value is None:
            continue
        if isinstance(raw_value, bool):
            raise ValueError("Boolean water level returned")
        value = float(raw_value)
        if math.isfinite(value):
            rows[timestamp] = value
    if not rows:
        raise ValueError("No valid measurements within requested interval")
    return rows


def main():
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    old_state = json.loads(STATE.read_text()) if STATE.exists() and MASTER.exists() else {}
    rows = {}
    if MASTER.exists():
        with MASTER.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != ["Time_UTC", "WaterLevel"]:
                raise ValueError("Expected existing CSV columns: Time_UTC,WaterLevel")
            for row in reader:
                value = float(row["WaterLevel"]) if row["WaterLevel"] else float("nan")
                if math.isfinite(value):
                    rows[parse_time(row["Time_UTC"])] = value

    # Cursor tracks attempted ranges, never the maximum observation timestamp.
    # Failed requests remain queued even if later requests succeed.
    cursor = max(START, parse_time(old_state["attempted_until"]) - timedelta(days=2)) if old_state else START
    ranges = [(parse_time(a), parse_time(b)) for a, b in old_state.get("pending", [])]
    while cursor < now:
        end = min(cursor + timedelta(days=7), now)
        ranges.append((cursor, end))
        cursor = end
    ranges = sorted(set(ranges))
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=Retry(
        total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])))
    failed = []
    for start, end in ranges:
        print(f"Requesting {iso(start)} to {iso(end)}", flush=True)
        try:
            new = fetch(session, start, end)
            rows.update(new)
            print(f"  Received {len(new)} valid points", flush=True)
        except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
            failed.append({"From_UTC": iso(start), "To_UTC": iso(end), "Error": str(exc)})
            print(f"  FAILED: {exc}", flush=True)

    if rows:
        write_csv(MASTER, ["Time_UTC", "WaterLevel"],
                  ({"Time_UTC": iso(t), "WaterLevel": rows[t]} for t in sorted(rows)))
        print(f"Master: {len(rows)} points; {iso(min(rows))} to {iso(max(rows))}")
    write_csv(FAILED, ["From_UTC", "To_UTC", "Error"], failed)
    state = {"attempted_until": iso(now),
             "pending": [[r["From_UTC"], r["To_UTC"]] for r in failed],
             "station_id": "22ae1f70-1a4c-4049-90dc-e688d6b25cf0",
             "datum_requested": "NAVD", "units_requested": "english", "cleaned": True}
    temp = STATE.with_suffix(".json.tmp")
    temp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, STATE)
    if failed or not rows:
        raise SystemExit("Download incomplete. Saved successful data; failed intervals will be retried.")


if __name__ == "__main__":
    main()
