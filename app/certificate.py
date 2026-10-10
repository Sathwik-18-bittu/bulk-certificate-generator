"""Certificate rendering (single predefined template, A4 landscape PDF)."""
import os
from datetime import date

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

NAVY = HexColor("#1b2a49")
GOLD = HexColor("#b08d3c")
GREY = HexColor("#555555")


def _fit_font_size(text, font, max_size, max_width, min_size=12):
    """Shrink the font until `text` fits in `max_width` (long names)."""
    size = max_size
    while size > min_size and stringWidth(text, font, size) > max_width:
        size -= 1
    return size


def _centered(c, y, text, font, size, color):
    c.setFont(font, size)
    c.setFillColor(color)
    c.drawCentredString(c.width_ / 2, y, text)


def render_certificate(path, *, name, achievement, event_name, title, issuer,
                       issue_date, certificate_number):
    """Render one certificate PDF to `path`.

    Text is drawn with drawString (not Platypus Paragraph) so user input is never
    interpreted as markup. The file is written to a temp name and renamed, so a
    crash can never leave a half-written PDF at the final path.
    """
    width, height = landscape(A4)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".part"

    # pageCompression=0: files are only a few KB and stay greppable/inspectable in tests
    c = canvas.Canvas(tmp_path, pagesize=(width, height), pageCompression=0)
    c.width_ = width
    c.setTitle(f"{title} - {name}")
    c.setAuthor(issuer)

    # double border
    c.setStrokeColor(NAVY)
    c.setLineWidth(6)
    c.rect(24, 24, width - 48, height - 48)
    c.setStrokeColor(GOLD)
    c.setLineWidth(2)
    c.rect(36, 36, width - 72, height - 72)

    inner = width - 160
    _centered(c, height - 130, title.upper(),
              "Helvetica-Bold", _fit_font_size(title.upper(), "Helvetica-Bold", 34, inner), NAVY)
    c.setStrokeColor(GOLD)
    c.line(width / 2 - 90, height - 148, width / 2 + 90, height - 148)

    _centered(c, height - 195, "This is to certify that", "Helvetica-Oblique", 16, GREY)
    _centered(c, height - 255, name,
              "Times-BoldItalic", _fit_font_size(name, "Times-BoldItalic", 44, inner), NAVY)
    c.setStrokeColor(NAVY)
    c.setLineWidth(0.8)
    c.line(width / 2 - 220, height - 268, width / 2 + 220, height - 268)

    line = achievement or "has successfully completed"
    _centered(c, height - 305, line, "Helvetica", _fit_font_size(line, "Helvetica", 16, inner, 10), GREY)
    _centered(c, height - 345, event_name, "Helvetica-Bold",
              _fit_font_size(event_name, "Helvetica-Bold", 22, inner), NAVY)

    try:
        pretty_date = date.fromisoformat(issue_date).strftime("%d %B %Y")
    except ValueError:
        pretty_date = issue_date
    c.setFont("Helvetica", 12)
    c.setFillColor(GREY)
    c.drawString(90, 90, f"Date: {pretty_date}")
    c.drawRightString(width - 90, 90, f"Issued by: {issuer}")
    _centered(c, 58, f"Certificate No. {certificate_number}", "Helvetica", 9, GREY)

    c.showPage()
    c.save()
    os.replace(tmp_path, path)
    return path
