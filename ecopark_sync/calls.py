import csv
from datetime import datetime
from decimal import Decimal
import re

from sqlalchemy import delete, select

from .db import make_session_factory
from .models import CallAttempt, CallCampaign
from .utils import now_utc_naive


def normalize_phone(value):
    digits = re.sub(r"\D+", "", value or "")
    if len(digits) == 11 and digits.startswith("8"):
        return "7" + digits[1:]
    if len(digits) == 10:
        return "7" + digits
    return digits


def parse_int(value):
    value = str(value or "").strip()
    if not value:
        return 0
    return int(Decimal(value.replace(",", ".")))


def parse_money(value):
    value = str(value or "").strip().replace("₽", "").replace(" ", "")
    if not value:
        return Decimal("0")
    return Decimal(value.replace(",", "."))


def parse_datetime_value(value):
    value = str(value or "").strip().strip('"')
    if not value:
        return None
    date_match = re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value)
    if date_match:
        value = date_match.group(0)
    else:
        date_match = re.search(r"\d{2}\.\d{2}\.\d{4} \(\d{2}:\d{2}:\d{2}\)", value)
        if date_match:
            value = date_match.group(0)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d.%m.%Y (%H:%M:%S)"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return datetime.fromisoformat(value)


def read_call_report(file_obj, source_file=""):
    text = file_obj.read()
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig")
    else:
        text = text.lstrip("\ufeff")

    rows = list(csv.reader(text.splitlines(), delimiter=";"))
    if not rows:
        raise RuntimeError("Пустой файл отчета обзвона")

    title = rows[0][0].strip().strip('"') if rows[0] else "Обзвон"
    external_id_match = re.search(r"(\d+)", title)
    external_id = external_id_match.group(1) if external_id_match else source_file or title

    campaign = {
        "external_id": external_id,
        "title": title,
        "caller_phone": "",
        "called_at": None,
        "report_created_at": None,
        "total_calls": 0,
        "callbacks": 0,
        "source_file": source_file,
    }
    attempts = []
    header_seen = False

    for row in rows[1:]:
        cells = [cell.strip().strip('"') for cell in row]
        if not any(cells):
            continue

        first = cells[0]
        if first == "Рассылка" and len(cells) > 1:
            campaign["called_at"] = parse_datetime_value(cells[1])
            if len(cells) > 3:
                campaign["report_created_at"] = parse_datetime_value(cells[3])
            continue
        if first == "Номер обзвона" and len(cells) > 1:
            campaign["caller_phone"] = cells[1]
            continue
        if first == "Сделано звонков" and len(cells) > 1:
            campaign["total_calls"] = parse_int(cells[1])
            continue
        if first == "Перезвонили" and len(cells) > 1:
            campaign["callbacks"] = parse_int(cells[1])
            continue
        if first == "Номер":
            header_seen = True
            continue
        if not header_seen:
            continue

        phone = first
        if not normalize_phone(phone):
            continue
        attempts.append(
            {
                "phone": phone,
                "phone_normalized": normalize_phone(phone),
                "call_duration_seconds": parse_int(cells[1] if len(cells) > 1 else 0),
                "manager_duration_seconds": parse_int(cells[2] if len(cells) > 2 else 0),
                "called_at": parse_datetime_value(cells[3] if len(cells) > 3 else ""),
                "cost": parse_money(cells[4] if len(cells) > 4 else ""),
                "comment": cells[5] if len(cells) > 5 else "",
            }
        )

    if not attempts:
        raise RuntimeError("В отчете не найдены строки звонков")
    if not campaign["total_calls"]:
        campaign["total_calls"] = len(attempts)

    return campaign, attempts


def import_call_report(file_obj, source_file=""):
    campaign_data, attempts = read_call_report(file_obj, source_file=source_file)
    Session = make_session_factory()
    imported_at = now_utc_naive()

    with Session() as session:
        campaign = session.scalar(
            select(CallCampaign).where(CallCampaign.external_id == campaign_data["external_id"])
        )
        if campaign is None:
            campaign = CallCampaign(**campaign_data, imported_at=imported_at)
            session.add(campaign)
            session.flush()
        else:
            for key, value in campaign_data.items():
                setattr(campaign, key, value)
            campaign.imported_at = imported_at
            session.flush()

        session.execute(delete(CallAttempt).where(CallAttempt.campaign_id == campaign.id))
        for attempt in attempts:
            session.add(CallAttempt(campaign_id=campaign.id, **attempt))

        session.commit()
        return {"campaign_id": campaign.id, "external_id": campaign.external_id, "attempts": len(attempts)}
