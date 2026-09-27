"""Generate the binary sample documents of the fictional Loopline E-Bikes knowledge base.

    pip install reportlab python-docx
    python scripts/make_samples.py

Writes samples/loopline/warranty-policy.pdf, loopline-city-2-manual.pdf and repair-services.docx.
The Markdown, HTML and CSV samples are hand-written files in the same folder.
"""

from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "samples" / "loopline"
FICTIONAL = "Loopline E-Bikes is a fictional shop. This document exists to demo CiteDesk."

WARRANTY = [
    ("h", "1. Overview"),
    (
        "p",
        "This warranty covers bikes bought new from Loopline E-Bikes, in store or online. It starts on the "
        "delivery date. To make a claim you need your proof of purchase and the frame number, which is "
        "engraved under the bottom bracket.",
    ),
    ("h", "2. Frame"),
    (
        "p",
        "Frames are covered for 10 years against manufacturing defects such as cracks in welds. Warranties are "
        "for the original owner and are not transferable, with one exception: the frame warranty passes to a "
        "second owner for the remaining period if the original receipt is provided.",
    ),
    ("h", "3. Motor and battery"),
    (
        "p",
        "The motor and the battery are covered for 2 years. The battery is also considered defective, and is "
        "replaced, if its capacity drops below 70% of the original within 2 years or 800 full charge cycles, "
        "whichever comes first. A gradual capacity loss that stays above 70% is normal ageing and is not a "
        "defect.",
    ),
    ("break", ""),
    ("h", "4. Other components"),
    (
        "p",
        "The display, controller, wiring and integrated lights are covered for 2 years. Wear parts (tyres, "
        "inner tubes, brake pads, chain, cassette and chainrings) are only covered against manufacturing "
        "defects that appear in the first 6 months.",
    ),
    ("h", "5. What is not covered"),
    ("li", "Damage from accidents, falls, misuse, racing or jumps."),
    ("li", "Bikes fitted with a speed tuning kit or other unauthorised modifications: the whole warranty is void."),
    ("li", "Water damage from immersion or cleaning with a pressure washer."),
    ("li", "Damage caused by chargers other than the original Loopline charger."),
    ("li", "Bikes used in commercial rental fleets, which have a separate 1-year warranty."),
    ("h", "6. How to make a claim"),
    (
        "p",
        "Contact support with your frame number, proof of purchase and photos or a short video of the problem. "
        "Bring the bike to the Hilltop workshop or ship it to us. We diagnose it within 5 working days and then "
        "repair or replace the faulty part. If a warranty repair takes longer than 7 days, we lend you a bike "
        "while you wait, subject to availability.",
    ),
]

MANUAL = [
    ("h", "1. Specifications"),
    (
        "table",
        [
            ["Motor", "250 W rear hub motor, 45 Nm torque"],
            ["Assistance", "up to 25 km/h (EU limit)"],
            ["Battery", "36 V, 14 Ah (504 Wh), removable and lockable"],
            ["Range", "50 to 110 km, depending on assist level, load, terrain and temperature"],
            ["Weight", "23.5 kg including battery"],
            ["Maximum total load", "130 kg (rider, luggage and bike)"],
            ["Charger", "4 A"],
        ],
    ),
    ("h", "2. Display and assist levels"),
    (
        "p",
        "Press the power button for 2 seconds to switch the bike on. There are four assist levels: Eco, Tour, "
        "Sport and Boost; change them with the plus and minus buttons. Hold the minus button for 3 seconds to "
        "use walk assist (up to 6 km/h) while pushing the bike. Hold the plus button for 2 seconds to switch "
        "the lights on or off.",
    ),
    ("break", ""),
    ("h", "3. Charging the battery"),
    (
        "p",
        "The battery can be charged on or off the bike. With the supplied 4 A charger, charging from empty to "
        "80% takes about 2.5 hours and a full charge from empty takes about 4.5 hours. The charger LED is red "
        "while charging and turns green when the battery is full.",
    ),
    (
        "p",
        "Charge indoors at a room temperature between 10 °C and 30 °C. Only use the original Loopline charger.",
    ),
    ("h", "4. Battery care and winter storage"),
    (
        "p",
        "If you will not ride for more than a month, for example over winter, store the battery off the bike at "
        "30 to 60% charge in a dry place between 10 °C and 20 °C. Check the charge level every 2 months and "
        "top it up to about 50%. Never store the battery fully empty or below 0 °C, as this damages the cells.",
    ),
    ("break", ""),
    ("h", "5. Error codes"),
    (
        "p",
        "If the display shows an error code, switch the bike off and on again first. If the code comes back, "
        "follow the table below.",
    ),
    (
        "table",
        [
            ["Code", "Meaning", "What to do"],
            ["E01", "Battery voltage low", "Charge the battery."],
            ["E02", "Battery temperature out of range", "Let the battery return to 0-40 °C before riding."],
            ["E03", "Motor overheating", "Stop and let the motor cool down for 15 minutes."],
            ["E04", "Display to controller communication error", "Check the display cable connector."],
            ["E05", "Brake sensor stuck", "Check that both brake levers return fully."],
            ["E06", "Motor phase error", "Stop riding and contact the workshop."],
            [
                "E07",
                "Speed sensor signal lost",
                "Check that the magnet on the rear wheel spoke is aligned and within 5 mm of the sensor.",
            ],
            ["E08", "Display error", "Switch off, remove and refit the battery, then switch on."],
            ["E09", "Controller over-current", "Reduce the load. Contact the workshop if it persists."],
            ["E10", "Firmware mismatch", "Visit a store for a free firmware update."],
        ],
    ),
    ("h", "6. Maintenance"),
    (
        "p",
        "Your first service is free after 300 km or 3 months, whichever comes first. After that, have the bike "
        "serviced every 2,000 km or once a year. Keep the tyres at 3.5 to 4.5 bar (50 to 65 psi). Lubricate the "
        "chain every 300 km and after riding in the rain. Check the brake pads once a month.",
    ),
]


def build_pdf(path: Path, title: str, blocks: list) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import ListFlowable, ListItem, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table

    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        title=title,
        author="Loopline E-Bikes (fictional)",
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )
    story = [Paragraph(title, styles["Title"]), Paragraph(f"<i>{FICTIONAL}</i>", styles["Normal"]), Spacer(1, 8)]
    bullets: list = []

    def flush_bullets() -> None:
        if bullets:
            story.append(ListFlowable([ListItem(b) for b in bullets], bulletType="bullet", leftIndent=12))
            bullets.clear()

    for kind, content in blocks:
        if kind != "li":
            flush_bullets()
        if kind == "h":
            story.append(Paragraph(content, styles["Heading2"]))
        elif kind == "p":
            story.append(Paragraph(content, styles["BodyText"]))
        elif kind == "li":
            bullets.append(Paragraph(content, styles["BodyText"]))
        elif kind == "table":
            cells = [[Paragraph(c, styles["BodyText"]) for c in row] for row in content]
            widths = [18 * mm, 60 * mm, 92 * mm] if len(content[0]) == 3 else [45 * mm, 125 * mm]
            table = Table(cells, colWidths=widths)
            table.setStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP")])
            story.append(table)
        elif kind == "break":
            story.append(PageBreak())
    flush_bullets()
    doc.build(story)


def build_docx(path: Path) -> None:
    from docx import Document

    doc = Document()
    doc.core_properties.title = "Loopline Workshop: repair services and prices"
    doc.add_heading("Loopline Workshop: repair services and prices", level=0)
    doc.add_paragraph(FICTIONAL)

    doc.add_heading("Service packages", level=1)
    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    for cell, text in zip(table.rows[0].cells, ["Service", "Price", "What is included", "Turnaround"], strict=True):
        cell.text = text
    for row in [
        [
            "Basic service",
            "€69",
            "Brakes and gears adjusted, tyre pressure, bolt torque check, software update",
            "Same day if dropped off before 10:00",
        ],
        [
            "Full service",
            "€149",
            "Basic service plus wheel truing, bearing check, labour for chain and brake pad replacement, "
            "battery health report",
            "2 working days",
        ],
        ["Battery health check", "€25 (free with a full service)", "Capacity test with a printed report", "1 hour"],
    ]:
        cells = table.add_row().cells
        for cell, text in zip(cells, row, strict=True):
            cell.text = text

    doc.add_heading("Common repairs", level=1)
    table = doc.add_table(rows=1, cols=2)
    table.style = "Table Grid"
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = "Repair", "Price"
    for repair, price in [
        ("Puncture repair, inner tube included", "€19 (€25 for a rear hub-motor wheel)"),
        ("Brake pad replacement, per wheel, parts included", "€29"),
        ("Hydraulic brake bleed, per brake", "€45"),
        ("Chain replacement (labour; the chain is extra)", "€20"),
        ("Spoke replacement", "€15"),
        ("Motor or battery diagnosis", "€40, deducted from the repair if you go ahead"),
    ]:
        cells = table.add_row().cells
        cells[0].text, cells[1].text = repair, price

    doc.add_heading("Booking and turnaround", level=1)
    doc.add_paragraph(
        "Book online or at the Hilltop workshop. Drop your bike off before 10:00 for same-day work. We always "
        "call you with a quote before doing any work over €100."
    )
    doc.add_paragraph(
        "Please collect your bike within 7 days of our pick-up message; after that we charge a storage fee of "
        "€5 per day."
    )
    doc.add_paragraph(
        "Mobile repairs: within 15 km of the workshop a mechanic can come to you, for a call-out fee of €35 plus "
        "the usual repair price."
    )

    doc.add_heading("Bikes we service", level=1)
    doc.add_paragraph(
        "We service and repair e-bikes and regular bikes of all brands, including Bosch, Shimano and Yamaha "
        "motor diagnostics."
    )
    doc.save(str(path))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    build_pdf(OUT / "warranty-policy.pdf", "Loopline Warranty Policy", WARRANTY)
    build_pdf(OUT / "loopline-city-2-manual.pdf", "Loopline City 2: Owner's manual", MANUAL)
    build_docx(OUT / "repair-services.docx")
    for name in ("warranty-policy.pdf", "loopline-city-2-manual.pdf", "repair-services.docx"):
        print(f"wrote {OUT / name}")


if __name__ == "__main__":
    main()
