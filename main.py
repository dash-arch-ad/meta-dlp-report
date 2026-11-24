import hashlib
import json
import math
import secrets
import time
import uuid
from urllib.parse import parse_qs, urlsplit
import os
import re
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
import requests
from google.oauth2.service_account import Credentials


# ==========================================
# 基本設定
# ==========================================

API_VERSION = "v27.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{API_VERSION}"


REPORT_TIMEZONE = "Asia/Tokyo"

FIXED_START_DATE = date(2026, 9, 1)
FIXED_END_DATE = date(2026, 11, 30)

HEADERS = [
    "media",
    "scope",
    "month",
    "day",
    "hour",
    "campaign_name",
    "adset_name",
    "ad_name",
    "gender",
    "age",
    "platform",
    "cv_view_1d",
    "cv_click_7d",
]


PURCHASE_ACTION_TYPES = [
    "offsite_conversion.fb_pixel_purchase",
    "purchase",
    "omni_purchase",
]


# ==========================================
# 設定読み込み
# ==========================================

class ExportError(Exception):
    """A redacted message suitable for the Actions log."""


def load_config():
    raw = os.environ.get("APP_SECRET_JSON")

    if not raw:
        raise ExportError(
            "GitHub Secrets の APP_SECRET_JSON が設定されていません。"
        )

    try:
        return json.loads(raw)

    except json.JSONDecodeError:
        raise ExportError("APP_SECRET_JSON のJSON形式が正しくありません。") from None



# ==========================================
# 取得期間
# ==========================================

def get_report_period():
    today = datetime.now(
        ZoneInfo(REPORT_TIMEZONE)
    ).date()

    # 案件開始日以前は取得しない
    if today <= FIXED_START_DATE:
        return None, None

    # 2026/9/2 ～ 2026/11/30 の間は
    # 9/1 ～ 起動日前日まで取得
    if FIXED_START_DATE < today <= FIXED_END_DATE:
        until = today - timedelta(days=1)

        return FIXED_START_DATE, until

    # 2026/12/1以降は全期間取得
    return FIXED_START_DATE, FIXED_END_DATE


# ==========================================
# Meta API
# ==========================================

def normalize_account_id(account_id):
    account_id = str(account_id).strip()

    digits = account_id.removeprefix("act_")
    if not re.fullmatch(r"[0-9]+", digits):
        raise ExportError("Invalid Meta account ID.")
    return f"act_{digits}"


def meta_page(url, token, params):
    try:
        response = requests.get(url, params=params, timeout=120, allow_redirects=False,
                                headers={"Authorization": f"Bearer {token}"})
    except requests.RequestException:
        raise ExportError("Meta API connection failed.") from None
    status = response.status_code
    try:
        data = response.json()
    except ValueError:
        raise ExportError(f"Meta API invalid JSON (HTTP {status}).") from None
    has_error = isinstance(data, dict) and "error" in data
    if not 200 <= status < 300 or has_error:
        error = data.get("error") if has_error else None
        details = ""
        if isinstance(error, dict):
            for key in ("code", "error_subcode"):
                if type(error.get(key)) is int and 0 <= error[key] <= 2_147_483_647:
                    details += f", {key}={error[key]}"
        raise ExportError(f"Meta API failed (HTTP {status}{details}).")
    return data


def next_cursor(data, endpoint):
    paging = data.get("paging", {})
    if not isinstance(paging, dict):
        raise ExportError("Meta returned invalid paging.")
    if "next" not in paging:
        return None
    link = paging["next"]
    if not isinstance(link, str):
        raise ExportError("Meta returned an invalid next page.")
    parts = urlsplit(link)
    if (parts.scheme != "https" or parts.netloc != "graph.facebook.com"
            or parts.path != urlsplit(endpoint).path or parts.fragment):
        raise ExportError("Meta returned an unexpected next page.")
    query = parse_qs(parts.query, keep_blank_values=True)
    after = query.get("after", [])
    if len(after) != 1 or not after[0] or any(k in query for k in ("before", "offset", "__paging_token")):
        raise ExportError("Meta returned unsupported pagination.")
    cursors = paging.get("cursors", {})
    if not isinstance(cursors, dict) or cursors.get("after", after[0]) != after[0]:
        raise ExportError("Meta returned inconsistent cursors.")
    return after[0]


def fetch_insights(
    token,
    account_id,
    since,
    until,
    level,
    fields,
    breakdowns=None,
    time_increment=None,
):
    account_id = normalize_account_id(
        account_id
    )

    url = (
        f"{GRAPH_API_BASE}/"
        f"{account_id}/insights"
    )

    params = {
        "level": level,
        "fields": ",".join(fields),
        "time_range": json.dumps({
            "since": since.isoformat(),
            "until": until.isoformat(),
        }),
        "action_attribution_windows": json.dumps([
            "1d_view",
            "7d_click",
        ]),
        "action_breakdowns": "action_type",
        "limit": 5000,
    }

    if breakdowns:
        params["breakdowns"] = ",".join(
            breakdowns
        )

    if time_increment is not None:
        params["time_increment"] = time_increment

    rows = []
    seen = set()
    while True:
        payload = meta_page(url, token, params)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ExportError("Meta returned an invalid data array.")
        for row in payload["data"]:
            if not isinstance(row, dict):
                raise ExportError("Meta returned an invalid row.")
            for field in [*fields, *(breakdowns or [])]:
                if field == "hourly_stats_aggregated_by_advertiser_time_zone":
                    parse_hour(row.get(field))
                    continue
                if field != "actions" and not isinstance(row.get(field), str):
                    raise ExportError("Meta returned an invalid report dimension.")
                if field in (breakdowns or []) and not row[field].strip():
                    raise ExportError("Meta returned an empty breakdown.")
            if time_increment is not None:
                try:
                    day = date.fromisoformat(row["date_start"])
                    if not since <= day <= until or row["date_start"] != day.isoformat():
                        raise ValueError
                except (TypeError, KeyError, ValueError):
                    raise ExportError("Meta returned an invalid date.") from None
            if "actions" in row and not isinstance(row["actions"], list):
                raise ExportError("Meta returned an invalid actions array.")
        rows.extend(payload["data"])
        after = next_cursor(payload, url)
        if after is None:
            return rows
        if token in after or after in seen:
            raise ExportError("Meta returned invalid or repeated pagination cursor.")
        seen.add(after)
        params["after"] = after


# ==========================================
# CV取得
# ==========================================

def to_number(value):
    try:
        if isinstance(value, bool):
            raise ValueError
        num = float(value)
        if not math.isfinite(num) or num < 0:
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise ExportError("Meta returned an invalid purchase count.") from None
    return int(num) if num.is_integer() else num


def get_purchase_cv(actions):
    if not actions:
        return 0, 0

    action_map = {}
    for item in actions:
        if not isinstance(item, dict) or not isinstance(item.get("action_type"), str) or not item["action_type"]:
            raise ExportError("Meta returned an invalid action.")
        action_type = item["action_type"]
        if action_type in PURCHASE_ACTION_TYPES:
            if action_type in action_map:
                raise ExportError("Meta returned duplicate purchase actions.")
            action_map[action_type] = item

    selected = None

    for action_type in PURCHASE_ACTION_TYPES:
        if action_type in action_map:
            selected = action_map[
                action_type
            ]
            break

    if selected is None:
        return 0, 0

    if "1d_view" not in selected and "7d_click" not in selected:
        raise ExportError("Meta returned a purchase action without the requested windows.")

    view_1d = to_number(
        selected.get(
            "1d_view",
            0
        )
    )

    click_7d = to_number(
        selected.get(
            "7d_click",
            0
        )
    )

    return view_1d, click_7d


# ==========================================
# 値変換
# ==========================================

def translate_gender(value):
    mapping = {
        "male": "男性",
        "female": "女性",
        "unknown": "不明",
    }

    return mapping.get(
        value,
        value or ""
    )


def parse_hour(value):
    text = str(value).strip()
    hour_pattern = r"(?:[01]?[0-9]|2[0-3])"
    clock = hour_pattern + r":[0-5][0-9](?::[0-5][0-9])?"
    if re.fullmatch(hour_pattern, text):
        return int(text)
    if re.fullmatch(rf"{clock}(?:\s*-\s*{clock})?", text):
        return int(text.split(":", 1)[0])
    raise ExportError("Meta returned an invalid hourly breakdown.")


def first_day_of_month(date_string):
    d = date.fromisoformat(
        date_string
    )

    return date(
        d.year,
        d.month,
        1
    )


def spreadsheet_date_serial(d):
    if not d:
        return ""

    base = date(
        1899,
        12,
        30
    )

    return (
        d - base
    ).days


# ==========================================
# 行生成
# ==========================================

def base_row(scope):
    return {
        "media": "Meta",
        "scope": scope,
        "month": "",
        "day": "",
        "hour": "",
        "campaign_name": "",
        "adset_name": "",
        "ad_name": "",
        "gender": "",
        "age": "",
        "platform": "",
        "cv_view_1d": 0,
        "cv_click_7d": 0,
    }


def row_to_list(row):
    return [
        row["media"],
        row["scope"],
        row["month"],
        row["day"],
        row["hour"],
        row["campaign_name"],
        row["adset_name"],
        row["ad_name"],
        row["gender"],
        row["age"],
        row["platform"],
        row["cv_view_1d"],
        row["cv_click_7d"],
    ]


# ==========================================
# campaign_month
# ==========================================

def build_campaign_month(
    token,
    account_id,
    since,
    until
):
    data = fetch_insights(
        token=token,
        account_id=account_id,
        since=since,
        until=until,
        level="campaign",
        fields=[
            "campaign_name",
            "actions",
        ],
        time_increment="monthly",
    )

    result = []

    for item in data:
        cv_view, cv_click = get_purchase_cv(
            item.get("actions")
        )

        row = base_row(
            "campaign_month"
        )

        month_date = first_day_of_month(
            item["date_start"]
        )

        row["month"] = spreadsheet_date_serial(
            month_date
        )

        row["campaign_name"] = item.get(
            "campaign_name",
            ""
        )

        row["cv_view_1d"] = cv_view
        row["cv_click_7d"] = cv_click

        result.append(
            row_to_list(row)
        )

    return result


# ==========================================
# campaign_day
# ==========================================

def build_campaign_day(
    token,
    account_id,
    since,
    until
):
    data = fetch_insights(
        token=token,
        account_id=account_id,
        since=since,
        until=until,
        level="campaign",
        fields=[
            "campaign_name",
            "actions",
        ],
        time_increment=1,
    )

    result = []

    for item in data:
        cv_view, cv_click = get_purchase_cv(
            item.get("actions")
        )

        row = base_row(
            "campaign_day"
        )

        day_date = date.fromisoformat(
            item["date_start"]
        )

        row["month"] = spreadsheet_date_serial(
            date(
                day_date.year,
                day_date.month,
                1
            )
        )

        row["day"] = spreadsheet_date_serial(
            day_date
        )

        row["campaign_name"] = item.get(
            "campaign_name",
            ""
        )

        row["cv_view_1d"] = cv_view
        row["cv_click_7d"] = cv_click

        result.append(
            row_to_list(row)
        )

    return result


# ==========================================
# hour
# ==========================================

def build_hour(
    token,
    account_id,
    since,
    until
):
    data = fetch_insights(
        token=token,
        account_id=account_id,
        since=since,
        until=until,
        level="account",
        fields=[
            "actions",
        ],
        breakdowns=[
            "hourly_stats_aggregated_by_advertiser_time_zone"
        ],
    )

    result = []

    for item in data:
        cv_view, cv_click = get_purchase_cv(
            item.get("actions")
        )

        row = base_row(
            "hour"
        )

        raw_hour = item.get(
            "hourly_stats_aggregated_by_advertiser_time_zone"
        )

        row["hour"] = parse_hour(
            raw_hour
        )

        row["cv_view_1d"] = cv_view
        row["cv_click_7d"] = cv_click

        result.append(
            row_to_list(row)
        )

    return result


# ==========================================
# ad
# ==========================================

def build_ad(
    token,
    account_id,
    since,
    until
):
    data = fetch_insights(
        token=token,
        account_id=account_id,
        since=since,
        until=until,
        level="ad",
        fields=[
            "campaign_name",
            "adset_name",
            "ad_name",
            "actions",
        ],
    )

    result = []

    for item in data:
        cv_view, cv_click = get_purchase_cv(
            item.get("actions")
        )

        row = base_row(
            "ad"
        )

        row["campaign_name"] = item.get(
            "campaign_name",
            ""
        )

        row["adset_name"] = item.get(
            "adset_name",
            ""
        )

        row["ad_name"] = item.get(
            "ad_name",
            ""
        )

        row["cv_view_1d"] = cv_view
        row["cv_click_7d"] = cv_click

        result.append(
            row_to_list(row)
        )

    return result


# ==========================================
# campaign_gen
# ==========================================

def build_campaign_gender(
    token,
    account_id,
    since,
    until
):
    data = fetch_insights(
        token=token,
        account_id=account_id,
        since=since,
        until=until,
        level="campaign",
        fields=[
            "campaign_name",
            "actions",
        ],
        breakdowns=[
            "gender"
        ],
    )

    result = []

    for item in data:
        cv_view, cv_click = get_purchase_cv(
            item.get("actions")
        )

        row = base_row(
            "campaign_gen"
        )

        row["campaign_name"] = item.get(
            "campaign_name",
            ""
        )

        row["gender"] = translate_gender(
            item.get("gender")
        )

        row["cv_view_1d"] = cv_view
        row["cv_click_7d"] = cv_click

        result.append(
            row_to_list(row)
        )

    return result


# ==========================================
# campaign_age
# ==========================================

def build_campaign_age(
    token,
    account_id,
    since,
    until
):
    data = fetch_insights(
        token=token,
        account_id=account_id,
        since=since,
        until=until,
        level="campaign",
        fields=[
            "campaign_name",
            "actions",
        ],
        breakdowns=[
            "age"
        ],
    )

    result = []

    for item in data:
        cv_view, cv_click = get_purchase_cv(
            item.get("actions")
        )

        row = base_row(
            "campaign_age"
        )

        row["campaign_name"] = item.get(
            "campaign_name",
            ""
        )

        row["age"] = item.get(
            "age",
            ""
        )

        row["cv_view_1d"] = cv_view
        row["cv_click_7d"] = cv_click

        result.append(
            row_to_list(row)
        )

    return result


# ==========================================
# campaign_pf
# ==========================================

def build_campaign_platform(
    token,
    account_id,
    since,
    until
):
    data = fetch_insights(
        token=token,
        account_id=account_id,
        since=since,
        until=until,
        level="campaign",
        fields=[
            "campaign_name",
            "actions",
        ],
        breakdowns=[
            "publisher_platform"
        ],
    )

    result = []

    for item in data:
        cv_view, cv_click = get_purchase_cv(
            item.get("actions")
        )

        row = base_row(
            "campaign_pf"
        )

        row["campaign_name"] = item.get(
            "campaign_name",
            ""
        )

        row["platform"] = item.get(
            "publisher_platform",
            ""
        )

        row["cv_view_1d"] = cv_view
        row["cv_click_7d"] = cv_click

        result.append(
            row_to_list(row)
        )

    return result


# ==========================================
# Google Sheets
# ==========================================

def open_sheet(config):
    service_account_info = config[
        "gcp_service_account"
    ]

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    credentials = (
        Credentials
        .from_service_account_info(
            service_account_info,
            scopes=scopes
        )
    )

    gc = gspread.authorize(
        credentials
    )

    spreadsheet = gc.open_by_key(
        config[
            "sheets"
        ][
            "spreadsheet_id"
        ]
    )

    return spreadsheet


LEASE_SECONDS = 3600


def sheet_state(book):
    return book.fetch_sheet_metadata(params={
        "fields": "sheets(properties(sheetId,title,sheetType,gridProperties(rowCount,columnCount))),"
                  "namedRanges(namedRangeId,name)"
    })


def range_names(state):
    return {item["name"]: item["namedRangeId"] for item in state.get("namedRanges", [])}


def delete_range(range_id):
    return {"deleteNamedRange": {"namedRangeId": range_id}}


def add_range(name, range_id, sheet_id):
    return {"addNamedRange": {"namedRange": {
        "name": name, "namedRangeId": range_id,
        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                  "startColumnIndex": 0, "endColumnIndex": 1},
    }}}


def lease_time(range_id):
    match = re.fullmatch(r"purchase_cv_([0-9]+)_[0-9a-f]{32}", range_id)
    if not match:
        raise ExportError("Unexpected Sheets lock. Check named ranges.")
    return int(match.group(1))


def acquire_lease(book, title):
    # All runners compete for one unique name. Each acquisition uses a fresh ID.
    key = hashlib.sha256(title.casefold().encode()).hexdigest()[:24]
    lock = "purchase_cv_lock_" + key
    state = sheet_state(book)
    existing = range_names(state).get(lock)
    now = int(time.time())
    if existing and now < lease_time(existing) + LEASE_SECONDS:
        return None
    token = f"purchase_cv_{now}_{uuid.uuid4().hex}"
    anchor = next(s["properties"]["sheetId"] for s in state["sheets"]
                  if s["properties"].get("sheetType", "GRID") == "GRID")
    lease = {"id": token, "name": lock, "receipt_prefix": "purchase_cv_done_" + key + "_",
             "anchor": anchor}
    ops = [delete_range(existing)] if existing else []
    ops.append(add_range(lock, token, anchor))
    try:
        book.batch_update({"requests": ops})
    except Exception:
        owner = range_names(sheet_state(book)).get(lock)
        if owner == token:
            return lease
        if owner and owner != existing:
            return None
        raise ExportError("Sheets lock acquisition failed.") from None
    return lease


def release_lease(book, lease, report=print):
    # IDs are never reused. A delayed cleanup cannot unlock the successor.
    try:
        if range_names(sheet_state(book)).get(lease["name"]) == lease["id"]:
            book.batch_update({"requests": [delete_range(lease["id"])]})
    except Exception:
        report("Sheets lock cleanup unconfirmed. Expiry permits recovery.")


def previous_report_height(book, title, new_height):
    escaped = title.replace("'", "''")
    result = book.values_get(f"'{escaped}'!A:M", params={"valueRenderOption": "FORMULA"})
    values = result.get("values", [])
    height = 0
    if values and values[0] == HEADERS:
        height = 1
        for row in values[1:]:
            if len(row) < 3 or row[0] != "Meta":
                break
            if row[1] not in {"campaign_month", "campaign_day", "hour", "ad", "campaign_gen", "campaign_age", "campaign_pf"}:
                break
            height += 1
    if any(any(cell != "" for cell in row) for row in values[height:new_height]):
        raise ExportError("Sheets output overlaps existing non-report data.")
    return height


def write_to_sheet(book, title, rows, lease):
    state = sheet_state(book)
    names = range_names(state)
    if names.get(lease["name"]) != lease["id"]:
        raise ExportError("Sheets lock ownership changed.")
    # A missing owner ID invalidates this *whole* batch even after the preliminary read.
    receipt_id = "receipt_" + lease["id"]
    receipt = lease["receipt_prefix"] + lease["id"]
    ops = [{"updateNamedRange": {"namedRange": {"namedRangeId": lease["id"], "name": receipt},
                                 "fields": "name"}},
           delete_range(lease["id"]), add_range(receipt, receipt_id, lease["anchor"])]
    # Keep receipts long enough for a timed-out writer to confirm its own commit.
    cutoff = int(time.time()) - 86400
    for name, range_id in names.items():
        saved_token = range_id.removeprefix("receipt_")
        if name == lease["receipt_prefix"] + saved_token and lease_time(saved_token) < cutoff:
            ops.append(delete_range(range_id))
    props = next((s["properties"] for s in state["sheets"]
                  if s["properties"]["title"] == title), None)
    output = [HEADERS, *rows]
    if props is None:
        sheet_id = secrets.randbelow(2**31 - 1) + 1
        height = len(output)
        ops.append({"addSheet": {"properties": {
            "sheetId": sheet_id, "title": title,
            "gridProperties": {"rowCount": max(1000, height), "columnCount": len(HEADERS)},
        }}})
    else:
        sheet_id = props["sheetId"]
        grid = props["gridProperties"]
        height = max(len(output), previous_report_height(book, title, len(output)))
        for dimension, current, needed in (("ROWS", grid["rowCount"], height),
                                            ("COLUMNS", grid["columnCount"], len(HEADERS))):
            if needed > current:
                ops.append({"appendDimension": {"sheetId": sheet_id, "dimension": dimension,
                                                "length": needed - current}})
    # updateCells with an explicit range clears only surplus values in the old report.
    cell_rows = []
    for row in output:
        cells = []
        for value in row:
            field = "numberValue" if isinstance(value, (int, float)) else "stringValue"
            cells.append({"userEnteredValue": {field: value}})
        cell_rows.append({"values": cells})
    ops.append({"updateCells": {
        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": height,
                  "startColumnIndex": 0, "endColumnIndex": len(HEADERS)},
        "rows": cell_rows, "fields": "userEnteredValue",
    }})
    for start_col, end_col, fmt, pattern in ((2, 4, "DATE", "yyyy-mm-dd"), (4, 5, "NUMBER", "0")):
        ops.append({"repeatCell": {
            "range": {"sheetId": sheet_id, "startColumnIndex": start_col, "endColumnIndex": end_col},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": fmt, "pattern": pattern}}},
            "fields": "userEnteredFormat.numberFormat",
        }})
    ops.append({"updateSheetProperties": {
        "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
        "fields": "gridProperties.frozenRowCount",
    }})
    try:
        book.batch_update({"requests": ops})
    except Exception:
        try:
            confirmed = range_names(sheet_state(book)).get(receipt) == receipt_id
        except Exception:
            raise ExportError("Sheets commit could not be confirmed.") from None
        if not confirmed:
            raise ExportError("Sheets commit was not confirmed.") from None



# ==========================================
# Main
# ==========================================

def main(report=print):
    config = load_config()

    since, until = get_report_period()

    if since is None or until is None:
        report(
            "取得対象期間外のため処理を終了します。"
        )
        return

    if until < since:
        report(
            "取得対象データがないため処理を終了します。"
        )
        return

    report(
        f"取得期間: "
        f"{since.isoformat()} "
        f"～ {until.isoformat()}"
    )

    token = config[
        "meta"
    ][
        "token"
    ]

    account_id = config[
        "meta"
    ][
        "account_id"
    ]

    if not isinstance(token, str) or not token.strip():
        raise ExportError("Meta token is missing.")
    book = open_sheet(config)
    lease = acquire_lease(book, "gitreport")
    if lease is None:
        report("他の実行が処理中のためスキップします。")
        return
    try:
        rows = collect_rows(token, account_id, since, until, report)
        write_to_sheet(book, "gitreport", rows, lease)
    except BaseException:
        release_lease(book, lease, report)
        raise
    report(f"gitreportへの出力が完了しました。出力行数: {len(rows)}")


def collect_rows(token, account_id, since, until, report=print):
    rows = []

    report(
        "campaign_month 取得中..."
    )

    rows.extend(
        build_campaign_month(
            token,
            account_id,
            since,
            until
        )
    )

    report(
        "campaign_day 取得中..."
    )

    rows.extend(
        build_campaign_day(
            token,
            account_id,
            since,
            until
        )
    )

    report(
        "hour 取得中..."
    )

    rows.extend(
        build_hour(
            token,
            account_id,
            since,
            until
        )
    )

    report(
        "ad 取得中..."
    )

    rows.extend(
        build_ad(
            token,
            account_id,
            since,
            until
        )
    )

    report(
        "campaign_gen 取得中..."
    )

    rows.extend(
        build_campaign_gender(
            token,
            account_id,
            since,
            until
        )
    )

    report(
        "campaign_age 取得中..."
    )

    rows.extend(
        build_campaign_age(
            token,
            account_id,
            since,
            until
        )
    )

    report(
        "campaign_pf 取得中..."
    )

    rows.extend(
        build_campaign_platform(
            token,
            account_id,
            since,
            until
        )
    )

    return rows


def run_cli() -> int:
    # Only report() can reach Actions. Suppress dependency logs, wire dumps,
    # warnings and raw tracebacks, including direct writes to file descriptors.
    with os.fdopen(os.dup(2), "w", buffering=1) as diagnostics:
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)

        def report(message: str) -> None:
            print(message, file=diagnostics, flush=True)

        try:
            main(report)
        except ExportError as error:
            report(str(error))
            return 1
        except Exception:
            report("Export failed. See the last operation above.")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(run_cli())
