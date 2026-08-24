import os
import json
import time
import requests
import gspread

from zoneinfo import ZoneInfo
from datetime import datetime, date, timedelta
from oauth2client.service_account import ServiceAccountCredentials


# ============================================================
# Settings
# ============================================================

META_API_VERSION = "v25.0"

JST = ZoneInfo("Asia/Tokyo")

WORKSHEET_NAME = "gitreport"

# Metaの購入アクション
PURCHASE_ACTION_TYPE = "offsite_conversion.fb_pixel_purchase"

# 時間帯は広告アカウントのタイムゾーンを使用
HOURLY_BREAKDOWN = "hourly_stats_aggregated_by_advertiser_time_zone"

# API再試行
MAX_RETRIES = 5
RETRY_BASE_SECONDS = 2


# ============================================================
# Main
# ============================================================

def main():
    print("=== Start Meta Hourly Purchase Export ===")

    config = load_secret()
    resolved = resolve_config(config)
    validate_config(resolved)

    since, until = get_target_date_range()

    print(f"Target range: {since:%Y-%m-%d} to {until:%Y-%m-%d}")

    meta_data = fetch_meta_hourly_purchase(
        act_id=resolved["meta"]["account_id"],
        token=resolved["meta"]["token"],
        since=since,
        until=until,
    )

    rows = build_output_rows(
        api_rows=meta_data,
        since=since,
        until=until,
    )

    print(f"Output rows: {len(rows)}")

    spreadsheet = connect_spreadsheet(
        sheet_id=resolved["sheet"]["spreadsheet_id"],
        google_creds_dict=resolved["sheet"]["google_service_account"],
    )

    write_to_sheet(
        spreadsheet=spreadsheet,
        rows=rows,
    )

    print("=== Completed ===")


# ============================================================
# Secrets
# ============================================================

def load_secret():
    secret_env = os.environ.get("APP_SECRET_JSON")

    if not secret_env:
        raise RuntimeError("APP_SECRET_JSON is not set")

    try:
        return json.loads(secret_env)

    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"APP_SECRET_JSON is invalid JSON: {e}"
        ) from e


def resolve_config(config):
    meta_conf = config.get("meta", {})
    sheets_conf = config.get("sheets", {})

    google_service_account = normalize_google_service_account(
        config.get("gcp_service_account")
    )

    return {
        "meta": {
            "token": meta_conf.get("token"),
            "account_id": meta_conf.get("account_id"),
        },
        "sheet": {
            "spreadsheet_id": sheets_conf.get("spreadsheet_id"),
            "google_service_account": google_service_account,
        },
    }


def validate_config(resolved):
    required = {
        "meta.token": resolved["meta"]["token"],
        "meta.account_id": resolved["meta"]["account_id"],
        "sheets.spreadsheet_id": resolved["sheet"]["spreadsheet_id"],
        "gcp_service_account": resolved["sheet"]["google_service_account"],
    }

    missing = [
        key
        for key, value in required.items()
        if not value
    ]

    if missing:
        raise RuntimeError(
            f"Missing required config keys: {', '.join(missing)}"
        )


def normalize_google_service_account(creds):
    if not creds:
        return None

    fixed = dict(creds)

    private_key = fixed.get("private_key", "")

    if private_key:
        fixed["private_key"] = private_key.replace("\\n", "\n")

    return fixed


# ============================================================
# Date range
# ============================================================

def add_months(base_date, months):
    month = base_date.month - 1 + months

    year = base_date.year + month // 12
    month = month % 12 + 1

    return date(
        year,
        month,
        1,
    )


def get_target_date_range():
    """
    実行日を基準に、

    当月
    前月
    前々月

    の3ヶ月を取得。

    当日は含めず、前日まで。

    例：
    実行日 2026-08-24

    since = 2026-06-01
    until = 2026-08-23
    """

    today_jst = datetime.now(JST).date()

    yesterday = today_jst - timedelta(days=1)

    this_month_start = date(
        today_jst.year,
        today_jst.month,
        1,
    )

    # 当月を含む過去3ヶ月
    since = add_months(
        this_month_start,
        -2,
    )

    until = yesterday

    # 月初1日に実行した場合、
    # yesterdayは前月なので問題なく取得可能
    return since, until


# ============================================================
# Meta API
# ============================================================

def normalize_meta_act_id(raw_act_id):
    cleaned = (
        str(raw_act_id)
        .replace("act=", "")
        .replace("act_", "")
        .replace("act", "")
        .strip()
    )

    return f"act_{cleaned}"


def fetch_meta_hourly_purchase(
    act_id,
    token,
    since,
    until,
):
    """
    日付 × 時間帯ごとの購入CVを取得。

    attribution:
    - 1d_view
    - 7d_click
    """

    normalized_act_id = normalize_meta_act_id(act_id)

    url = (
        f"https://graph.facebook.com/"
        f"{META_API_VERSION}/"
        f"{normalized_act_id}/insights"
    )

    params = {
        "access_token": token,

        # アカウント全体で集計
        "level": "account",

        "time_range": json.dumps({
            "since": since.strftime("%Y-%m-%d"),
            "until": until.strftime("%Y-%m-%d"),
        }),

        # 日単位 × 時間帯
        "time_increment": "1",

        # 広告アカウントのタイムゾーン
        "breakdowns": json.dumps([
            HOURLY_BREAKDOWN
        ]),

        # actionsをaction_type単位で取得
        "action_breakdowns": json.dumps([
            "action_type"
        ]),

        # 必要なアトリビューションのみ指定
        "action_attribution_windows": json.dumps([
            "1d_view",
            "7d_click",
        ]),

        "fields": ",".join([
            "date_start",
            "date_stop",
            "actions",
        ]),

        "limit": 5000,
    }

    all_rows = []

    while True:

        data = meta_request_with_retry(
            url=url,
            params=params,
        )

        batch = data.get("data", [])

        all_rows.extend(batch)

        next_url = (
            data
            .get("paging", {})
            .get("next")
        )

        if not next_url:
            break

        # paging.nextには必要なパラメータがすべて含まれる
        url = next_url
        params = None

    return all_rows


def meta_request_with_retry(
    url,
    params=None,
):
    """
    Meta APIの一時的な
    429 / 5xx / code 1 / code 2
    などに対応。
    """

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):

        try:
            response = requests.get(
                url,
                params=params,
                timeout=120,
            )

        except requests.RequestException as e:
            last_error = e

            if attempt == MAX_RETRIES:
                break

            sleep_seconds = RETRY_BASE_SECONDS ** attempt

            print(
                f"Meta request retry "
                f"{attempt}/{MAX_RETRIES}"
            )

            time.sleep(sleep_seconds)
            continue

        try:
            payload = response.json()

        except ValueError:
            payload = {}

        if response.ok and "error" not in payload:
            return payload

        error = payload.get("error", {})

        error_code = error.get("code")
        error_subcode = error.get("error_subcode")
        error_message = error.get("message", "")

        retryable_status = (
            response.status_code == 429
            or response.status_code >= 500
        )

        retryable_meta_code = error_code in {
            1,
            2,
            4,
            17,
            32,
            613,
        }

        if (
            attempt < MAX_RETRIES
            and (
                retryable_status
                or retryable_meta_code
            )
        ):
            sleep_seconds = RETRY_BASE_SECONDS ** attempt

            print(
                f"Meta temporary error. "
                f"Retry {attempt}/{MAX_RETRIES}. "
                f"status={response.status_code}, "
                f"code={error_code}, "
                f"subcode={error_subcode}"
            )

            time.sleep(sleep_seconds)
            continue

        raise RuntimeError(
            "Meta API request failed. "
            f"status={response.status_code}, "
            f"code={error_code}, "
            f"subcode={error_subcode}, "
            f"message={error_message}"
        )

    raise RuntimeError(
        f"Meta API request failed after {MAX_RETRIES} retries: "
        f"{type(last_error).__name__}"
    )


# ============================================================
# Actions
# ============================================================

def extract_purchase_attribution(actions):
    """
    Meta actions例：

    [
        {
            "action_type":
                "offsite_conversion.fb_pixel_purchase",
            "value": "10",
            "1d_view": "2",
            "7d_click": "8"
        }
    ]

    ↓

    (2, 8)
    """

    view_1d = 0
    click_7d = 0

    if not isinstance(actions, list):
        return view_1d, click_7d

    for action in actions:

        if not isinstance(action, dict):
            continue

        if action.get("action_type") != PURCHASE_ACTION_TYPE:
            continue

        view_1d += to_number(
            action.get("1d_view")
        )

        click_7d += to_number(
            action.get("7d_click")
        )

    return view_1d, click_7d


# ============================================================
# Output
# ============================================================

def build_output_rows(
    api_rows,
    since,
    until,
):
    """
    すべての日付 × 24時間を作成して、
    Meta APIのCV値を反映。

    CVなしの時間帯は0。
    """

    hourly_data = {}

    # --------------------------------------------------------
    # まず全期間 × 24時間を0で作成
    # --------------------------------------------------------

    current_date = since

    while current_date <= until:

        date_text = current_date.strftime("%Y-%m-%d")

        for hour in range(24):

            hour_text = f"{hour:02d}:00"

            hourly_data[
                (date_text, hour_text)
            ] = {
                "view_1d": 0,
                "click_7d": 0,
            }

        current_date += timedelta(days=1)

    # --------------------------------------------------------
    # Metaデータ反映
    # --------------------------------------------------------

    for item in api_rows:

        date_text = normalize_date(
            item.get("date_start")
        )

        raw_hour = item.get(
            HOURLY_BREAKDOWN,
            "",
        )

        hour_text = normalize_hour(
            raw_hour
        )

        if not date_text or not hour_text:
            continue

        view_1d, click_7d = (
            extract_purchase_attribution(
                item.get("actions", [])
            )
        )

        key = (
            date_text,
            hour_text,
        )

        if key not in hourly_data:
            hourly_data[key] = {
                "view_1d": 0,
                "click_7d": 0,
            }

        hourly_data[key]["view_1d"] += view_1d
        hourly_data[key]["click_7d"] += click_7d

    # --------------------------------------------------------
    # Sheet rows
    # --------------------------------------------------------

    rows = []

    for (
        date_text,
        hour_text,
    ), metrics in sorted(
        hourly_data.items()
    ):

        rows.append([
            "meta",
            "time",
            date_text,
            hour_text,
            normalize_number_for_sheet(
                metrics["view_1d"]
            ),
            normalize_number_for_sheet(
                metrics["click_7d"]
            ),
        ])

    return rows


def normalize_date(value):
    if not value:
        return ""

    return str(value)[:10]


def normalize_hour(value):
    """
    Meta API:

    00:00:00 - 00:59:59

    ↓

    00:00
    """

    if not value:
        return ""

    value = str(value).strip()

    start = value.split("-")[0].strip()

    if len(start) >= 5:
        return start[:5]

    return ""


# ============================================================
# Google Sheets
# ============================================================

def connect_spreadsheet(
    sheet_id,
    google_creds_dict,
):
    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]

        creds = (
            ServiceAccountCredentials
            .from_json_keyfile_dict(
                google_creds_dict,
                scope,
            )
        )

        client = gspread.authorize(creds)

        spreadsheet = client.open_by_key(
            sheet_id
        )

        print(
            "Google Sheets connected successfully"
        )

        return spreadsheet

    except Exception as e:
        raise RuntimeError(
            f"Google Sheets connection error: "
            f"{type(e).__name__}"
        ) from e


def write_to_sheet(
    spreadsheet,
    rows,
):
    header = [[
        "Media",
        "scope",
        "Month",
        "time",
        "purchase_1d_view",
        "purchase_7d_click",
    ]]

    try:
        try:
            worksheet = spreadsheet.worksheet(
                WORKSHEET_NAME
            )

        except gspread.WorksheetNotFound:
            worksheet = (
                spreadsheet.add_worksheet(
                    title=WORKSHEET_NAME,
                    rows=3000,
                    cols=6,
                )
            )

        worksheet.clear()

        output = header + rows

        # 必要行数・列数を確保
        required_rows = len(output)

        if worksheet.row_count < required_rows:
            worksheet.resize(
                rows=required_rows,
                cols=6,
            )

        worksheet.update(
            range_name="A1",
            values=output,
            value_input_option="RAW",
        )

    except Exception as e:
        raise RuntimeError(
            f"Write error ({WORKSHEET_NAME}): "
            f"{type(e).__name__}"
        ) from e


# ============================================================
# Utils
# ============================================================

def to_number(value):
    if value in (
        None,
        "",
    ):
        return 0

    try:
        return float(value)

    except (
        TypeError,
        ValueError,
    ):
        return 0


def normalize_number_for_sheet(value):
    """
    2.0 → 2
    2.5 → 2.5
    """

    if float(value).is_integer():
        return int(value)

    return value


# ============================================================
# Run
# ============================================================

if __name__ == "__main__":
    main()
