from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from .config import env, require_dependency
from .db import make_session_factory
from .models import Accrual, Balance, OwnerPlot, Payment
from .web import plot_sort_key

try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
except ModuleNotFoundError:
    Credentials = None
    build = None


SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)


def month_key(value):
    if value is None:
        return ""
    return value.strftime("%Y-%m")


def money(value):
    if value is None:
        return 0
    value = Decimal(value)
    return float(value)


def load_report_data():
    Session = make_session_factory()
    with Session() as session:
        owner_rows = session.execute(
            select(
                OwnerPlot.id,
                OwnerPlot.plot_number,
                OwnerPlot.owner,
                Balance.total,
            ).outerjoin(Balance, Balance.owner_plot_id == OwnerPlot.id)
        ).mappings().all()

        accrual_rows = session.execute(
            select(Accrual.owner_plot_id, Accrual.date, Accrual.amount)
        ).all()
        payment_rows = session.execute(
            select(Payment.owner_plot_id, Payment.date, Payment.amount)
        ).all()

    accruals_by_owner_plot = defaultdict(Decimal)
    payments_by_owner_plot = defaultdict(Decimal)
    accruals_by_month = defaultdict(lambda: defaultdict(Decimal))
    payments_by_month = defaultdict(lambda: defaultdict(Decimal))
    months = set()

    for owner_plot_id, date, amount in accrual_rows:
        amount = Decimal(amount or 0)
        accruals_by_owner_plot[owner_plot_id] += amount
        month = month_key(date)
        if month:
            months.add(month)
            accruals_by_month[owner_plot_id][month] += amount

    for owner_plot_id, date, amount in payment_rows:
        amount = Decimal(amount or 0)
        payments_by_owner_plot[owner_plot_id] += amount
        month = month_key(date)
        if month:
            months.add(month)
            payments_by_month[owner_plot_id][month] += amount

    sorted_months = sorted(months)
    sorted_owner_rows = sorted(owner_rows, key=plot_sort_key)
    return sorted_owner_rows, sorted_months, accruals_by_owner_plot, payments_by_owner_plot, accruals_by_month, payments_by_month


def build_sheet_values():
    (
        owner_rows,
        months,
        accruals_by_owner_plot,
        payments_by_owner_plot,
        accruals_by_month,
        payments_by_month,
    ) = load_report_data()

    fixed_header = ["Участок", "Общий долг", "Начислено", "Оплачено"]
    header = fixed_header.copy()
    subheader = fixed_header.copy()
    for month in months:
        header.extend([month, ""])
        subheader.extend(["Начислено", "Оплачено"])

    updated_at_row = [f"Обновлено: {datetime.now().strftime('%d.%m.%Y')}"]
    values = [updated_at_row, header, subheader]
    for row in owner_rows:
        if row["owner"] == "ИП Матросов":
            continue
        if "_" in str(row["plot_number"]):
            continue

        owner_plot_id = row["id"]
        line = [
            row["plot_number"],
            money(row["total"]),
            money(accruals_by_owner_plot[owner_plot_id]),
            money(payments_by_owner_plot[owner_plot_id]),
        ]
        for month in months:
            line.extend([
                money(accruals_by_month[owner_plot_id][month]),
                money(payments_by_month[owner_plot_id][month]),
            ])
        values.append(line)

    return values


def sheets_service():
    require_dependency(Credentials, "google-auth")
    require_dependency(build, "google-api-python-client")
    credentials = Credentials.from_service_account_file(
        env("GOOGLE_SERVICE_ACCOUNT_FILE", required=True),
        scopes=SCOPES,
    )
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def ensure_sheet(service, spreadsheet_id, worksheet):
    spreadsheet = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for sheet in spreadsheet.get("sheets", []):
        properties = sheet.get("properties", {})
        if properties.get("title") == worksheet:
            return properties["sheetId"]

    response = service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": worksheet}}}]},
    ).execute()
    return response["replies"][0]["addSheet"]["properties"]["sheetId"]


def export_to_google_sheets():
    spreadsheet_id = env("GOOGLE_SHEETS_SPREADSHEET_ID", required=True)
    worksheet = env("GOOGLE_SHEETS_WORKSHEET", "Участки")
    values = build_sheet_values()
    service = sheets_service()
    sheet_id = ensure_sheet(service, spreadsheet_id, worksheet)
    column_count = len(values[1])

    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=f"'{worksheet}'",
        body={},
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{worksheet}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "unmergeCells": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": max(len(values), 3),
                            "startColumnIndex": 0,
                            "endColumnIndex": column_count,
                        }
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": 0,
                            "endIndex": column_count,
                        },
                        "properties": {"hiddenByUser": False},
                        "fields": "hiddenByUser",
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": 2,
                            "endIndex": 4,
                        },
                        "properties": {"hiddenByUser": True},
                        "fields": "hiddenByUser",
                    }
                },
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_id,
                            "gridProperties": {"frozenRowCount": 3, "frozenColumnCount": 2},
                        },
                        "fields": "gridProperties(frozenRowCount,frozenColumnCount)",
                    }
                },
                {"clearBasicFilter": {"sheetId": sheet_id}},
                {
                    "mergeCells": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "endRowIndex": 3,
                            "startColumnIndex": 0,
                            "endColumnIndex": 1,
                        },
                        "mergeType": "MERGE_ALL",
                    }
                },
                {
                    "mergeCells": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "endRowIndex": 3,
                            "startColumnIndex": 1,
                            "endColumnIndex": 2,
                        },
                        "mergeType": "MERGE_ALL",
                    }
                },
                *[
                    {
                        "mergeCells": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 1,
                                "endRowIndex": 2,
                                "startColumnIndex": column_index,
                                "endColumnIndex": column_index + 2,
                            },
                            "mergeType": "MERGE_ALL",
                        }
                    }
                    for column_index in range(4, column_count, 2)
                ],
                {
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 3},
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 0.09, "green": 0.42, "blue": 0.36},
                                "horizontalAlignment": "CENTER",
                                "textFormat": {
                                    "bold": True,
                                    "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                                },
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat)",
                    }
                },
                {
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 0.96, "green": 0.98, "blue": 0.97},
                                "horizontalAlignment": "LEFT",
                                "textFormat": {
                                    "bold": True,
                                    "foregroundColor": {"red": 0.09, "green": 0.42, "blue": 0.36},
                                },
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat)",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 3,
                            "endRowIndex": len(values),
                            "startColumnIndex": 1,
                            "endColumnIndex": column_count,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                },
                {
                    "updateBorders": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": len(values),
                            "startColumnIndex": 0,
                            "endColumnIndex": column_count,
                        },
                        "top": {"style": "SOLID", "width": 1, "color": {"red": 0.75, "green": 0.78, "blue": 0.82}},
                        "bottom": {"style": "SOLID", "width": 1, "color": {"red": 0.75, "green": 0.78, "blue": 0.82}},
                        "left": {"style": "SOLID", "width": 1, "color": {"red": 0.75, "green": 0.78, "blue": 0.82}},
                        "right": {"style": "SOLID", "width": 1, "color": {"red": 0.75, "green": 0.78, "blue": 0.82}},
                        "innerHorizontal": {"style": "SOLID", "width": 1, "color": {"red": 0.88, "green": 0.90, "blue": 0.93}},
                        "innerVertical": {"style": "SOLID", "width": 1, "color": {"red": 0.88, "green": 0.90, "blue": 0.93}},
                    }
                },
                {
                    "autoResizeDimensions": {
                        "dimensions": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": 0,
                            "endIndex": column_count,
                        }
                    }
                },
            ]
        },
    ).execute()

    return {
        "spreadsheet_id": spreadsheet_id,
        "worksheet": worksheet,
        "rows": len(values) - 3,
        "columns": column_count,
    }
