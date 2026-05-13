import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

import openpyxl

XLSX_PATH = "/Users/jacksonsharkey/Desktop/CommuneUSA.xlsx"
OUTPUT_DIR = Path(__file__).parent / "output"

SHEET_MAP = {
    "County Officials": "county_officials.json",
    "WA Officials": "wa_officials.json",
    "State Legislature": "state_legislature.json",
    "Federal Legislators": "federal_legislators.json",
}

VOTING_SHEETS = ["Voting Record", "Voting Record1"]

# Maps cleaned Excel column names → voting_records schema fields
VOTING_COL_MAP = {
    "official_name":      "official_name",
    "bill_motion":        "bill_name",
    "topic_category":     "topic_category",
    "date":               "vote_date",
    "their_vote":         "vote_cast",
    "result":             "result",
    "constituent_impact": "constituent_impact",
    "source_url":         "source_url",
}


def clean_header(raw):
    if raw is None:
        return None
    text = str(raw)
    text = "".join(c for c in text if unicodedata.category(c) != "So" and ord(c) < 128)
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return text or None


def serialize_value(val):
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date().isoformat()
    if isinstance(val, float):
        return int(val) if val == int(val) else val
    if isinstance(val, str):
        val = val.strip()
        return val if val else None
    return val


def is_section_row(row_values, headers):
    first = row_values[0]
    if first is not None and "▶" in str(first):
        return True
    populated = sum(1 for v in row_values if v is not None and str(v).strip())
    threshold = max(1, len(headers) * 0.30)
    return populated < threshold


def find_sheet(wb, name):
    target = name.lower()
    for sheet_name in wb.sheetnames:
        clean = re.sub(r"[^\x00-\x7F]", "", sheet_name).strip().lower()
        if target in clean:
            return wb[sheet_name]
    return None


def extract_records(ws):
    rows = list(ws.iter_rows(values_only=True))

    # Find header row: first row with more than one populated cell
    header_idx = None
    for i, row in enumerate(rows):
        populated = sum(1 for v in row if v is not None and str(v).strip())
        if populated > 1:
            header_idx = i
            break

    if header_idx is None:
        return []

    raw_headers = rows[header_idx]
    headers = [clean_header(h) for h in raw_headers]

    records = []
    for row in rows[header_idx + 1 :]:
        row_values = list(row)

        # Pad or trim to match header length
        while len(row_values) < len(headers):
            row_values.append(None)
        row_values = row_values[: len(headers)]

        if is_section_row(row_values, headers):
            continue

        # Skip entirely empty rows
        if all(v is None or str(v).strip() == "" for v in row_values):
            continue

        record = {}
        for key, val in zip(headers, row_values):
            if key is None:
                continue
            record[key] = serialize_value(val)
        records.append(record)

    return records


def extract_voting_records(wb):
    all_records = []
    for sheet_key in VOTING_SHEETS:
        ws = find_sheet(wb, sheet_key)
        if ws is None:
            print(f"  WARN  sheet not found: {sheet_key}")
            continue
        raw = extract_records(ws)
        count = 0
        for rec in raw:
            mapped = {
                "official_name":    rec.get("official_name"),
                "official_id":      None,
                "bill_name":        rec.get("bill_motion"),
                "bill_description": None,
                "topic_category":   rec.get("topic_category"),
                "vote_date":        rec.get("date"),
                "vote_cast":        rec.get("their_vote"),
                "result":           rec.get("result"),
                "constituent_impact": rec.get("constituent_impact"),
                "source_url":       rec.get("source_url"),
            }
            if not mapped["official_name"] and not mapped["bill_name"]:
                continue
            all_records.append(mapped)
            count += 1
        print(f"  OK    {sheet_key} → {count} records extracted")
    return all_records


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.load_workbook(XLSX_PATH, data_only=True)

    for sheet_name, output_file in SHEET_MAP.items():
        ws = find_sheet(wb, sheet_name)
        if ws is None:
            print(f"  WARN  sheet not found: {sheet_name}")
            continue

        records = extract_records(ws)
        out_path = OUTPUT_DIR / output_file

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)

        print(f"  OK    {sheet_name} → {output_file} ({len(records)} records)")

    voting_records = extract_voting_records(wb)
    out_path = OUTPUT_DIR / "voting_records.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(voting_records, f, indent=2, ensure_ascii=False)
    print(f"  OK    voting records → voting_records.json ({len(voting_records)} total)")


if __name__ == "__main__":
    main()
