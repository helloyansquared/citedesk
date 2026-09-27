import io
import zipfile

import httpx
import pytest

from citedesk.loaders import (
    LoaderError,
    iter_source_files,
    load_bytes,
    load_file,
    load_url,
    parse_csv,
    parse_docx,
    parse_html,
    parse_markdown,
)
from tests.helpers import SAMPLES


def test_every_sample_loads():
    names = [name for _, name in iter_source_files(SAMPLES)]
    assert names == [
        "faq.md",
        "loopline-city-2-manual.pdf",
        "rental-prices.csv",
        "repair-services.docx",
        "shipping-and-returns.html",
        "warranty-policy.pdf",
    ]
    for path, name in iter_source_files(SAMPLES):
        doc = load_file(path, name)
        assert doc.sections and doc.checksum and doc.path


def test_markdown_headings_front_matter_and_code_fences():
    doc = parse_markdown(
        "---\ntitle: Help centre\nurl: https://example.com/help\n---\n"
        "Intro text\n\n## Returns\nThirty days.\n### E-bikes\nUnder 50 km.\n"
        "```\n# not a heading\n```\n## Shipping\nFast.",
        "fallback",
    )
    assert doc.title == "Help centre"
    assert doc.url == "https://example.com/help"
    headings = [s.heading for s in doc.sections]
    assert headings == ["", "Returns", "Returns > E-bikes", "Shipping"]
    assert "# not a heading" in doc.sections[2].text


def test_markdown_h1_becomes_title():
    doc = parse_markdown("# FAQ\n\n## Hours\nTen to six.", "fallback")
    assert doc.title == "FAQ"
    assert doc.sections[0].heading == "Hours"


def test_html_keeps_main_content_tables_lists_and_skips_chrome():
    doc = load_file(SAMPLES / "shipping-and-returns.html")
    assert doc.title == "Shipping & returns"
    text = "\n".join(s.text for s in doc.sections)
    assert "Belgium, Luxembourg, Germany, Netherlands | 3–5 working days | €49 | €9.90" in text
    assert "- Gift cards" in text
    assert "Newsletter" not in text  # footer
    assert "Cart (0)" not in text  # nav
    assert "Returns > E-bikes" in [s.heading for s in doc.sections]


def test_html_without_main_and_with_canonical():
    html = (
        '<html><head><title>T</title><link rel="canonical" href="https://x.example/p"></head>'
        "<body><nav>menu</nav><h2>Part</h2><p>Hello&nbsp;there<br>line two</p><script>evil()</script></body></html>"
    )
    doc = parse_html(html, "fallback")
    assert doc.url == "https://x.example/p"
    assert doc.sections[0].heading == "Part"
    assert doc.sections[0].text == "Hello there\nline two"


def test_pdf_pages_headings_and_title():
    doc = load_file(SAMPLES / "loopline-city-2-manual.pdf")
    assert doc.title == "Loopline City 2: Owner's manual"
    by_heading = {s.heading: s for s in doc.sections}
    assert by_heading["3. Charging the battery"].page == 2
    assert "4.5 hours" in by_heading["3. Charging the battery"].text
    errors = by_heading["5. Error codes"].text
    assert "E07 Speed sensor signal lost" in errors
    # hard-wrapped PDF lines are joined back together
    assert "Sport and Boost" in by_heading["2. Display and assist levels"].text


def test_docx_headings_and_tables():
    doc = load_file(SAMPLES / "repair-services.docx")
    assert doc.title == "Loopline Workshop: repair services and prices"
    sections = {s.heading: s.text for s in doc.sections}
    assert "Full service | €149" in sections["Service packages"]
    assert "Puncture repair, inner tube included | €19" in sections["Common repairs"]


def _docx(body_xml: str) -> bytes:
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", f"<w:document {ns}><w:body>{body_xml}</w:body></w:document>")
    return buf.getvalue()


def test_docx_localised_heading_styles_and_lists():
    body = (
        '<w:p><w:pPr><w:pStyle w:val="Titre1"/></w:pPr><w:r><w:t>Garantie</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="Titre2"/></w:pPr><w:r><w:t>Batterie</w:t></w:r></w:p>'
        "<w:p><w:pPr><w:numPr/></w:pPr><w:r><w:t>Deux ans</w:t></w:r></w:p>"
    )
    doc = parse_docx(_docx(body), "fallback")
    assert doc.title == "Garantie"
    assert doc.sections[0].heading == "Batterie"
    assert doc.sections[0].text == "- Deux ans"


def test_bad_docx_raises():
    with pytest.raises(LoaderError):
        parse_docx(b"not a zip", "x")


def test_csv_rows_become_labelled_lines_and_semicolons_work():
    doc = parse_csv("Bike;Day;Week\nCity;€39;€189\nCargo;€65;\n", "Prices")
    assert doc.sections[0].text == "Bike: City; Day: €39; Week: €189\nBike: Cargo; Day: €65"


def test_empty_document_raises():
    with pytest.raises(LoaderError, match="No text"):
        load_bytes(b"   ", "empty.txt", suffix=".txt")


def test_unsupported_type_raises():
    with pytest.raises(LoaderError):
        load_bytes(b"x", "a.xyz", suffix=".xyz")


def test_windows_1252_text_is_decoded():
    doc = load_bytes("Café crème".encode("cp1252"), "menu.txt", suffix=".txt")
    assert doc.sections[0].text == "Café crème"


def test_load_url_html_and_pdf():
    pdf = (SAMPLES / "warranty-policy.pdf").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".pdf"):
            return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
        return httpx.Response(
            200, text="<title>Page</title><h2>Hi</h2><p>Text</p>", headers={"content-type": "text/html"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    page = load_url("https://x.example/help", client)
    assert page.url == "https://x.example/help" and page.source == "https://x.example/help"
    assert page.sections[0].text == "Text"
    assert load_url("https://x.example/w.pdf", client).title == "Loopline Warranty Policy"


def test_load_url_error():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(LoaderError, match="Could not fetch"):
        load_url("https://x.example/missing", client)


def test_missing_path_raises():
    with pytest.raises(LoaderError):
        iter_source_files("does/not/exist")
