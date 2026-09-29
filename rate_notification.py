import io
import re
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from xml.sax.saxutils import escape


RATE_USAGE = (
    "Формат: /rate <направление> <код> <цена USD> <тарификация> <техпрефикс>\n"
    "Пример: /rate Poland 48 0.09 1/1 103"
)


@dataclass(frozen=True)
class RateNotification:
    destination: str
    code: str
    rate: Decimal
    tarification: str
    tech_prefix: str

    @property
    def rate_text(self):
        value = format(self.rate, "f").rstrip("0").rstrip(".")
        return value or "0"


def is_rate_command(text):
    first = str(text or "").strip().split(maxsplit=1)[0].lower()
    return first == "rate" or first == "/rate" or first.startswith("/rate@")


def parse_rate_command(text):
    parts = str(text or "").strip().split()
    if not parts or not is_rate_command(parts[0]):
        raise ValueError(RATE_USAGE)
    if len(parts) < 6:
        raise ValueError(RATE_USAGE)

    destination = " ".join(parts[1:-4]).strip()
    code, rate_raw, tarification, tech_prefix = parts[-4:]
    if not destination or len(destination) > 80:
        raise ValueError("Направление не указано или слишком длинное.\n" + RATE_USAGE)
    if not re.fullmatch(r"\d{1,20}", code):
        raise ValueError("Код должен состоять из цифр, например 48.")
    try:
        rate = Decimal(rate_raw.replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError("Цена должна быть числом, например 0.09.") from exc
    if not rate.is_finite() or rate <= 0 or rate >= 100:
        raise ValueError("Цена должна быть больше 0 и меньше 100 USD.")
    match = re.fullmatch(r"(\d{1,5})/(\d{1,5})", tarification)
    if not match or int(match.group(1)) <= 0 or int(match.group(2)) <= 0:
        raise ValueError("Тарификация должна быть в формате 1/1 или 60/60.")
    if not re.fullmatch(r"\d{1,20}", tech_prefix):
        raise ValueError("Техпрефикс должен состоять из цифр, например 103.")

    return RateNotification(destination, code, rate, tarification, tech_prefix)


def rate_notification_filename(rate):
    destination = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._-]+", "_", rate.destination).strip("._-")
    destination = destination[:60] or "Destination"
    return f"RN_{destination}_Rate_{rate.tech_prefix}_{rate.rate_text}.xlsx"


def _text_cell(address, style, value):
    return (
        f'<c r="{address}" s="{style}" t="inlineStr">'
        f'<is><t xml:space="preserve">{escape(str(value))}</t></is></c>'
    )


def build_rate_notification_xlsx(rate):
    sheet = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetViews><sheetView showGridLines="0" workbookViewId="0"/></sheetViews>
  <sheetFormatPr defaultRowHeight="15"/>
  <cols>
    <col min="1" max="1" width="18" customWidth="1"/>
    <col min="2" max="2" width="10" customWidth="1"/>
    <col min="3" max="3" width="14" customWidth="1"/>
    <col min="4" max="4" width="15" customWidth="1"/>
    <col min="5" max="5" width="14" customWidth="1"/>
    <col min="6" max="6" width="13" customWidth="1"/>
  </cols>
  <sheetData>
    <row r="1" ht="22" customHeight="1">
      {_text_cell("A1", 1, "Destination")}
      {_text_cell("B1", 1, "Code")}
      {_text_cell("C1", 1, "Rate (USD)")}
      {_text_cell("D1", 1, "Tarification")}
      {_text_cell("E1", 1, "Tech Prefix")}
      {_text_cell("F1", 1, "Status")}
    </row>
    <row r="2" ht="22" customHeight="1">
      {_text_cell("A2", 2, rate.destination)}
      {_text_cell("B2", 3, rate.code)}
      <c r="C2" s="4" t="n"><v>{escape(rate.rate_text)}</v></c>
      {_text_cell("D2", 3, rate.tarification)}
      {_text_cell("E2", 3, rate.tech_prefix)}
      {_text_cell("F2", 3, "New Rate")}
    </row>
  </sheetData>
  <pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>
</worksheet>'''

    files = {
        "[Content_Types].xml": '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>''',
        "_rels/.rels": '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>''',
        "xl/workbook.xml": '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Rate Notification" sheetId="1" r:id="rId1"/></sheets>
</workbook>''',
        "xl/_rels/workbook.xml.rels": '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>''',
        "xl/styles.xml": '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <numFmts count="1"><numFmt numFmtId="200" formatCode="0.000"/></numFmts>
  <fonts count="2">
    <font><sz val="11"/><name val="Carlito"/></font>
    <font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Carlito"/></font>
  </fonts>
  <fills count="3">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border/>
    <border><left style="thin"><color rgb="FFD9E2F3"/></left><right style="thin"><color rgb="FFD9E2F3"/></right><top style="thin"><color rgb="FFD9E2F3"/></top><bottom style="thin"><color rgb="FFD9E2F3"/></bottom><diagonal/></border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="5">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="200" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>''',
        "xl/worksheets/sheet1.xml": sheet,
    }

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content.encode("utf-8"))
    return output.getvalue()
