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

PURCHASE_ACTION_TYPE = "offsite_conversion.fb_pixel_purchase"

HOURLY_BREAKDOWN = "hourly_stats_aggregated_by_advertiser_time_zone"

MAX_RETRIES = 5
RETRY_BASE_SECONDS = 2

# 通常は7日単位で取得
DEFAULT_CHUNK_DAYS = 7


# ============================================================
# Main
# ============================================================

def main():
    print("=== Start Meta Hourly Purchase Export ===")

    config = load_secret()

    resolved = resolve_config(config)

    validate_config(resolved)

    since, until = get_target_date_range()

    print(
        f"Target range: "
        f"{since:%Y-%m-%d} to {until:%Y-%m-%d}"
    )

    meta_data = fetch_meta_hourly_purchase(
        act_id=resolved["meta"]["account_id"],
        token=resolved["meta"]["token"],
        since=since,
        until=until,
    )

    print(
        f"Meta API rows fetched: {len(meta_data)}"
    )

    rows = build_output_rows(
        api_rows=meta_data,
        since=since,
        until=until,
    )

    print(
        f"Output rows built: {len(rows)}"
    )

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
    secret_env = os.environ.get(
        "APP_SECRET_JSON"
    )

    if not secret_env:
        raise RuntimeError(
            "APP_SECRET_JSON is not set"
        )

    try:
        return json.loads(
            secret_env
        )

    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"APP_SECRET_JSON is invalid JSON: {e}"
        ) from e


def resolve_config(config):
    meta_conf = config.get(
        "meta",
        {},
    )

    sheets_conf = config.get(
        "sheets",
        {},
    )

    google_service_account = (
        normalize_google_service_account(
            config.get(
                "gcp_service_account"
            )
        )
    )

    return {
        "meta": {
            "token": meta_conf.get(
                "token"
            ),
            "account_id": meta_conf.get(
                "account_id"
            ),
        },
        "sheet": {
            "spreadsheet_id": sheets_conf.get(
                "spreadsheet_id"
            ),
            "google_service_account": (
                google_service_account
            ),
        },
    }


def validate_config(resolved):
    required = {
        "meta.token": (
            resolved["meta"]["token"]
        ),
        "meta.account_id": (
            resolved["meta"]["account_id"]
        ),
        "sheets.spreadsheet_id": (
            resolved["sheet"]["spreadsheet_id"]
        ),
        "gcp_service_account": (
            resolved["sheet"]["google_service_account"]
        ),
    }

    missing = [
        key
        for key, value in required.items()
        if not value
    ]

    if missing:
        raise RuntimeError(
            "Missing required config keys: "
            + ", ".join(missing)
        )


def normalize_google_service_account(
    creds
):
    if not creds:
        return None

    fixed = dict(
        creds
    )

    private_key = fixed.get(
        "private_key",
        "",
    )

    if private_key:
        fixed["private_key"] = (
            private_key.replace(
                "\\n",
                "\n",
            )
        )

    return fixed


# ============================================================
# Date
# ============================================================

def add_months(
    base_date,
    months,
):
    month = (
        base_date.month
        - 1
        + months
    )

    year = (
        base_date.year
        + month // 12
    )

    month = (
        month % 12
        + 1
    )

    return date(
        year,
        month,
        1,
    )


def get_target_date_range():
    """
    当月を含む過去3ヶ月。

    当日は含めず、前日まで。

    例：
    実行日 2026-08-24

    2026-06-01 ～ 2026-08-23
    """

    today_jst = (
        datetime.now(JST).date()
    )

    yesterday = (
        today_jst
        - timedelta(days=1)
    )

    this_month_start = date(
        today_jst.year,
        today_jst.month,
        1,
    )

    since = add_months(
        this_month_start,
        -2,
    )

    until = yesterday

    return since, until


def split_date_ranges(
    since,
    until,
    max_days,
):
    ranges = []

    current_since = since

    while current_since <= until:
        current_until = min(
            current_since
            + timedelta(
                days=max_days - 1
            ),
            until,
        )

        ranges.append(
            (
                current_since,
                current_until,
            )
        )

        current_since = (
            current_until
            + timedelta(days=1)
        )

    return ranges


# ============================================================
# Meta
# ============================================================

def normalize_meta_act_id(
    raw_act_id
):
    cleaned = (
        str(raw_act_id)
        .replace(
            "act=",
            "",
        )
        .replace(
            "act_",
            "",
        )
        .replace(
            "act",
            "",
        )
        .strip()
    )

    return (
        f"act_{cleaned}"
    )


def fetch_meta_hourly_purchase(
    act_id,
    token,
    since,
    until,
):
    """
    過去3ヶ月を7日単位で分割取得。

    7日取得でMeta APIが
    "reduce the amount of data"
    を返した場合は
    1日単位へ自動フォールバック。
    """

    normalized_act_id = (
        normalize_meta_act_id(
            act_id
        )
    )

    ranges = split_date_ranges(
        since=since,
        until=until,
        max_days=DEFAULT_CHUNK_DAYS,
    )

    all_rows = []

    total_ranges = len(
        ranges
    )

    for index, (
        chunk_since,
        chunk_until,
    ) in enumerate(
        ranges,
        start=1,
    ):

        print(
            f"Fetching Meta hourly data "
            f"{index}/{total_ranges}: "
            f"{chunk_since:%Y-%m-%d} to "
            f"{chunk_until:%Y-%m-%d}"
        )

        try:
            batch = (
                fetch_meta_hourly_purchase_chunk(
                    act_id=normalized_act_id,
                    token=token,
                    since=chunk_since,
                    until=chunk_until,
                )
            )

            all_rows.extend(
                batch
            )

        except MetaDataTooLargeError:
            print(
                "Chunk too large. "
                "Falling back to daily requests: "
                f"{chunk_since:%Y-%m-%d} to "
                f"{chunk_until:%Y-%m-%d}"
            )

            daily_ranges = split_date_ranges(
                since=chunk_since,
                until=chunk_until,
                max_days=1,
            )

            for (
                daily_since,
                daily_until,
            ) in daily_ranges:

                print(
                    "Fetching fallback daily data: "
                    f"{daily_since:%Y-%m-%d}"
                )

                daily_batch = (
                    fetch_meta_hourly_purchase_chunk(
                        act_id=normalized_act_id,
                        token=token,
                        since=daily_since,
                        until=daily_until,
                    )
                )

                all_rows.extend(
                    daily_batch
                )

    return all_rows


class MetaDataTooLargeError(
    RuntimeError
):
    pass


def fetch_meta_hourly_purchase_chunk(
    act_id,
    token,
    since,
    until,
):
    """
    指定期間の

    日付 × 時間帯 × 購入CV

    を取得。
    """

    url = (
        f"https://graph.facebook.com/"
        f"{META_API_VERSION}/"
        f"{act_id}/insights"
    )

    params = {
        "access_token": token,

        "level": "account",

        "time_range": json.dumps({
            "since": (
                since.strftime(
                    "%Y-%m-%d"
                )
            ),
            "until": (
                until.strftime(
                    "%Y-%m-%d"
                )
            ),
        }),

        # 日別 × 時間帯
        "time_increment": "1",

        # 広告アカウントの
        # タイムゾーン基準
        "breakdowns": json.dumps([
            HOURLY_BREAKDOWN
        ]),

        # 欲しいアトリビューションのみ
        "action_attribution_windows": (
            json.dumps([
                "1d_view",
                "7d_click",
            ])
        ),

        # actions内から
        # purchaseだけ抽出する
        "fields": "actions",

        "limit": 5000,
    }

    all_rows = []

    while True:
        data = meta_request_with_retry(
            url=url,
            params=params,
        )

        batch = data.get(
            "data",
            [],
        )

        all_rows.extend(
            batch
        )

        next_url = (
            data
            .get(
                "paging",
                {},
            )
            .get(
                "next"
            )
        )

        if not next_url:
            break

        url = next_url
        params = None

    return all_rows


def meta_request_with_retry(
    url,
    params=None,
):
    last_error = None

    for attempt in range(
        1,
        MAX_RETRIES + 1,
    ):

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

            sleep_seconds = (
                RETRY_BASE_SECONDS
                ** attempt
            )

            print(
                "Meta request connection error. "
                f"Retry {attempt}/{MAX_RETRIES}"
            )

            time.sleep(
                sleep_seconds
            )

            continue

        try:
            payload = (
                response.json()
            )

        except ValueError:
            payload = {}

        if (
            response.ok
            and "error" not in payload
        ):
            return payload

        error = payload.get(
            "error",
            {},
        )

        error_code = error.get(
            "code"
        )

        error_subcode = error.get(
            "error_subcode"
        )

        error_message = error.get(
            "message",
            "",
        )

        # ------------------------------------------
        # データ量過多
        # ------------------------------------------

        message_lower = (
            str(error_message).lower()
        )

        if (
            error_code == 1
            and (
                "reduce the amount of data"
                in message_lower
            )
        ):
            raise MetaDataTooLargeError(
                "Meta API requested too much data. "
                f"status={response.status_code}, "
                f"code={error_code}, "
                f"subcode={error_subcode}"
            )

        # ------------------------------------------
        # 一時的なAPIエラー
        # ------------------------------------------

        retryable_status = (
            response.status_code == 429
            or response.status_code >= 500
        )

        retryable_meta_code = (
            error_code in {
                1,
                2,
                4,
                17,
                32,
                613,
            }
        )

        if (
            attempt < MAX_RETRIES
            and (
                retryable_status
                or retryable_meta_code
            )
        ):
            sleep_seconds = (
                RETRY_BASE_SECONDS
                ** attempt
            )

            print(
                "Meta temporary error. "
                f"Retry {attempt}/{MAX_RETRIES}. "
                f"status={response.status_code}, "
                f"code={error_code}, "
                f"subcode={error_subcode}"
            )

            time.sleep(
                sleep_seconds
            )

            continue

        # tokenなどはログへ出さない
        raise RuntimeError(
            "Meta API request failed. "
            f"status={response.status_code}, "
            f"code={error_code}, "
            f"subcode={error_subcode}, "
            f"message={error_message}"
        )

    raise RuntimeError(
        "Meta API request failed after "
        f"{MAX_RETRIES} retries. "
        f"error={type(last_error).__name__}"
    )


# ============================================================
# Purchase Attribution
# ============================================================

def extract_purchase_attribution(
    actions
):
    """
    actionsから

    offsite_conversion.fb_pixel_purchase

    の

    1d_view
    7d_click

    を抽出。
    """

    view_1d = 0
    click_7d = 0

    if not isinstance(
        actions,
        list,
    ):
        return (
            view_1d,
            click_7d,
        )

    for action in actions:

        if not isinstance(
            action,
            dict,
        ):
            continue

        action_type = (
            action.get(
                "action_type"
            )
        )

        if (
            action_type
            != PURCHASE_ACTION_TYPE
        ):
            continue

        view_1d += to_number(
            action.get(
                "1d_view"
            )
        )

        click_7d += to_number(
            action.get(
                "7d_click"
            )
        )

    return (
        view_1d,
        click_7d,
    )


# ============================================================
# Output rows
# ============================================================

def build_output_rows(
    api_rows,
    since,
    until,
):
    """
    出力：

    A Media
    B scope
    C Month
    D time
    E purchase_1d_view
    F purchase_7d_click

    CVがない時間帯も0で出力。
    """

    hourly_data = {}

    # ------------------------------------------
    # 全日付 × 24時間を作成
    # ------------------------------------------

    current_date = since

    while current_date <= until:

        date_text = (
            current_date.strftime(
                "%Y-%m-%d"
            )
        )

        for hour in range(
            24
        ):

            hour_text = (
                f"{hour:02d}:00"
            )

            hourly_data[
                (
                    date_text,
                    hour_text,
                )
            ] = {
                "view_1d": 0,
                "click_7d": 0,
            }

        current_date += (
            timedelta(days=1)
        )

    # ------------------------------------------
    # Meta API値を反映
    # ------------------------------------------

    for item in api_rows:

        date_text = normalize_date(
            item.get(
                "date_start"
            )
        )

        raw_hour = item.get(
            HOURLY_BREAKDOWN,
            "",
        )

        hour_text = normalize_hour(
            raw_hour
        )

        if (
            not date_text
            or not hour_text
        ):
            continue

        (
            view_1d,
            click_7d,
        ) = extract_purchase_attribution(
            item.get(
                "actions",
                [],
            )
        )

        key = (
            date_text,
            hour_text,
        )

        if key not in hourly_data:
            hourly_data[
                key
            ] = {
                "view_1d": 0,
                "click_7d": 0,
            }

        hourly_data[
            key
        ]["view_1d"] += (
            view_1d
        )

        hourly_data[
            key
        ]["click_7d"] += (
            click_7d
        )

    # ------------------------------------------
    # Sheet用
    # ------------------------------------------

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
                metrics[
                    "view_1d"
                ]
            ),
            normalize_number_for_sheet(
                metrics[
                    "click_7d"
                ]
            ),
        ])

    return rows


def normalize_date(
    value
):
    if not value:
        return ""

    return str(
        value
    )[:10]


def normalize_hour(
    value
):
    """
    Meta API：

    00:00:00 - 00:59:59

    ↓

    00:00
    """

    if not value:
        return ""

    value = str(
        value
    ).strip()

    start = (
        value
        .split("-")[0]
        .strip()
    )

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
            (
                "https://spreadsheets."
                "google.com/feeds"
            ),
            (
                "https://www.googleapis.com/"
                "auth/drive"
            ),
        ]

        creds = (
            ServiceAccountCredentials
            .from_json_keyfile_dict(
                google_creds_dict,
                scope,
            )
        )

        client = gspread.authorize(
            creds
        )

        spreadsheet = (
            client.open_by_key(
                sheet_id
            )
        )

        print(
            "Google Sheets connected successfully"
        )

        return spreadsheet

    except Exception as e:
        raise RuntimeError(
            "Google Sheets connection error: "
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
            worksheet = (
                spreadsheet.worksheet(
                    WORKSHEET_NAME
                )
            )

        except gspread.WorksheetNotFound:
            worksheet = (
                spreadsheet.add_worksheet(
                    title=WORKSHEET_NAME,
                    rows=3000,
                    cols=6,
                )
            )

        output = (
            header
            + rows
        )

        required_rows = len(
            output
        )

        if (
            worksheet.row_count
            < required_rows
        ):
            worksheet.resize(
                rows=required_rows,
                cols=6,
            )

        # 既存値を消してから更新
        worksheet.clear()

        worksheet.update(
            range_name="A1",
            values=output,
            value_input_option="RAW",
        )

        print(
            f"Sheet updated: "
            f"{WORKSHEET_NAME}, "
            f"{len(rows)} rows"
        )

    except Exception as e:
        raise RuntimeError(
            f"Write error "
            f"({WORKSHEET_NAME}): "
            f"{type(e).__name__}"
        ) from e


# ============================================================
# Utils
# ============================================================

def to_number(
    value
):
    if value in (
        None,
        "",
    ):
        return 0

    try:
        return float(
            value
        )

    except (
        TypeError,
        ValueError,
    ):
        return 0


def normalize_number_for_sheet(
    value
):
    try:
        numeric = float(
            value
        )

    except (
        TypeError,
        ValueError,
    ):
        return 0

    if numeric.is_integer():
        return int(
            numeric
        )

    return numeric


# ============================================================
# Run
# ============================================================

if __name__ == "__main__":
    main()
