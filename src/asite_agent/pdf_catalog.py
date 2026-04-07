from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader


URL_PATTERN = re.compile(r"https://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
SECTION_PATTERN = re.compile(r"^\s*(\d+\.\d+)\.\s+(.+?)\s*$")


@dataclass
class AsiteApiCatalog:
    source_pdf: str
    sections: list[dict[str, str]]
    sample_uris: list[str]

    def has_uri_hint(self, hint: str) -> bool:
        return any(hint in uri for uri in self.sample_uris)


def _extract_sections(text: str) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    for line in text.splitlines():
        match = SECTION_PATTERN.match(line.strip())
        if not match:
            continue
        sections.append({"number": match.group(1), "title": match.group(2)})
    return sections


def _extract_uris(text: str) -> list[str]:
    candidates = set(URL_PATTERN.findall(text))
    # Some links are wrapped in the PDF text stream; try an additional pass without line breaks.
    candidates.update(URL_PATTERN.findall(text.replace("\n", "")))

    uris: set[str] = set()
    for uri in candidates:
        cleaned = uri.rstrip(").,;\">")
        if "asite.com" not in cleaned:
            continue
        if len(cleaned) < 20:
            continue
        uris.add(cleaned)
    return sorted(uris)


def load_or_build_catalog(pdf_path: Path) -> AsiteApiCatalog:
    cache_dir = Path(".cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "asite_api_catalog.json"

    if cache_path.exists() and pdf_path.exists():
        if cache_path.stat().st_mtime >= pdf_path.stat().st_mtime:
            return AsiteApiCatalog(**json.loads(cache_path.read_text(encoding="utf-8")))

    if not pdf_path.exists():
        return AsiteApiCatalog(source_pdf=str(pdf_path), sections=[], sample_uris=[])

    reader = PdfReader(str(pdf_path))
    page_text: list[str] = []
    # First 120 pages include TOC and most endpoint examples, enough for this stage.
    for page in reader.pages[:120]:
        page_text.append(page.extract_text() or "")
    full_text = "\n".join(page_text)

    catalog = AsiteApiCatalog(
        source_pdf=str(pdf_path),
        sections=_extract_sections(full_text),
        sample_uris=_extract_uris(full_text),
    )
    cache_path.write_text(
        json.dumps(
            {
                "source_pdf": catalog.source_pdf,
                "sections": catalog.sections,
                "sample_uris": catalog.sample_uris,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return catalog
