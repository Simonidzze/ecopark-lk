#!/usr/bin/env python3
"""Build the application DOCX template from the approved Word reference."""

from hashlib import sha256
from html import unescape
from pathlib import Path
import re
import sys
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile


EXPECTED_SHA256 = "13024d74add6e7cea8c011a7ea79e4b4fef6e16dd2c25b953bbf29f08d19ca46"

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

PARAGRAPH_PATTERN = re.compile(r"<w:p\b[\s\S]*?</w:p>")
ROW_PATTERN = re.compile(r"<w:tr\b[\s\S]*?</w:tr>")
CELL_PATTERN = re.compile(r"<w:tc\b[\s\S]*?</w:tc>")


def fail(message):
    raise RuntimeError(message)


def visible_text(xml):
    text_parts = re.findall(r"<w:t\b[^>]*>([\s\S]*?)</w:t>", xml)
    return unescape("".join(text_parts))


def replacement_paragraph(paragraph_xml, replacement):
    opening_tag = re.match(r"<w:p\b[^>]*>", paragraph_xml)
    if opening_tag is None:
        fail(f"Could not preserve paragraph wrapper for {visible_text(paragraph_xml)!r}")
    paragraph_properties = re.search(r"<w:pPr>[\s\S]*?</w:pPr>", paragraph_xml)
    run_properties = re.search(r"<w:rPr>[\s\S]*?</w:rPr>", paragraph_xml)
    ppr = paragraph_properties.group(0) if paragraph_properties else ""
    rpr = run_properties.group(0) if run_properties else ""
    value = escape(replacement)
    return (
        f'{opening_tag.group(0)}{ppr}<w:r>{rpr}'
        f'<w:t xml:space="preserve">{value}</w:t></w:r></w:p>'
    )


def replace_paragraph(document_xml, source_text, replacement):
    count = 0

    def callback(match):
        nonlocal count
        paragraph_xml = match.group(0)
        if visible_text(paragraph_xml).strip() != source_text.strip():
            return paragraph_xml
        count += 1
        return replacement_paragraph(paragraph_xml, replacement)

    result = PARAGRAPH_PATTERN.sub(callback, document_xml)
    if count != 1:
        fail(f"Expected one paragraph {source_text!r}, found {count}")
    return result


def remove_paragraph(document_xml, source_text):
    count = 0

    def callback(match):
        nonlocal count
        paragraph_xml = match.group(0)
        if visible_text(paragraph_xml).strip() != source_text.strip():
            return paragraph_xml
        count += 1
        return ""

    result = PARAGRAPH_PATTERN.sub(callback, document_xml)
    if count != 1:
        fail(f"Expected one removable paragraph {source_text!r}, found {count}")
    return result


def replace_table_value(document_xml, label, replacement):
    count = 0

    def row_callback(row_match):
        nonlocal count
        row_xml = row_match.group(0)
        cells = list(CELL_PATTERN.finditer(row_xml))
        if len(cells) < 2 or visible_text(cells[0].group(0)).strip() != label:
            return row_xml
        value_cell = cells[1]
        cell_xml = value_cell.group(0)
        paragraphs = list(PARAGRAPH_PATTERN.finditer(cell_xml))
        if not paragraphs:
            fail(f"Table row {label!r} has no value paragraph")
        paragraph = paragraphs[0]
        updated_cell = (
            cell_xml[: paragraph.start()]
            + replacement_paragraph(paragraph.group(0), replacement)
            + cell_xml[paragraph.end() :]
        )
        count += 1
        return row_xml[: value_cell.start()] + updated_cell + row_xml[value_cell.end() :]

    result = ROW_PATTERN.sub(row_callback, document_xml)
    if count != 1:
        fail(f"Expected one table row {label!r}, found {count}")
    return result


def assert_remaining_blanks_are_manual(document_xml):
    unexpected = []
    for paragraph in PARAGRAPH_PATTERN.findall(document_xml):
        text = visible_text(paragraph).strip()
        if "___" not in text:
            continue
        if text.startswith("___________________ / Дорогин"):
            continue
        if set(text) <= {"_"}:
            continue
        unexpected.append(text)
    if unexpected:
        fail(f"Unconverted fill-in blanks: {unexpected}")


def build(source_path, output_path):
    source_bytes = source_path.read_bytes()
    actual_sha256 = sha256(source_bytes).hexdigest()
    if actual_sha256 != EXPECTED_SHA256:
        fail(
            "Reference DOCX checksum mismatch: "
            f"expected {EXPECTED_SHA256}, got {actual_sha256}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(source_path, "r") as source, ZipFile(output_path, "w", ZIP_DEFLATED) as target:
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename == "word/document.xml":
                xml = payload.decode("utf-8")
                replacements = (
                    (
                        "Адрес: 633204, Новосибирская область, г. Искитим,",
                        "Адрес: {{TSN_ADDRESS}}",
                    ),
                    (
                        "Телефон: +7 961 846-52-56 | Электронная почта: tsn-ecopark@mail.ru",
                        "Телефон: {{TSN_PHONE}} | Электронная почта: {{TSN_EMAIL}}",
                    ),
                    (
                        "Собственнику земельного участка № ___",
                        "Собственнику земельного участка № {{PLOT_NUMBER}}",
                    ),
                    ("__________________________________________", "{{OWNER_NAME}}"),
                    (
                        "Адрес участка: Новосибирская область, Искитимский муниципальный район, сельское поселение Быстровский сельсовет, с. Быстровка, микрорайон Экопарк, з/у № 10\"",
                        "Адрес участка: {{PLOT_ADDRESS}}",
                    ),
                    (
                        "Исх. № ____от ________________  г.",
                        "Исх. № {{CLAIM_NUMBER}}    от {{CLAIM_DATE}}",
                    ),
                    (
                        "Вы являетесь собственником земельного участка № _____, кадастровый номер ___________________, расположенного по адресу: Новосибирская область, Искитимский муниципальный район, сельское поселение Быстровский сельсовет, с. Быстровка, микрорайон Экопарк, з/у № ______ и членом ТСН «Микрорайон Экопарк».",
                        "Вы являетесь собственником земельного участка № {{PLOT_NUMBER}}, кадастровый номер {{CADASTRAL_NUMBER}}, расположенного по адресу: {{PLOT_ADDRESS}}, и членом ТСН «Микрорайон Экопарк».",
                    ),
                    (
                        "В соответствии с Уставом ТСН члены общества обязаны своевременно уплачивать членские и целевые взносы, размеры которых устанавливается решением Общего собрания членов ТСН.",
                        "В соответствии с Уставом ТСН члены товарищества обязаны своевременно уплачивать членские и целевые взносы, размеры которых устанавливаются решением общего собрания членов ТСН.",
                    ),
                    (
                        "Протоколом общего собрания от_________ установлены членские взносы в размере 285 руб./сотка.  По данным бухгалтерского учета ТСН у Вас образовалась задолженность по членским взносам в размере _______________:",
                        "Протоколом общего собрания от {{CHARGE_BASIS}} установлены членские взносы в размере 285 руб./сотка. По данным бухгалтерского учета ТСН по лицевому счету {{ACCOUNT}} у Вас образовалась задолженность по членским взносам в размере {{PRINCIPAL_AMOUNT}}:",
                    ),
                    (
                        "Обязанность по внесению взносов распространяется на всех членов товарищества. Целевые взносы вносятся членами товарищества на расчетный счет товарищества по решению общего собрания членов товарищества, определяющему их размер и срок внесения, в порядке, установленном уставом товарищества. Размер взносов определяется на основании приходно-расходной сметы товарищества и финансово-экономического обоснования, утвержденных общим собранием членов товарищества.Уставом товарищества может быть установлен порядок взимания и размер пеней в случае несвоевременной уплаты взносов. В случае неуплаты взносов и пеней товарищество вправе взыскать их в судебном порядке.",
                        "Обязанность по внесению взносов распространяется на всех членов товарищества. Целевые взносы вносятся членами товарищества на расчетный счет товарищества по решению общего собрания членов товарищества, определяющему их размер и срок внесения, в порядке, установленном уставом товарищества. Размер взносов определяется на основании приходно-расходной сметы товарищества и финансово-экономического обоснования, утвержденных общим собранием членов товарищества. Уставом товарищества может быть установлен порядок взимания и размер пеней в случае несвоевременной уплаты взносов. В случае неуплаты взносов и пеней товарищество вправе взыскать их в судебном порядке.",
                    ),
                    (
                        "В соответствии с п.7.5 Уставом ТСН член товарищества обязан нести бремя содержания своего земельного участка; вносить обязательные платежи и членские и целевые взносы, а также другие платежи, предусмотренные законодательством и Уставом, в размерах и в сроки, определяемые законодательством и Общим собранием членов товарищества; выполнять решения общего собрания членов товарищества и решения правления.",
                        "В соответствии с п. 7.5 Устава ТСН член товарищества обязан нести бремя содержания своего земельного участка; вносить обязательные платежи, членские и целевые взносы, а также другие платежи, предусмотренные законодательством и Уставом, в размерах и сроки, определяемые законодательством и общим собранием членов товарищества; выполнять решения общего собрания членов товарищества и решения правления.",
                    ),
                    (
                        "В связи с этим, задолженность по членскому взносу в размере _________ руб. подлежит взысканию с должника в пользу ТСН.",
                        "В связи с этим задолженность по членскому взносу в размере {{PRINCIPAL_AMOUNT}} подлежит взысканию с должника в пользу ТСН.",
                    ),
                    (
                        "Таким образом, взнос установлен решением общего собрания, которое не отменено, недействительным не признано, в связи с чем, подлежит уплате всеми членами товарищества.",
                        "Таким образом, взнос установлен решением общего собрания, которое не отменено и не признано недействительным, в связи с чем подлежит уплате всеми членами товарищества.",
                    ),
                    (
                        "На основании вышеизложенного, прошу незамедлительно рассмотреть настоящую досудебную претензию и произвести погашение образовавшейся задолженности в размере _______ рублей в течение 10 календарных дней со дня получения настоящей претензии (если иной срок не установлен применимым законом или договором. Претензия считается врученной любым способом, позволяющим установить ее получение должником.",
                        "На основании вышеизложенного прошу незамедлительно рассмотреть настоящую досудебную претензию и погасить образовавшуюся задолженность в размере {{TOTAL_AMOUNT}} в течение 10 календарных дней со дня получения настоящей претензии (если иной срок не установлен применимым законом или договором). Претензия считается врученной любым способом, позволяющим установить ее получение должником.",
                    ),
                    (
                        "В случае удовлетворения требований ТСН о погашении задолженности, прошу направить в ТСН подтверждение оплаты по адресу электронной почты tsn-ecopark@mail.ru либо передать копию платежного документа председателю ТСН.",
                        "В случае удовлетворения требований ТСН о погашении задолженности прошу направить в ТСН подтверждение оплаты по адресу электронной почты {{TSN_EMAIL}} либо передать копию платежного документа председателю ТСН.",
                    ),
                    (
                        "Назначение платежа: «Погашение задолженности по претензии № ___ от _____ _______ г., участок № _____». ",
                        "Назначение платежа: «Погашение задолженности по претензии № {{CLAIM_NUMBER}} от {{CLAIM_DATE}}, участок № {{PLOT_NUMBER}}».",
                    ),
                )
                for source_text, replacement in replacements:
                    xml = replace_paragraph(xml, source_text, replacement)

                xml = remove_paragraph(xml, "ул. Карбышева, д. 16")
                xml = replace_table_value(xml, "Расчетная дата", "{{CALCULATION_DATE}}")
                xml = replace_table_value(xml, "Период задолженности", "{{DEBT_PERIOD}}")
                xml = replace_table_value(xml, "Основной долг", "{{PRINCIPAL_AMOUNT}}")
                xml = replace_table_value(xml, "Пени / проценты", "{{PENALTY_TEXT}}")
                xml = replace_table_value(xml, "ИТОГО К ОПЛАТЕ", "{{TOTAL_AMOUNT}}")

                for token in CLAIM_TOKENS:
                    if "{{" + token + "}}" not in xml:
                        fail(f"Missing token {{{{{token}}}}} in generated template")
                assert_remaining_blanks_are_manual(xml)
                if "з/у № 10\"" in xml:
                    fail("Hard-coded plot 10 address remains in generated template")
                payload = xml.encode("utf-8")

            elif info.filename == "word/settings.xml":
                settings = payload.decode("utf-8")
                if "<w:updateFields" not in settings:
                    settings = settings.replace(
                        "</w:settings>",
                        '<w:updateFields w:val="true"/></w:settings>',
                    )
                payload = settings.encode("utf-8")

            target.writestr(info, payload)


def main():
    if len(sys.argv) != 3:
        fail("Usage: build_claim_template.py SOURCE.docx OUTPUT.docx")
    build(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())


if __name__ == "__main__":
    main()
