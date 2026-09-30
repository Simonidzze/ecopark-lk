from html import unescape
from pathlib import Path
import re


CLAIM_SIGNATURE_TEXT = "___________________ / Дорогин Артем Владимирович /М.П."
CLAIM_SIGNATURE_RELATIONSHIP_ID = "rIdClaimSignature"
CLAIM_STAMP_RELATIONSHIP_ID = "rIdClaimStamp"
CLAIM_SIGNATURE_MEDIA_PATH = "word/media/claim_signature.png"
CLAIM_STAMP_MEDIA_PATH = "word/media/claim_stamp.png"
PARAGRAPH_PATTERN = re.compile(r"<w:p\b[\s\S]*?</w:p>")


def claim_asset_path(filename):
    return (
        Path(__file__).resolve().parent.parent
        / "templates"
        / "documents"
        / "assets"
        / filename
    )


def load_claim_image_parts():
    parts = {
        CLAIM_SIGNATURE_MEDIA_PATH: claim_asset_path("claim_signature.png").read_bytes(),
        CLAIM_STAMP_MEDIA_PATH: claim_asset_path("claim_stamp.png").read_bytes(),
    }
    for filename, payload in parts.items():
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError(f"Файл {filename} должен быть PNG")
    return parts


def visible_paragraph_text(paragraph_xml):
    return unescape(
        "".join(re.findall(r"<w:t\b[^>]*>([\s\S]*?)</w:t>", paragraph_xml))
    )


def inline_image_run(
    relationship_id,
    name,
    description,
    width_emu,
    height_emu,
    drawing_id,
):
    return f"""<w:r><w:rPr><w:noProof/></w:rPr><w:drawing>
<wp:inline distT="0" distB="0" distL="0" distR="0">
<wp:extent cx="{width_emu}" cy="{height_emu}"/>
<wp:effectExtent l="0" t="0" r="0" b="0"/>
<wp:docPr id="{drawing_id}" name="{name}" descr="{description}"/>
<wp:cNvGraphicFramePr><a:graphicFrameLocks xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" noChangeAspect="1"/></wp:cNvGraphicFramePr>
<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
<pic:nvPicPr><pic:cNvPr id="{drawing_id}" name="{name}" descr="{description}"/><pic:cNvPicPr/></pic:nvPicPr>
<pic:blipFill><a:blip r:embed="{relationship_id}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>
<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{width_emu}" cy="{height_emu}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>
</pic:pic></a:graphicData></a:graphic>
</wp:inline></w:drawing></w:r>"""


def signature_block_xml():
    signature = inline_image_run(
        CLAIM_SIGNATURE_RELATIONSHIP_ID,
        "Подпись председателя",
        "Подпись председателя правления ТСН",
        1_440_000,
        919_589,
        101,
    )
    stamp = inline_image_run(
        CLAIM_STAMP_RELATIONSHIP_ID,
        "Печать ТСН",
        "Печать ТСН Микрорайон Экопарк",
        1_152_000,
        1_159_481,
        102,
    )
    return f"""<w:tbl><w:tblPr>
<w:tblW w:w="9200" w:type="dxa"/><w:jc w:val="center"/><w:tblLayout w:type="fixed"/>
<w:tblBorders><w:top w:val="nil"/><w:left w:val="nil"/><w:bottom w:val="nil"/><w:right w:val="nil"/><w:insideH w:val="nil"/><w:insideV w:val="nil"/></w:tblBorders>
<w:tblCellMar><w:top w:w="0" w:type="dxa"/><w:left w:w="0" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/><w:right w:w="0" w:type="dxa"/></w:tblCellMar>
</w:tblPr><w:tblGrid><w:gridCol w:w="2700"/><w:gridCol w:w="4300"/><w:gridCol w:w="2200"/></w:tblGrid><w:tr>
<w:tc><w:tcPr><w:tcW w:w="2700" w:type="dxa"/><w:vAlign w:val="center"/></w:tcPr><w:p><w:pPr><w:spacing w:before="0" w:after="0"/><w:jc w:val="center"/></w:pPr>{signature}</w:p></w:tc>
<w:tc><w:tcPr><w:tcW w:w="4300" w:type="dxa"/><w:vAlign w:val="center"/></w:tcPr><w:p><w:pPr><w:spacing w:before="0" w:after="0"/><w:jc w:val="center"/></w:pPr><w:r><w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman"/><w:sz w:val="23"/><w:lang w:val="ru-RU"/></w:rPr><w:t>/ Дорогин Артем Владимирович /</w:t></w:r></w:p></w:tc>
<w:tc><w:tcPr><w:tcW w:w="2200" w:type="dxa"/><w:vAlign w:val="center"/></w:tcPr><w:p><w:pPr><w:spacing w:before="0" w:after="0"/><w:jc w:val="center"/></w:pPr>{stamp}</w:p></w:tc>
</w:tr></w:tbl>"""


def insert_signature_block(document_xml):
    replacements = 0

    def replace_signature(match):
        nonlocal replacements
        paragraph_xml = match.group(0)
        if visible_paragraph_text(paragraph_xml).strip() != CLAIM_SIGNATURE_TEXT:
            return paragraph_xml
        replacements += 1
        return signature_block_xml()

    document_xml = PARAGRAPH_PATTERN.sub(replace_signature, document_xml)
    if replacements != 1:
        raise ValueError(
            f"В DOCX-шаблоне ожидался один блок подписи, найдено: {replacements}"
        )
    return document_xml


def add_image_relationships(relationships_xml):
    for relationship_id in (
        CLAIM_SIGNATURE_RELATIONSHIP_ID,
        CLAIM_STAMP_RELATIONSHIP_ID,
    ):
        if f'Id="{relationship_id}"' in relationships_xml:
            raise ValueError(f"В DOCX-шаблоне уже используется связь {relationship_id}")
    relationships = (
        f'<Relationship Id="{CLAIM_SIGNATURE_RELATIONSHIP_ID}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="media/claim_signature.png"/>'
        f'<Relationship Id="{CLAIM_STAMP_RELATIONSHIP_ID}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="media/claim_stamp.png"/>'
    )
    if relationships_xml.count("</Relationships>") != 1:
        raise ValueError("Некорректный файл связей DOCX-шаблона")
    return relationships_xml.replace(
        "</Relationships>", relationships + "</Relationships>"
    )


def add_png_content_type(content_types_xml):
    if re.search(
        r'<Default\b[^>]*Extension="png"', content_types_xml, re.IGNORECASE
    ):
        return content_types_xml
    if content_types_xml.count("</Types>") != 1:
        raise ValueError("Некорректный файл типов содержимого DOCX-шаблона")
    png_type = '<Default Extension="png" ContentType="image/png"/>'
    return content_types_xml.replace("</Types>", png_type + "</Types>")


def patch_claim_image_part(filename, payload):
    if filename == "word/document.xml":
        return insert_signature_block(payload.decode("utf-8")).encode("utf-8")
    if filename == "word/_rels/document.xml.rels":
        return add_image_relationships(payload.decode("utf-8")).encode("utf-8")
    if filename == "[Content_Types].xml":
        return add_png_content_type(payload.decode("utf-8")).encode("utf-8")
    return payload
