import csv
import io
from typing import Dict, List, Tuple
from urllib.parse import urlparse


def parse_csv_file(raw: bytes, label: str) -> Tuple[List[Dict[str, str]], List[str]]:
    text = raw.decode("utf-8-sig", errors="ignore")
    reader = csv.DictReader(io.StringIO(text))
    errors: List[str] = []
    rows: List[Dict[str, str]] = []

    if not reader.fieldnames:
        return [], [f"{label}: CSV baslik satiri bulunamadi."]

    normalized_fields = {f.strip().lower(): f for f in reader.fieldnames if f}
    if "url" not in normalized_fields:
        return [], [f"{label}: 'url' kolonu zorunludur."]

    url_field = normalized_fields["url"]
    type_field = normalized_fields.get("type")

    for idx, row in enumerate(reader, start=2):
        raw_url = (row.get(url_field) or "").strip()
        if not raw_url:
            continue
        url = ensure_url_scheme(raw_url)
        record = {"url": url}
        if type_field:
            record["type"] = (row.get(type_field) or "").strip().lower()
        rows.append(record)

        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            errors.append(f"{label}: {idx}. satirdaki URL gecersiz ({raw_url})")

    return rows, errors


def parse_gsc_csv(raw: bytes) -> Tuple[List[Dict[str, str]], List[str]]:
    text = raw.decode("utf-8-sig", errors="ignore")
    reader = csv.DictReader(io.StringIO(text))
    errors: List[str] = []
    rows: List[Dict[str, str]] = []

    if not reader.fieldnames:
        return [], ["GSC CSV: baslik satiri bulunamadi."]

    fields = {f.strip().lower(): f for f in reader.fieldnames if f}
    if "url" not in fields:
        return [], ["GSC CSV: 'url' kolonu zorunludur."]

    for idx, row in enumerate(reader, start=2):
        url = ensure_url_scheme((row.get(fields["url"]) or "").strip())
        if not url:
            continue
        rows.append(
            {
                "url": url,
                "clicks": (row.get(fields.get("clicks", ""), "") or "0").strip() if fields.get("clicks") else "0",
                "impressions": (row.get(fields.get("impressions", ""), "") or "0").strip()
                if fields.get("impressions")
                else "0",
                "position": (row.get(fields.get("position", ""), "") or "999").strip()
                if fields.get("position")
                else "999",
            }
        )

        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            errors.append(f"GSC CSV: {idx}. satirdaki URL gecersiz.")

    return rows, errors


def ensure_url_scheme(value: str) -> str:
    if not value:
        return value
    if value.startswith(("http://", "https://")):
        return value
    return f"https://{value}"
