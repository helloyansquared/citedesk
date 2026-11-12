"""Turn files and web pages into Documents made of titled sections.

Supported: Markdown, plain text, HTML (files or URLs), PDF, Word (.docx) and CSV. Only the
standard library plus pypdfium2 is used, so it installs anywhere.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

import httpx

from citedesk.schema import Document, Section
from citedesk.text import clean_whitespace

SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt", ".html", ".htm", ".pdf", ".docx", ".csv"}
USER_AGENT = "CiteDesk/0.1 (+https://github.com/helloyansquared/citedesk)"


class LoaderError(RuntimeError):
    pass


def is_url(source: str) -> bool:
    return source.startswith(("http://", "https://"))


def iter_source_files(source: str | Path) -> list[tuple[Path, str]]:
    """Expand a file or folder into (path, display name) pairs. Names are relative to the folder."""
    path = Path(source)
    if path.is_file():
        return [(path, path.name)]
    if path.is_dir():
        files = sorted(
            p
            for p in path.rglob("*")
            if p.is_file()
            and p.suffix.lower() in SUPPORTED_SUFFIXES
            and not any(part.startswith((".", "_")) for part in p.relative_to(path).parts)
        )
        return [(p, p.relative_to(path).as_posix()) for p in files]
    raise LoaderError(f"Not found: {source}")


def load_file(path: str | Path, name: str | None = None) -> Document:
    path = Path(path)
    data = path.read_bytes()
    return load_bytes(data, name or path.name, suffix=path.suffix.lower(), path=str(path.resolve()))


def load_bytes(data: bytes, name: str, *, suffix: str, url: str | None = None, path: str | None = None) -> Document:
    checksum = hashlib.sha256(data).hexdigest()[:16]
    fallback_title = _title_from_name(name)
    if suffix in (".md", ".markdown"):
        doc = parse_markdown(_decode(data), fallback_title)
    elif suffix == ".txt":
        doc = Document(source="", title=fallback_title, sections=[Section("", clean_whitespace(_decode(data)))])
    elif suffix in (".html", ".htm"):
        doc = parse_html(_decode(data), fallback_title)
    elif suffix == ".pdf":
        doc = parse_pdf(data, fallback_title)
    elif suffix == ".docx":
        doc = parse_docx(data, fallback_title)
    elif suffix == ".csv":
        doc = parse_csv(_decode(data), fallback_title)
    else:
        raise LoaderError(f"Unsupported file type: {name}")
    doc.source = name
    doc.checksum = checksum
    doc.url = doc.url or url
    doc.path = path
    doc.sections = [s for s in doc.sections if s.text.strip()]
    if not doc.sections:
        raise LoaderError(f"No text found in {name} (a scanned PDF needs OCR first)")
    return doc


def load_url(url: str, client: httpx.Client | None = None, timeout: float = 20.0) -> Document:
    own = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    try:
        response = client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise LoaderError(f"Could not fetch {url}: {exc}") from exc
    finally:
        if own:
            client.close()
    ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
    path_part = url.split("?")[0].split("#")[0].lower()
    if ctype == "application/pdf" or path_part.endswith(".pdf"):
        suffix = ".pdf"
    elif ctype in ("text/markdown", "text/x-markdown") or path_part.endswith((".md", ".markdown")):
        suffix = ".md"
    elif ctype == "text/plain":
        suffix = ".txt"
    elif ctype == "text/csv" or path_part.endswith(".csv"):
        suffix = ".csv"
    else:
        suffix = ".html"
    return load_bytes(response.content, url, suffix=suffix, url=str(response.url))


# --------------------------------------------------------------------------- markdown

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_markdown(text: str, fallback_title: str) -> Document:
    text = text.replace("\r\n", "\n")
    title, url = None, None
    front = _FRONT_MATTER.match(text)
    if front:
        for line in front.group(1).splitlines():
            key, _, value = line.partition(":")
            value = value.strip().strip("\"'")
            if key.strip().lower() == "title" and value:
                title = value
            elif key.strip().lower() == "url" and value:
                url = value
        text = text[front.end() :]

    builder = _SectionBuilder()
    in_fence = False
    for line in text.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        match = None if in_fence else _MD_HEADING.match(line)
        if match:
            level, heading = len(match.group(1)), match.group(2).strip()
            if level == 1 and title is None:
                title = heading
                builder.start([])
                continue
            builder.heading(level, heading)
        else:
            builder.add(line)
    doc = Document(source="", title=title or fallback_title, sections=builder.finish())
    doc.url = url
    return doc


class _SectionBuilder:
    """Collects lines under a stack of headings ("Returns > E-bikes")."""

    def __init__(self) -> None:
        self.stack: list[tuple[int, str]] = []
        self.lines: list[str] = []
        self.sections: list[Section] = []
        self.page: int | None = None

    def start(self, stack: list[tuple[int, str]]) -> None:
        self._flush()
        self.stack = stack

    def heading(self, level: int, text: str) -> None:
        self._flush()
        self.stack = [(lvl, h) for lvl, h in self.stack if lvl < level] + [(level, text)]

    def add(self, line: str) -> None:
        self.lines.append(line)

    def _flush(self) -> None:
        text = clean_whitespace("\n".join(self.lines))
        if text:
            self.sections.append(Section(" > ".join(h for _, h in self.stack), text, self.page))
        self.lines = []

    def finish(self) -> list[Section]:
        self._flush()
        return self.sections


# --------------------------------------------------------------------------- html

_SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe", "form", "button", "select", "nav"}
_CHROME_TAGS = {"header", "footer", "aside"}  # skipped unless they sit inside <main>/<article>
_BLOCK_TAGS = {
    "p", "div", "section", "article", "main", "ul", "ol", "table", "thead", "tbody", "tr", "blockquote",
    "pre", "dl", "dt", "dd", "figure", "figcaption", "details", "summary", "address",
}  # fmt: skip
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_VOID = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}


class _HTMLTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.open: list[str] = []
        self.skip_depth = 0
        self.title = ""
        self.canonical: str | None = None
        self.in_title = False
        self.heading_level: int | None = None
        self.heading_text: list[str] = []
        # (kind, payload, in_main): kind is "text" or "heading"
        self.events: list[tuple[str, object, bool]] = []
        self.cell_index = 0
        self.saw_main = False

    @property
    def in_main(self) -> bool:
        return "main" in self.open or "article" in self.open

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "link" and (attr.get("rel") or "").lower() == "canonical" and attr.get("href"):
            self.canonical = attr["href"]
        if tag in _VOID:
            if tag == "br" and not self.skip_depth:
                self._text("\n")
            return
        self.open.append(tag)
        if tag in ("main", "article"):
            self.saw_main = True
        if self.skip_depth or tag in _SKIP_TAGS or (tag in _CHROME_TAGS and not self.in_main):
            self.skip_depth += 1
            return
        if tag == "title":
            self.in_title = True
        elif tag in _HEADINGS:
            self.heading_level = _HEADINGS[tag]
            self.heading_text = []
        elif tag == "li":
            self._text("\n- ")
        elif tag == "tr":
            self.cell_index = 0
            self._text("\n")
        elif tag in ("td", "th"):
            if self.cell_index:
                self._text(" | ")
            self.cell_index += 1
        elif tag in _BLOCK_TAGS:
            self._text("\n\n" if tag == "p" else "\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID or tag not in self.open:
            return
        # pop up to and including the matching tag (tolerates unclosed children)
        while self.open:
            top = self.open.pop()
            if self.skip_depth:
                self.skip_depth -= 1
                if top == tag:
                    return
                continue
            if top == "title":
                self.in_title = False
            elif top in _HEADINGS and self.heading_level is not None:
                text = clean_whitespace(" ".join(self.heading_text)).replace("\n", " ")
                if text:
                    self.events.append(("heading", (self.heading_level, text), self.in_main or top == "main"))
                self.heading_level = None
            elif top in _BLOCK_TAGS:
                self._text("\n")
            if top == tag:
                return

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
            return
        if self.skip_depth:
            return
        if "pre" not in self.open:
            data = re.sub(r"\s+", " ", data)  # HTML collapses whitespace; line breaks come from tags
        if self.heading_level is not None:
            self.heading_text.append(data)
        else:
            self._text(data)

    def _text(self, text: str) -> None:
        self.events.append(("text", text, self.in_main))


def parse_html(html: str, fallback_title: str) -> Document:
    parser = _HTMLTextParser()
    parser.feed(html)
    parser.close()
    events = [e for e in parser.events if e[2]] if parser.saw_main else parser.events

    builder = _SectionBuilder()
    title = clean_whitespace(parser.title).split("\n")[0] or None
    h1_seen = False
    buffer: list[str] = []
    for kind, payload, _ in events:
        if kind == "text":
            buffer.append(str(payload))
            continue
        builder.add("".join(buffer))
        buffer = []
        level, text = payload  # type: ignore[misc]
        if level == 1 and not h1_seen:
            h1_seen = True
            title = text
            builder.start([])
        else:
            builder.heading(level, text)
    builder.add("".join(buffer))
    sections = [Section(s.heading, _tidy_lines(s.text), s.page) for s in builder.finish()]
    doc = Document(source="", title=title or fallback_title, sections=sections)
    doc.url = parser.canonical
    return doc


def _tidy_lines(text: str) -> str:
    lines = [line.strip() for line in text.split("\n")]
    lines = [line for line in lines if line not in ("-", "|")]
    return clean_whitespace("\n".join(lines))


# --------------------------------------------------------------------------- pdf

# "4. Error codes", "4.2 Storing the battery", "SECTION 3 — Returns"
_PDF_HEADING = re.compile(r"^(\d{1,2}(\.\d{1,2})*\.?|section \d+[:.—-]?)\s+\S.{0,70}$", re.IGNORECASE)


def parse_pdf(data: bytes, fallback_title: str) -> Document:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise LoaderError(f"Could not open PDF: {exc}") from exc
    try:
        meta_title = (pdf.get_metadata_dict().get("Title") or "").strip()
        builder = _SectionBuilder()
        for index in range(len(pdf)):
            page = pdf[index]
            textpage = page.get_textpage()
            text = textpage.get_text_bounded()
            textpage.close()
            page.close()
            builder.start(builder.stack)  # new page -> new section, same heading
            builder.page = index + 1
            for line in _reflow(clean_whitespace(text).split("\n")):
                stripped = line.strip()
                if _looks_like_pdf_heading(stripped):
                    level = stripped.split()[0].rstrip(".").count(".") + 2
                    builder.heading(level, stripped)
                else:
                    builder.add(line)
        sections = builder.finish()
    finally:
        pdf.close()
    title = meta_title or _first_line_title(sections) or fallback_title
    return Document(source="", title=title, sections=sections)


def _reflow(lines: list[str]) -> list[str]:
    """Undo the PDF's hard line wrapping: a line starting in lower case continues the previous one."""
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if out and stripped[:1].islower() and out[-1] and not out[-1].endswith((".", ":", "!", "?")):
            out[-1] = f"{out[-1]} {stripped}"
        else:
            out.append(stripped)
    return out


def _looks_like_pdf_heading(line: str) -> bool:
    return bool(_PDF_HEADING.match(line)) and not line.endswith((".", ",", ";", ":")) and len(line) <= 80


def _first_line_title(sections: list[Section]) -> str | None:
    if not sections:
        return None
    first = sections[0].text.split("\n", 1)[0].strip()
    return first if 3 <= len(first) <= 80 else None


# --------------------------------------------------------------------------- docx

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def parse_docx(data: bytes, fallback_title: str) -> Document:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            root = ElementTree.fromstring(zf.read("word/document.xml"))
            core_title = _docx_core_title(zf)
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise LoaderError(f"Not a valid .docx file: {exc}") from exc

    body = root.find(f"{_W}body")
    builder = _SectionBuilder()
    title = core_title
    for element in list(body) if body is not None else []:
        if element.tag == f"{_W}p":
            text = _docx_paragraph_text(element)
            if not text.strip():
                builder.add("")
                continue
            level = _docx_heading_level(element)
            if level == 0 or (level == 1 and title is None):
                title = title or text.strip()
                builder.start([])
            elif level:
                builder.heading(level, text.strip())
            else:
                is_list = element.find(f"{_W}pPr/{_W}numPr") is not None
                builder.add(("- " if is_list else "") + text)
        elif element.tag == f"{_W}tbl":
            for row in element.iter(f"{_W}tr"):
                cells = [clean_whitespace(_docx_paragraphs_text(cell)) for cell in row.iter(f"{_W}tc")]
                builder.add(" | ".join(cells))
            builder.add("")
    return Document(source="", title=title or fallback_title, sections=builder.finish())


def _docx_core_title(zf: zipfile.ZipFile) -> str | None:
    try:
        core = ElementTree.fromstring(zf.read("docProps/core.xml"))
    except (KeyError, ElementTree.ParseError):
        return None
    node = core.find("{http://purl.org/dc/elements/1.1/}title")
    return node.text.strip() if node is not None and node.text and node.text.strip() else None


def _docx_heading_level(paragraph: ElementTree.Element) -> int | None:
    """0 for the Title style, 1-6 for Heading 1-6 (also localised ids like 'Titre1'), else None."""
    style = paragraph.find(f"{_W}pPr/{_W}pStyle")
    if style is None:
        return None
    value = (style.get(f"{_W}val") or "").replace(" ", "").lower()
    if value in ("title", "titre"):
        return 0
    match = re.match(r"^(heading|titre|berschrift|kop|titolo|encabezado|标题)(\d)$", value)
    return int(match.group(2)) if match else None


def _docx_paragraph_text(paragraph: ElementTree.Element) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        if node.tag == f"{_W}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{_W}tab":
            parts.append("\t")
        elif node.tag in (f"{_W}br", f"{_W}cr"):
            parts.append("\n")
    return "".join(parts)


def _docx_paragraphs_text(element: ElementTree.Element) -> str:
    return "\n".join(_docx_paragraph_text(p) for p in element.iter(f"{_W}p"))


# --------------------------------------------------------------------------- csv


def parse_csv(text: str, fallback_title: str) -> Document:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [row for row in csv.reader(io.StringIO(text), dialect) if any(cell.strip() for cell in row)]
    if not rows:
        return Document(source="", title=fallback_title, sections=[])
    header, body = [h.strip() for h in rows[0]], rows[1:]
    lines = []
    for row in body:
        cells = zip(header, row, strict=False)  # short rows just have fewer cells
        pairs = [f"{h or f'col{i + 1}'}: {v.strip()}" for i, (h, v) in enumerate(cells) if v.strip()]
        lines.append("; ".join(pairs))
    return Document(source="", title=fallback_title, sections=[Section("", "\n".join(lines))])


# --------------------------------------------------------------------------- helpers


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _title_from_name(name: str) -> str:
    stem = name.rstrip("/").split("/")[-1]
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    words = re.sub(r"[-_]+", " ", stem).strip()
    return words[:1].upper() + words[1:] if words else name
