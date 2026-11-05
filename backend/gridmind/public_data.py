"""Traceable public observations and bounded CSV imports; never fabricate readings."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

ZENODO_URL = "https://zenodo.org/records/14983362/files/corrected_daily_energy_met_MU.csv?download=1"
ZENODO_RECORD = "https://zenodo.org/records/14983362"
ZENODO_MD5 = "172540c2c13282cc3dd4c9679b537aa0"
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024


def _download(url: str, destination: Path) -> bytes:
    error = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "GRIDMIND-academic-prototype/1.0"})
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read(MAX_DOWNLOAD_BYTES + 1)
            if len(payload) > MAX_DOWNLOAD_BYTES:
                raise ValueError("Public download exceeds the 10 MiB limit")
            destination.write_bytes(payload)
            return payload
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            error = exc
            if attempt < 2:
                time.sleep(0.25 * (attempt + 1))
    raise RuntimeError(f"Public source unavailable: {error}") from error


def _observation(source_id: str, region: str, metric: str, timestamp: str,
                 value: float, unit: str, **extra) -> dict:
    identity = f"{source_id}|{region}|{metric}|{timestamp}"
    if extra.get("series_key"):
        identity += f"|{extra['series_key']}"
    return {
        "id": str(uuid5(NAMESPACE_URL, identity)),
        "source_id": source_id, "region": region, "metric": metric,
        "observed_at": timestamp, "value": value, "unit": unit,
        "quality": "observed", **extra,
    }


def fetch_karnataka(raw_dir: Path) -> tuple[list[dict], dict]:
    """Download/cache the versioned Zenodo CSV and extract actual Karnataka GWh."""
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / "corrected_daily_energy_met_MU.csv"
    payload = raw_path.read_bytes() if raw_path.exists() else _download(ZENODO_URL, raw_path)
    # The immutable record advertises this checksum. A failed verification must
    # not silently replace evidence with invented or differently versioned data.
    if hashlib.md5(payload).hexdigest() != ZENODO_MD5:
        raise ValueError("Zenodo raw CSV checksum differs from record 14983362; inspect the source before importing")
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
    if not reader.fieldnames or not {"Date", "Karnataka"}.issubset(reader.fieldnames):
        raise ValueError("Unexpected Zenodo format: expected Date and Karnataka columns")
    observations, skipped, duplicates = [], 0, 0
    seen, conflicts = {}, set()
    for row in reader:
        try:
            date = datetime.strptime(row["Date"], "%Y-%m-%d")
            value = float(row["Karnataka"])
            if not math.isfinite(value) or value < 0:
                raise ValueError("Invalid source reading")
        except (ValueError, TypeError):
            skipped += 1
            continue
        if row["Date"] in seen:
            skipped += 1
            if seen[row["Date"]] != value:
                conflicts.add(row["Date"])
            else:
                duplicates += 1
            continue
        seen[row["Date"]] = value
        # Local date anchor denotes the start of a DAILY aggregate, not a
        # timestamped instantaneous meter reading.
        timestamp = date.replace(tzinfo=ZoneInfo("Asia/Kolkata")).isoformat()
        observations.append(_observation(
            "zenodo-14983362-karnataka", "Karnataka", "supplied_energy_gwh",
            timestamp, value, "GWh", quality="source_corrected",
            metadata={"original_unit": "MU", "period": "daily", "source_date": row["Date"]},
        ))
    # Conflicting duplicated dates have no defensible preferred measurement.
    # Exclude every version from labels while retaining the full raw evidence.
    observations = [item for item in observations if item["metadata"]["source_date"] not in conflicts]
    skipped += len(conflicts)
    if not observations:
        raise ValueError("The source contains no valid Karnataka observations")
    observations.sort(key=lambda x: x["observed_at"])
    source = {
        "id": "zenodo-14983362-karnataka", "name": "Karnataka daily supplied electricity",
        "provider": "Kieran Hunt and Hannah Bloomfield / Zenodo", "source_type": "historical_public_csv",
        "url": ZENODO_RECORD, "download_url": ZENODO_URL, "license": "CC-BY-4.0",
        "frequency": "daily", "units": "GWh", "region": "Karnataka",
        "metric": "supplied_energy_gwh", "status": "historical",
        "metadata": {
            "raw_path": str(raw_path.resolve()), "sha256": hashlib.sha256(payload).hexdigest(),
            "md5": ZENODO_MD5, "acquired_at": datetime.now(timezone.utc).isoformat(),
            "record_doi": "10.5281/zenodo.14983362", "rows": len(observations), "skipped_rows": skipped,
            "identical_duplicate_rows": duplicates, "conflicting_dates_excluded": sorted(conflicts),
            "first_observation": observations[0]["observed_at"], "last_observation": observations[-1]["observed_at"],
            "interpretation": "Daily supplied electricity (energy met), not unconstrained demand; 1 MU = 1 GWh.",
            "time_semantics": "Asia/Kolkata local daily aggregate anchored at midnight; no intraday measurement inferred.",
        },
    }
    provenance_path = raw_dir / "corrected_daily_energy_met_MU.provenance.json"
    if provenance_path.exists():
        previous = json.loads(provenance_path.read_text(encoding="utf-8"))
        if previous.get("metadata", {}).get("sha256") == source["metadata"]["sha256"]:
            source["metadata"]["acquired_at"] = previous["metadata"].get("acquired_at", source["metadata"]["acquired_at"])
    provenance_path.write_text(json.dumps(source, indent=2), encoding="utf-8")
    return observations, source


def _timestamp(text: str, timezone_name: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Missing timestamp")
    dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        zone = ZoneInfo(timezone_name)
        first, second = dt.replace(tzinfo=zone, fold=0), dt.replace(tzinfo=zone, fold=1)
        if first.utcoffset() != second.utcoffset():
            raise ValueError("Ambiguous or nonexistent local time: provide an explicit UTC offset")
        dt = first
    return dt.astimezone(timezone.utc).isoformat()


def parse_csv(content: str | bytes, *, max_rows: int = 20000,
              source_id: str = "uploaded-csv", region: str = "Custom",
              timezone_name: str = "UTC") -> tuple[list[dict], dict]:
    """Import canonical CSV or a bounded UK Low Carbon London CSV subset.

    Canonical columns: timestamp,value,unit,metric, with optional region and
    series_key. Explicit value_kind labels must be observed.
    LCL columns: LCLid,stdorToU,DateTime,KWH/hh (per half hour).
    Naive London DST transition times are rejected instead of guessing a fold.
    """
    if not 1 <= max_rows <= 20000:
        raise ValueError("max_rows must be between 1 and 20000")
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")
    if len(content.encode("utf-8")) > MAX_DOWNLOAD_BYTES:
        raise ValueError("CSV exceeds the 10 MiB import limit; prepare a bounded subset")
    reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))
    if not reader.fieldnames:
        raise ValueError("CSV has no header")
    headers = {h.strip(): h for h in reader.fieldnames}
    london_value = next((h for h in headers if h.lower().replace(" ", "") in {"kwh/hh(perhalf hour)".replace(" ", ""), "kwh/hh", "kwhperhh"}), None)
    london = {"LCLid", "DateTime"}.issubset(headers) and london_value is not None
    if not london and not {"timestamp", "value", "unit", "metric"}.issubset(headers):
        raise ValueError("Expected timestamp,value,unit,metric or London LCLid,DateTime,KWH/hh columns")
    observations, rejected, seen, duplicates, truncated = [], [], {}, 0, False
    for row_number, raw in enumerate(reader, start=2):
        if row_number > max_rows + 1:
            truncated = True
            break
        row = {key: (raw.get(original) or "") for key, original in headers.items()}
        try:
            if row.get("value_kind", "observed").strip() != "observed":
                raise ValueError("Only observed measurements can be imported; predicted or simulated values are not observations")
            if london:
                household = row["LCLid"].strip()
                if not household:
                    raise ValueError("Missing household identifier")
                timestamp = _timestamp(row["DateTime"], "Europe/London")
                value, unit, metric = float(row[london_value]), "kWh", "household_energy_kwh"
                row_region = f"London/{household}"
                series_key = household
            else:
                timestamp = _timestamp(row["timestamp"], timezone_name)
                value, unit, metric = float(row["value"]), row["unit"].strip(), row["metric"].strip()
                row_region = (row.get("region") or region).strip()
                series_key = (row.get("series_key") or "regional").strip()
            if not math.isfinite(value) or value < 0:
                raise ValueError("Value must be a finite nonnegative number")
            if unit not in {"kWh", "MWh", "GWh", "kW", "MW"}:
                raise ValueError("Unsupported unit: use kWh, MWh, GWh, kW or MW")
            if not metric or len(metric) > 60 or not row_region or len(row_region) > 100:
                raise ValueError("Invalid metric or region")
            if not series_key or len(series_key) > 100:
                raise ValueError("Invalid series_key; use between 1 and 100 characters")
        except (ValueError, TypeError, OverflowError) as exc:
            rejected.append({"row": row_number, "reason": str(exc)})
            continue
        key = (row_region, series_key, metric, timestamp)
        if key in seen:
            if seen[key] != (value, unit):
                # No row order can establish which value is authentic. Abort
                # this import instead of retaining an arbitrary first reading.
                raise ValueError("Conflicting duplicate timestamp, metric, region and series_key; import was not applied")
            duplicates += 1
            continue
        seen[key] = (value, unit)
        observations.append(_observation(source_id, row_region, metric, timestamp, value, unit,
            series_key=series_key,
            metadata={"row": row_number, "tariff_group": row.get("stdorToU"),
                      "period": "half-hour" if london else "source-defined"}))
    # Flag extreme upper-tail readings for review; keep the original value.
    groups: dict[tuple, list[dict]] = {}
    for item in observations:
        groups.setdefault((item["region"], item["series_key"], item["metric"], item["unit"]), []).append(item)
    outliers = 0
    for group in groups.values():
        if len(group) < 20:
            continue
        values = sorted(item["value"] for item in group)
        q1, q3 = values[len(values) // 4], values[3 * len(values) // 4]
        threshold = q3 + 3 * (q3 - q1)
        for item in group:
            if item["value"] > threshold:
                item["quality"] = "outlier_review"
                outliers += 1
    observations.sort(key=lambda item: (item["observed_at"], item["region"]))
    return observations, {
        "accepted": len(observations), "rejected": len(rejected), "duplicates": duplicates,
        "outliers_flagged": outliers, "errors": rejected[:100], "truncated": truncated,
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "format": "london_half_hourly" if london else "canonical",
        "time_semantics": "UTC; naive input interpreted in Europe/London" if london else f"UTC; naive input interpreted in {timezone_name}",
        "geographic_scope": "UK London households; not Karnataka" if london else region,
    }


parse_observations_csv = parse_csv
