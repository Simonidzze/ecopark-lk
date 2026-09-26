from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile


PDF_CONTENT_TYPE = "application/pdf"
DEFAULT_DEBT_PERIOD_START = date(2025, 10, 1)
DEFAULT_CLAIM_BASIS = "01.10.2025"
TOKEN_PATTERN = re.compile(r"\{\{[A-Z][A-Z0-9_]*\}\}")
CADASTRAL_NUMBER_PATTERN = re.compile(r"(?<!\d)(\d{2}:\d{2}:\d{6,7}:\d+)(?!\d)")

CLAIM_TOKENS = (
    "TSN_ADDRESS",
    "TSN_PHONE",
    "TSN_EMAIL",
    "PLOT_NUMBER",
    "OWNER_NAME",
    "PLOT_ADDRESS",
    "CADASTRAL_NUMBER",
    "CLAIM_NUMBER",
    "CLAIM_DATE",
    "CHARGE_BASIS",
    "ACCOUNT",
    "CALCULATION_DATE",
    "DEBT_PERIOD",
    "PRINCIPAL_AMOUNT",
    "PENALTY_TEXT",
    "TOTAL_AMOUNT",
)


class ClaimPdfConversionError(RuntimeError):
    pass


def claim_template_path():
    return Path(__file__).resolve().parent.parent / "templates" / "documents" / "pretrial_claim.docx"


def clean_xml_text(value):
    value = str(value or "").strip()
    return "".join(
        character
        for character in value
        if character in "\t\n\r"
        or "\u0020" <= character <= "\ud7ff"
        or "\ue000" <= character <= "\ufffd"
    )


def format_ru_date(value):
    if isinstance(value, datetime):
        value = value.date()
    months = (
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    )
    return f"{value.day} {months[value.month - 1]} {value.year} г."


def format_short_date(value):
    if isinstance(value, datetime):
        value = value.date()
    return value.strftime("%d.%m.%Y")


def format_ru_money(value):
    amount = Decimal(value or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    rubles = int(amount)
    kopecks = int((amount - Decimal(rubles)) * 100)
    rubles_text = f"{rubles:,}".replace(",", " ")
    return f"{sign}{rubles_text} руб. {kopecks:02d} коп."


def extract_cadastral_number(value):
    match = CADASTRAL_NUMBER_PATTERN.search(str(value or ""))
    return match.group(1) if match else ""


def parse_iso_date(value, field_name, default=None):
    value = str(value or "").strip()
    if not value:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Поле «{field_name}» должно содержать дату в формате ГГГГ-ММ-ДД") from exc


def format_debt_period(period_from, period_to):
    if period_from and period_to:
        return f"с {format_short_date(period_from)} по {format_short_date(period_to)}"
    if period_from:
        return f"с {format_short_date(period_from)}"
    if period_to:
        return f"не указан (по состоянию на {format_short_date(period_to)})"
    return "не указан"


def penalty_description(amount, basis=""):
    amount = Decimal(amount or 0)
    if amount <= 0:
        return "не начислены"
    basis = str(basis or "").strip().rstrip(".")
    if not basis:
        basis = "основание не указано"
    else:
        basis = f"основание: {basis}"
    return f"{format_ru_money(amount)}; {basis}"


def claim_values(
    *,
    tsn_address,
    tsn_phone,
    tsn_email,
    plot_number,
    owner_name,
    plot_address,
    cadastral_number,
    claim_number,
    claim_date,
    charge_basis,
    account,
    calculation_date,
    debt_period_from,
    debt_period_to,
    principal_amount,
    penalty_amount,
    penalty_basis,
    total_amount,
):
    charge_basis = str(charge_basis or "").strip().rstrip(".")
    return {
        "TSN_ADDRESS": tsn_address or "не указан",
        "TSN_PHONE": tsn_phone or "не указан",
        "TSN_EMAIL": tsn_email or "не указан",
        "PLOT_NUMBER": plot_number or "не указан",
        "OWNER_NAME": owner_name or "не указан",
        "PLOT_ADDRESS": plot_address or "не указан",
        "CADASTRAL_NUMBER": cadastral_number or "не указан",
        "CLAIM_NUMBER": claim_number or "б/н",
        "CLAIM_DATE": format_ru_date(claim_date),
        "CHARGE_BASIS": charge_basis or DEFAULT_CLAIM_BASIS,
        "ACCOUNT": account or "не указан",
        "CALCULATION_DATE": format_short_date(calculation_date),
        "DEBT_PERIOD": format_debt_period(debt_period_from, debt_period_to),
        "PRINCIPAL_AMOUNT": format_ru_money(principal_amount),
        "PENALTY_TEXT": penalty_description(penalty_amount, penalty_basis),
        "TOTAL_AMOUNT": format_ru_money(total_amount),
    }


def render_pretrial_claim(values, template_path=None):
    template_path = Path(template_path or claim_template_path())
    missing_keys = sorted(set(CLAIM_TOKENS) - set(values))
    if missing_keys:
        raise ValueError(f"Не переданы значения для шаблона: {', '.join(missing_keys)}")

    output = BytesIO()
    with ZipFile(template_path, "r") as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename == "word/document.xml":
                document_xml = payload.decode("utf-8")
                for key in CLAIM_TOKENS:
                    token = "{{" + key + "}}"
                    if token not in document_xml:
                        raise ValueError(f"В DOCX-шаблоне отсутствует поле {token}")
                    replacement = escape(clean_xml_text(values[key]))
                    document_xml = document_xml.replace(token, replacement)

                unresolved = sorted(set(TOKEN_PATTERN.findall(document_xml)))
                if unresolved:
                    raise ValueError(f"В DOCX остались незаполненные поля: {', '.join(unresolved)}")
                payload = document_xml.encode("utf-8")
            target.writestr(info, payload)

    output.seek(0)
    return output


def convert_docx_to_pdf(document, converter=None, timeout=120):
    converter_name = converter or os.environ.get("LIBREOFFICE_BINARY", "").strip()
    if converter_name:
        converter_path = shutil.which(converter_name)
    else:
        converter_path = shutil.which("soffice") or shutil.which("libreoffice")
    if not converter_path:
        raise ClaimPdfConversionError(
            "LibreOffice не найден: установите soffice/libreoffice или задайте LIBREOFFICE_BINARY"
        )

    original_position = document.tell()
    document.seek(0)
    document_bytes = document.read()
    document.seek(original_position)

    with TemporaryDirectory(prefix="ecopark-claim-") as temporary_directory:
        working_directory = Path(temporary_directory)
        source_path = working_directory / "claim.docx"
        output_path = working_directory / "claim.pdf"
        profile_path = working_directory / "libreoffice-profile"
        source_path.write_bytes(document_bytes)
        profile_path.mkdir()

        command = (
            converter_path,
            "--headless",
            "--nologo",
            "--nodefault",
            "--nolockcheck",
            "--nofirststartwizard",
            f"-env:UserInstallation={profile_path.as_uri()}",
            "--convert-to",
            "pdf:writer_pdf_Export",
            "--outdir",
            str(working_directory),
            str(source_path),
        )
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ClaimPdfConversionError(
                f"LibreOffice не завершил конвертацию за {timeout} секунд"
            ) from exc
        except OSError as exc:
            raise ClaimPdfConversionError(f"Не удалось запустить LibreOffice: {exc}") from exc

        if result.returncode != 0:
            details = (result.stderr or result.stdout or "неизвестная ошибка").strip()
            raise ClaimPdfConversionError(
                f"LibreOffice завершил конвертацию с кодом {result.returncode}: {details}"
            )
        if not output_path.is_file():
            details = (result.stderr or result.stdout or "PDF-файл не создан").strip()
            raise ClaimPdfConversionError(f"LibreOffice не создал PDF: {details}")

        pdf_bytes = output_path.read_bytes()
        if not pdf_bytes.startswith(b"%PDF-"):
            raise ClaimPdfConversionError("LibreOffice создал файл с некорректной сигнатурой PDF")

    return BytesIO(pdf_bytes)


def render_pretrial_claim_pdf(values, template_path=None, converter=None):
    document = render_pretrial_claim(values, template_path=template_path)
    return convert_docx_to_pdf(document, converter=converter)


def safe_claim_filename(plot_number, claim_date, claim_number=None, extension="docx"):
    plot_slug = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_-]+", "-", str(plot_number or "").strip())
    plot_slug = plot_slug.strip("-") or "unknown"
    number_slug = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_-]+", "-", str(claim_number or "").strip())
    number_suffix = f"-n-{number_slug.strip('-')}" if number_slug.strip("-") else ""
    extension = re.sub(r"[^0-9A-Za-z]+", "", str(extension or "")) or "pdf"
    return f"pretenziya-uchastok-{plot_slug}{number_suffix}-{claim_date.isoformat()}.{extension}"
