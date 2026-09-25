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
ESTIMATE_RE = re.compile(r"estimasi\s*(\d{2}/\d{2}/\d{4})\b", re.IGNORECASE)


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


def estimated_dates(value):
    """Return replacement dates explicitly introduced as an estimate."""
    return ESTIMATE_RE.findall(value or "")


def active_dates(value):
    """Return dates that are still active, excluding cancelled alternatives."""
    text = value or ""
    # A deferred equipment date becomes active only when the source explicitly
    # provides an estimate. This must take precedence over the cancelled date
    # earlier in the same cell.
    estimate = estimated_dates(text)
    if estimate:
        return estimate
    if "diputuskan setelah alat datang" in text.lower():
        return []
    # Explicit reschedule/re-UAT destinations take precedence over the old date.
    destination = re.search(r"(?:reschedule|re-?uat)\s+ke\s*(.+)$", text, flags=re.IGNORECASE)
    if destination:
        destination_dates = dates(destination.group(1))
        if destination_dates:
            return destination_dates
    if "cancelled" in text.lower():
        # A later conditional date, e.g. "Jika WhatsApp segera ready 28/10/2026",
        # is the replacement candidate. Dates before it are cancelled.
        conditional = re.search(r"\bJika\b(.*)$", text, flags=re.IGNORECASE)
        return dates(conditional.group(1)) if conditional else []
    return dates(text)


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
            # Some dated summary rows (for example "Review Feedback UAT")
            # have no sub-scenario cell; retain them as a valid scope item.
            if scenario and not sub and dates(uat):
                sub, prerequisite = scenario, ""
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
        sit_estimates, uat_estimates = estimated_dates(sit), estimated_dates(uat)
        # Do not retain the cancelled/deferred date in the published dataset
        # when an explicit estimated replacement is available.
        if sit_estimates:
            sit_dates = sit_estimates
        if uat_estimates:
            uat_dates = uat_estimates
        sit_active, uat_active = active_dates(sit), active_dates(uat)
        # Deferred equipment tests are intentionally omitted from both the
        # active calendar and the detail history until the equipment arrives.
        deferred_equipment = (
            ("diputuskan setelah alat datang" in sit.lower() or "diputuskan setelah alat datang" in uat.lower())
            and not (sit_estimates or uat_estimates)
        )
        if "diputuskan setelah alat datang" in sit.lower():
            sit_dates = []
        if "diputuskan setelah alat datang" in uat.lower():
            uat_dates = []
        if deferred_equipment:
            continue
        sit_iso = sorted({iso(value) for value in sit_dates})
        uat_iso = sorted({iso(value) for value in uat_dates})
        sit_active_iso = sorted({iso(value) for value in sit_active})
        uat_active_iso = sorted({iso(value) for value in uat_active})
        result.append({
            "scenario": current_scenario,
            "subScenario": sub,
            "prerequisite": prerequisite,
            "sitDates": sit_iso,
            "uatDates": uat_iso,
            # The latest chronological date is the effective date for reschedule/re-UAT.
            "sitDate": sit_active_iso[-1] if sit_active_iso else None,
            "uatDate": uat_active_iso[-1] if uat_active_iso else None,
            "deferredEquipment": deferred_equipment,
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
