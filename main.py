import json
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

API_VERSION = "v26.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{API_VERSION}"

SHEET_NAME = "gitreport"
MEDIA_NAME = "Meta"

REPORT_TIMEZONE = "Asia/Tokyo"

FIXED_START_DATE = date(2026, 6, 1)
FIXED_END_DATE = date(2026, 8, 31)

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


# 購入系action_type
# 同じ行に複数存在する場合の二重計上を避けるため、
# 上から優先して1種類だけ採用する
PURCHASE_ACTION_TYPES = [
    "offsite_conversion.fb_pixel_purchase",
    "purchase",
    "omni_purchase",
]


# ==========================================
# 設定読み込み
# ==========================================

def load_config():
    raw = os.environ.get("REPORT_CONFIG_JSON")

    if not raw:
        raise RuntimeError(
            "GitHub Secrets の REPORT_CONFIG_JSON が設定されていません。"
        )

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"REPORT_CONFIG_JSON のJSON形式が正しくありません: {e}"
        )


# ==========================================
# 取得期間
# ==========================================

def get_report_period():
    today = datetime.now(
        ZoneInfo(REPORT_TIMEZONE)
    ).date()

    # 案件開始前
    if today <= FIXED_START_DATE:
        return None, None

    # 案件期間中
    if FIXED_START_DATE < today <= FIXED_END_DATE:
        until = today - timedelta(days=1)
        return FIXED_START_DATE, until

    # 案件終了後
    return FIXED_START_DATE, FIXED_END_DATE


# ==========================================
# Meta API
# ==========================================

def normalize_account_id(account_id):
    account_id = str(account_id).strip()

    if account_id.startswith("act_"):
        return account_id

    return f"act_{account_id}"


def meta_get_all(url, params):
    rows = []

    while url:
        response = requests.get(
            url,
            params=params,
            timeout=120
        )

        if not response.ok:
            raise RuntimeError(
                f"Meta APIエラー\n"
                f"status={response.status_code}\n"
                f"response={response.text}"
            )

        payload = response.json()

        rows.extend(payload.get("data", []))

        url = (
            payload
            .get("paging", {})
            .get("next")
        )

        # paging.next にパラメータが含まれるため
        # 2ページ目以降はparams不要
        params = None

    return rows


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
    account_id = normalize_account_id(account_id)

    url = (
        f"{GRAPH_API_BASE}/"
        f"{account_id}/insights"
    )

    params = {
        "access_token": token,
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
        params["breakdowns"] = ",".join(breakdowns)

    if time_increment is not None:
        params["time_increment"] = time_increment

    return meta_get_all(url, params)


# ==========================================
# CV取得
# ==========================================

def to_number(value):
    if value in (None, ""):
        return 0

    try:
        num = float(value)

        if num.is_integer():
            return int(num)

        return num

    except (TypeError, ValueError):
        return 0


def get_purchase_cv(actions):
    if not actions:
        return 0, 0

    action_map = {
        item.get("action_type"): item
        for item in actions
        if item.get("action_type")
    }

    selected = None

    for action_type in PURCHASE_ACTION_TYPES:
        if action_type in action_map:
            selected = action_map[action_type]
            break

    if selected is None:
        return 0, 0

    view_1d = to_number(
        selected.get("1d_view", 0)
    )

    click_7d = to_number(
        selected.get("7d_click", 0)
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
    """
    Meta APIの
    00:00:00 - 00:59:59
    のような値から0～23を取得
    """

    if value is None:
        return ""

    text = str(value)

    match = re.match(
        r"^\s*(\d{1,2}):",
        text
    )

    if match:
        return int(match.group(1))

    try:
        hour = int(text)

        if 0 <= hour <= 23:
            return hour
    except ValueError:
        pass

    raise ValueError(
        f"hourの形式を判定できません: {value}"
    )


def first_day_of_month(date_string):
    d = date.fromisoformat(date_string)

    return date(
        d.year,
        d.month,
        1
    )


def spreadsheet_date_serial(d):
    """
    Google Sheetsの日付型として書き込むためのシリアル値
    """

    if not d:
        return ""

    base = date(1899, 12, 30)

    return (d - base).days


# ==========================================
# 行生成
# ==========================================

def base_row(scope):
    return {
        "media": MEDIA_NAME,
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
# scope別取得
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

        row = base_row("campaign_month")

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

        row = base_row("campaign_day")

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

        row = base_row("hour")

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

        row = base_row("ad")

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

        row = base_row("campaign_gen")

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

        row = base_row("campaign_age")

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

        row = base_row("campaign_pf")

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
        config["sheets"]["spreadsheet_id"]
    )

    try:
        worksheet = spreadsheet.worksheet(
            SHEET_NAME
        )

    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=SHEET_NAME,
            rows=1000,
            cols=len(HEADERS),
        )

    return worksheet


def write_sheet(
    worksheet,
    rows
):
    worksheet.clear()

    all_values = [
        HEADERS,
        *rows
    ]

    worksheet.update(
        range_name="A1",
        values=all_values,
        value_input_option="RAW",
    )

    # month / day をGoogle Sheetsの日付型表示
    worksheet.format(
        "C:D",
        {
            "numberFormat": {
                "type": "DATE",
                "pattern": "yyyy-mm-dd",
            }
        }
    )

    # hourは整数表示
    worksheet.format(
        "E:E",
        {
            "numberFormat": {
                "type": "NUMBER",
                "pattern": "0",
            }
        }
    )

    worksheet.freeze(
        rows=1
    )


# ==========================================
# Main
# ==========================================

def main():
    config = load_config()

    since, until = get_report_period()

    if since is None or until is None:
        print(
            "取得対象期間外のため処理を終了します。"
        )
        return

    if until < since:
        print(
            "取得対象データがないため処理を終了します。"
        )
        return

    print(
        f"取得期間: "
        f"{since.isoformat()} "
        f"～ {until.isoformat()}"
    )

    token = config["meta"]["token"]
    account_id = config["meta"]["account_id"]

    rows = []

    print("campaign_month 取得中...")
    rows.extend(
        build_campaign_month(
            token,
            account_id,
            since,
            until
        )
    )

    print("campaign_day 取得中...")
    rows.extend(
        build_campaign_day(
            token,
            account_id,
            since,
            until
        )
    )

    print("hour 取得中...")
    rows.extend(
        build_hour(
            token,
            account_id,
            since,
            until
        )
    )

    print("ad 取得中...")
    rows.extend(
        build_ad(
            token,
            account_id,
            since,
            until
        )
    )

    print("campaign_gen 取得中...")
    rows.extend(
        build_campaign_gender(
            token,
            account_id,
            since,
            until
        )
    )

    print("campaign_age 取得中...")
    rows.extend(
        build_campaign_age(
            token,
            account_id,
            since,
            until
        )
    )

    print("campaign_pf 取得中...")
    rows.extend(
        build_campaign_platform(
            token,
            account_id,
            since,
            until
        )
    )

    print(
        f"出力行数: {len(rows)}"
    )

    worksheet = open_sheet(
        config
    )

    write_sheet(
        worksheet,
        rows
    )

    print(
        "gitreportへの出力が完了しました。"
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as e:
        print(
            f"ERROR: {e}",
            file=sys.stderr
        )
        raise
