"""Trafilatura-backed, versioned article extraction."""

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from html.parser import HTMLParser
import re
import unicodedata
from urllib.parse import urlsplit

import trafilatura
from trafilatura.metadata import extract_metadata

from app.models import EffortBand, ExtractionErrorType, ExtractionStatus
from app.services.effort import effort_band_v1
from app.services.fetch import FetchError, SafeHttpFetcher


@dataclass(frozen=True)
class ExtractionV1Config:
    """Pilot v1 values; they are configurable rules, not validated standards."""

    extraction_version: str = "extraction/v1"
    effort_rule_version: str = "effort_rule/v1"
    minimum_chars: int = 800
    minimum_units: int = 3
    source_text_max_chars: int = 50_000
    excerpt_max_chars: int = 280
    effort_short_max_chars: int = 6_000
    effort_medium_max_chars: int = 18_000


@dataclass(frozen=True)
class ArticleExtraction:
    title: str
    source_domain: str
    source_text: str | None
    source_text_hash: str | None
    source_char_count: int | None
    source_truncated: bool
    source_excerpt: str | None
    extraction_status: ExtractionStatus
    extraction_error_type: ExtractionErrorType | None
    extraction_version: str
    effort_band: EffortBand
    effort_rule_version: str


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._inside_title = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title":
            self._inside_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._inside_title = False

    def handle_data(self, data: str) -> None:
        if self._inside_title:
            self.parts.append(data)


def normalize_text_v1(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = []
    for line in value.split("\n"):
        line = re.sub(r"[\t \f\v]+", " ", line).strip()
        if line:
            paragraphs.append(line)
    return "\n\n".join(paragraphs)


def deterministic_excerpt_v1(text: str, max_chars: int = 280) -> str | None:
    for match in re.finditer(r"[^.!?。！？]+[.!?。！？]+", text):
        sentence = match.group(0).strip()
        if 60 <= len(sentence) <= max_chars and any(character.isalnum() for character in sentence):
            return sentence
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return None
    if len(compact) <= max_chars:
        return compact
    candidate = compact[:max_chars]
    boundary = candidate.rfind(" ")
    if boundary >= max_chars // 2:
        candidate = candidate[:boundary]
    return candidate.rstrip()


def _unit_count(text: str) -> int:
    paragraph_count = len([part for part in text.split("\n\n") if part.strip()])
    sentence_count = len(re.findall(r"[.!?。！？](?:\s|$)", text))
    return max(paragraph_count, sentence_count)


def _domain(url: str) -> str:
    return (urlsplit(url).hostname or "Unknown source").lower()


def _html_title(html: str) -> str | None:
    parser = _TitleParser()
    try:
        parser.feed(html)
    except Exception:
        return None
    title = normalize_text_v1(" ".join(parser.parts)).replace("\n\n", " ").strip()
    return title[:500] or None


class ArticleExtractor:
    def __init__(
        self,
        fetcher: SafeHttpFetcher,
        *,
        config: ExtractionV1Config | None = None,
        text_extractor: Callable[..., str | None] = trafilatura.extract,
    ) -> None:
        self._fetcher = fetcher
        self._config = config or ExtractionV1Config()
        self._text_extractor = text_extractor

    def extract(self, normalized_url: str) -> ArticleExtraction:
        config = self._config
        fallback_domain = _domain(normalized_url)
        try:
            fetched = self._fetcher.fetch(normalized_url)
        except FetchError as exc:
            status = (
                ExtractionStatus.UNSUPPORTED
                if exc.code is ExtractionErrorType.UNSUPPORTED_MIME
                else ExtractionStatus.FAILED
            )
            return ArticleExtraction(
                title=fallback_domain,
                source_domain=fallback_domain,
                source_text=None,
                source_text_hash=None,
                source_char_count=None,
                source_truncated=False,
                source_excerpt=None,
                extraction_status=status,
                extraction_error_type=exc.code,
                extraction_version=config.extraction_version,
                effort_band=EffortBand.UNKNOWN,
                effort_rule_version=config.effort_rule_version,
            )

        domain = _domain(fetched.final_url)
        html = fetched.content.decode("utf-8", errors="replace")
        title = None
        try:
            metadata = extract_metadata(html, default_url=fetched.final_url)
            if metadata is not None and metadata.title:
                title = normalize_text_v1(metadata.title).replace("\n\n", " ")[:500] or None
        except Exception:
            title = None
        title = title or _html_title(html) or domain

        try:
            extracted = self._text_extractor(
                html,
                url=fetched.final_url,
                output_format="txt",
                include_comments=False,
                include_tables=False,
                favor_precision=True,
            )
            normalized = normalize_text_v1(extracted or "")
        except Exception:
            normalized = ""

        if not normalized:
            return ArticleExtraction(
                title=title,
                source_domain=domain,
                source_text=None,
                source_text_hash=None,
                source_char_count=None,
                source_truncated=False,
                source_excerpt=None,
                extraction_status=ExtractionStatus.FAILED,
                extraction_error_type=ExtractionErrorType.EXTRACTION_FAILED,
                extraction_version=config.extraction_version,
                effort_band=EffortBand.UNKNOWN,
                effort_rule_version=config.effort_rule_version,
            )

        character_count = len(normalized)
        excerpt = deterministic_excerpt_v1(normalized, config.excerpt_max_chars)
        if character_count < config.minimum_chars or _unit_count(normalized) < config.minimum_units:
            return ArticleExtraction(
                title=title,
                source_domain=domain,
                source_text=None,
                source_text_hash=None,
                source_char_count=character_count,
                source_truncated=False,
                source_excerpt=excerpt,
                extraction_status=ExtractionStatus.INADEQUATE,
                extraction_error_type=ExtractionErrorType.INADEQUATE_CONTENT,
                extraction_version=config.extraction_version,
                effort_band=EffortBand.UNKNOWN,
                effort_rule_version=config.effort_rule_version,
            )

        effort = effort_band_v1(
            extraction_status=ExtractionStatus.SUCCESS,
            source_char_count=character_count,
            short_max=config.effort_short_max_chars,
            medium_max=config.effort_medium_max_chars,
        )
        if character_count > config.source_text_max_chars:
            return ArticleExtraction(
                title=title,
                source_domain=domain,
                source_text=None,
                source_text_hash=None,
                source_char_count=character_count,
                source_truncated=True,
                source_excerpt=excerpt,
                extraction_status=ExtractionStatus.SUCCESS,
                extraction_error_type=None,
                extraction_version=config.extraction_version,
                effort_band=effort,
                effort_rule_version=config.effort_rule_version,
            )

        return ArticleExtraction(
            title=title,
            source_domain=domain,
            source_text=normalized,
            source_text_hash=sha256(normalized.encode("utf-8")).hexdigest(),
            source_char_count=character_count,
            source_truncated=False,
            source_excerpt=excerpt,
            extraction_status=ExtractionStatus.SUCCESS,
            extraction_error_type=None,
            extraction_version=config.extraction_version,
            effort_band=effort,
            effort_rule_version=config.effort_rule_version,
        )
