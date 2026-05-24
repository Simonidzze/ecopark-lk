import csv
from datetime import datetime
from decimal import Decimal
import re

from sqlalchemy import delete, func, select

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
        campaign = find_campaign_for_import(session, campaign_data)
        if campaign is None:
            campaign = CallCampaign(**campaign_data, imported_at=imported_at)
            session.add(campaign)
            session.flush()
        else:
            campaign.title = campaign.title or campaign_data["title"]
            campaign.caller_phone = campaign.caller_phone or campaign_data["caller_phone"]
            campaign.called_at = campaign.called_at or campaign_data["called_at"]
            campaign.report_created_at = campaign_data["report_created_at"] or campaign.report_created_at
            campaign.callbacks = max(campaign.callbacks or 0, campaign_data["callbacks"] or 0)
            campaign.source_file = append_source_file(campaign.source_file, source_file)
            campaign.imported_at = imported_at
            session.flush()

        if source_file:
            session.execute(
                delete(CallAttempt)
                .where(CallAttempt.campaign_id == campaign.id)
                .where(CallAttempt.source_file == source_file)
            )
        for attempt in attempts:
            session.add(CallAttempt(campaign_id=campaign.id, source_file=source_file, **attempt))
        session.flush()

        collapse_campaign_attempts(session, campaign)

        session.commit()
        return {"campaign_id": campaign.id, "external_id": campaign.external_id, "attempts": len(attempts)}


def find_campaign_for_import(session, campaign_data):
    called_at = campaign_data.get("called_at")
    if called_at is not None:
        campaign = session.scalar(
            select(CallCampaign)
            .where(func.date(CallCampaign.called_at) == called_at.date().isoformat())
            .order_by(CallCampaign.id)
        )
        if campaign is not None:
            return campaign

    return session.scalar(
        select(CallCampaign).where(CallCampaign.external_id == campaign_data["external_id"])
    )


def append_source_file(existing, source_file):
    if not source_file:
        return existing or ""
    files = [item for item in (existing or "").split(", ") if item]
    if source_file not in files:
        files.append(source_file)
    return ", ".join(files)


def collapse_campaign_attempts(session, campaign):
    attempts = session.scalars(
        select(CallAttempt)
        .where(CallAttempt.campaign_id == campaign.id)
        .order_by(CallAttempt.called_at, CallAttempt.phone)
    ).all()
    grouped = {}
    for attempt in attempts:
        day = attempt.called_at.date().isoformat() if attempt.called_at else ""
        key = (attempt.phone_normalized, day)
        item = grouped.setdefault(
            key,
            {
                "phone": attempt.phone,
                "phone_normalized": attempt.phone_normalized,
                "call_duration_seconds": 0,
                "manager_duration_seconds": 0,
                "called_at": attempt.called_at,
                "cost": Decimal("0"),
                "comment": "",
                "source_file": "",
            },
        )
        item["call_duration_seconds"] += attempt.call_duration_seconds or 0
        item["manager_duration_seconds"] += attempt.manager_duration_seconds or 0
        item["cost"] += Decimal(attempt.cost or 0)
        item["source_file"] = append_source_file(item["source_file"], attempt.source_file)
        if attempt.comment:
            item["comment"] = append_source_file(item["comment"], attempt.comment)
        if attempt.called_at and (item["called_at"] is None or attempt.called_at < item["called_at"]):
            item["called_at"] = attempt.called_at

    session.execute(delete(CallAttempt).where(CallAttempt.campaign_id == campaign.id))
    for item in grouped.values():
        session.add(CallAttempt(campaign_id=campaign.id, **item))
    campaign.total_calls = len(grouped)


def merge_campaigns_by_day():
    Session = make_session_factory()
    with Session() as session:
        campaigns = session.scalars(
            select(CallCampaign).order_by(CallCampaign.called_at, CallCampaign.id)
        ).all()
        by_day = {}
        merged = 0
        for campaign in campaigns:
            if campaign.called_at is None:
                continue
            day = campaign.called_at.date().isoformat()
            primary = by_day.get(day)
            if primary is None:
                by_day[day] = campaign
                continue

            attempts = session.scalars(
                select(CallAttempt).where(CallAttempt.campaign_id == campaign.id)
            ).all()
            for attempt in attempts:
                attempt.campaign_id = primary.id
            primary.title = primary.title or campaign.title
            primary.source_file = append_source_file(primary.source_file, campaign.source_file)
            primary.callbacks = max(primary.callbacks or 0, campaign.callbacks or 0)
            session.delete(campaign)
            merged += 1

        session.flush()
        for campaign in by_day.values():
            collapse_campaign_attempts(session, campaign)

        session.commit()
        return {"days": len(by_day), "merged_campaigns": merged}
