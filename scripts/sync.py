#!/usr/bin/env python3
"""Fetch the public Google Doc and publish a small, cacheable calendar dataset."""
import json
import re
import sys
from urllib.parse import unquote
from datetime import datetime, timezone, timedelta
from html import unescape
from html.parser import HTMLParser
from urllib.request import Request, urlopen

SOURCE_URL = "https://docs.google.com/document/d/1a43x7WAKps9MfEAEcAe0HG_CfZE7ZrCZlW8wyyU5BQI/edit"
EXPORT_URL = SOURCE_URL.replace("/edit", "/export?format=html")
SCOPE_SOURCE_URL = "https://docs.google.com/document/d/1wXNFkmNGXnklR15dsUcCuufNT9E0C5b-pvDLa0vuC-g/edit?tab=t.0"
SCOPE_EXPORT_URL = SCOPE_SOURCE_URL.split("?", 1)[0].replace("/edit", "/export?format=html")
DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag == "td" and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag):
        if tag == "td" and self.row is not None and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(unescape(data))


def dates(value):
    return DATE_RE.findall(value or "")


def iso(date):
    return datetime.strptime(date, "%d/%m/%Y").strftime("%Y-%m-%d")


def parse_rows(html):
    parser = TableParser()
    parser.feed(html)
    if len(parser.rows) < 10 or parser.rows[0][:6] != ["No", "Skenario", "Sub Skenario", "Prasyarat", "Tanggal SIT", "Tanggal UAT"]:
        raise RuntimeError("Google Docs table layout is not recognized")

    current_scenario = ""
    result = []
    for row in parser.rows[1:]:
        if len(row) >= 7:
            scenario, sub, prerequisite, sit, uat = row[1:6]
            # Google Docs exports merged scenario cells as empty cells on
            # continuation rows; retain the most recent non-empty scenario.
            if scenario:
                current_scenario = scenario
        elif len(row) == 6:
            # Rows with an empty numbering cell omit that first cell.
            sub, prerequisite, sit, uat = row[1:5]
        elif len(row) == 4:
            # Rows with empty merged prerequisite cells omit that cell too.
            sub, prerequisite, sit, uat = row[0], "", row[1], row[2]
        elif len(row) >= 5:
            sub, prerequisite, sit, uat = row[0:4]
        else:
            continue
        if not sub or not current_scenario:
            continue
        sit_dates, uat_dates = dates(sit), dates(uat)
        sit_iso = sorted({iso(value) for value in sit_dates})
        uat_iso = sorted({iso(value) for value in uat_dates})
        result.append({
            "scenario": current_scenario,
            "subScenario": sub,
            "prerequisite": prerequisite,
            "sitDates": sit_iso,
            "uatDates": uat_iso,
            # The latest chronological date is the effective date for reschedule/re-UAT.
            "sitDate": sit_iso[-1] if sit_iso else None,
            "uatDate": uat_iso[-1] if uat_iso else None,
        })
    if not result or not any(item["sitDate"] or item["uatDate"] for item in result):
        raise RuntimeError("No calendar dates found in Google Docs table")
    return result


def extract_scope_links(html):
    # The scope document links three test-case sheets in section order:
    # UAT 16-09, UAT 18-09, and the 23-09 re-test.
    links = []
    for encoded in re.findall(r'href="https://www\.google\.com/url\?q=([^"&]+)', html):
        link = unquote(unescape(encoded)).replace("&amp;", "&")
        if link.startswith("https://docs.google.com/"):
            links.append(link)
    if len(links) < 3:
        raise RuntimeError("Expected three scope mapping links in the scope document")
    return {
        "2026-09-16": links[0],
        "2026-09-18": links[1],
        "2026-09-23": links[2],
    }


def main():
    request = Request(EXPORT_URL, headers={"User-Agent": "melawai-calendar-sync/1.0"})
    with urlopen(request, timeout=30) as response:
        html = response.read().decode("utf-8-sig")
    scope_request = Request(SCOPE_EXPORT_URL, headers={"User-Agent": "melawai-calendar-sync/1.0"})
    with urlopen(scope_request, timeout=30) as response:
        scope_html = response.read().decode("utf-8-sig")
    rows = parse_rows(html)
    scope_links = extract_scope_links(scope_html)
    events = {}
    for row in rows:
        label = f"{row['scenario']} - {row['subScenario']}"
        for kind, field in (("SIT", "sitDate"), ("UAT", "uatDate")):
            date = row[field]
            if date:
                events.setdefault(date, {"SIT": [], "UAT": []})[kind].append({
                    "label": label,
                    "scenario": row["scenario"],
                    "subScenario": row["subScenario"],
                    "prerequisite": row["prerequisite"],
                })

    refreshed = datetime.now(timezone(timedelta(hours=7))).isoformat(timespec="seconds")
    output = {"source": SOURCE_URL, "scopeSource": SCOPE_SOURCE_URL, "scopeLinks": scope_links,
              "refreshedAt": refreshed, "rows": rows, "events": events}
    with open("data.json", "w", encoding="utf-8") as target:
        json.dump(output, target, ensure_ascii=False, indent=2)
        target.write("\n")
    print(f"Published {len(rows)} rows and {len(events)} dates at {refreshed}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"sync failed: {error}", file=sys.stderr)
        sys.exit(1)
