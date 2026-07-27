"""기사 수집. RSS를 1차 소스로 쓰고 본문은 기사 페이지에서 스크래핑한다."""
from __future__ import annotations

import csv
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

from .config import Settings
from .errors import CollectError
from .models import Article

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
BODY_SELECTORS = ("#article-view-content-div", "[itemprop='articleBody']", ".article-veiw-body")
# 본문 안에 섞여 들어오는 사진 캡션 / 저작권 / 기자 서명 줄
NOISE_PATTERNS = (
    re.compile(r"^/?(사진|제공|자료)\s*=", re.I),
    re.compile(r"저작권자.*무단전재"),
    re.compile(r"^[\w가-힣]+\s*기자\s*[\w.@-]*$"),
    re.compile(r"^\S+@\S+\.\S+$"),
    re.compile(r"/(제공|사진)\s*=", re.I),
    re.compile(r"^[☞▶※◈]"),  # 연재 안내·관련기사 링크 줄
    re.compile(r"편집자\s*주"),
    re.compile(r"^\[.{0,20}\]$"),  # [현장의 틈] 같은 연재 표지 줄
)


def _session(settings: Settings) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": settings.user_agent, "Accept-Language": "ko-KR,ko;q=0.9"})
    return s


def _text(node: Optional[ET.Element]) -> str:
    return (node.text or "").strip() if node is not None else ""


def _article_id_from_url(url: str) -> str:
    qs = parse_qs(urlparse(url).query)
    idxno = qs.get("idxno", [""])[0]
    if idxno:
        return "SIN{}".format(idxno)
    slug = re.sub(r"[^A-Za-z0-9]+", "", urlparse(url).path)[-8:]
    return "SIN{}".format(slug or "UNKNOWN")


def _parse_pubdate(raw: str) -> str:
    """RSS pubDate(KST 로컬 표기)를 ISO8601 +09:00 문자열로 정규화한다."""
    raw = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.astimezone(KST).isoformat()
    return datetime.now(KST).isoformat()


def _clean_body(text: str) -> str:
    lines: List[str] = []
    for line in (l.strip() for l in text.splitlines()):
        if not line:
            continue
        if any(p.search(line) for p in NOISE_PATTERNS):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def fetch_rss_entries(settings: Settings, session: Optional[requests.Session] = None) -> List[Dict[str, str]]:
    session = session or _session(settings)
    try:
        resp = session.get(settings.rss_url, timeout=settings.request_timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise CollectError("RSS 요청 실패: {} ({})".format(settings.rss_url, exc)) from exc

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        raise CollectError("RSS 파싱 실패: {} ({})".format(settings.rss_url, exc)) from exc

    entries: List[Dict[str, str]] = []
    for item in root.iter("item"):
        link = _text(item.find("link"))
        if not link:
            continue
        entries.append(
            {
                "title": _text(item.find("title")),
                "link": link,
                "description": _text(item.find("description")),
                "author": _text(item.find("author")),
                "pub_date": _text(item.find("pubDate")),
            }
        )
    if not entries:
        raise CollectError("RSS에 기사 항목이 없습니다: {}".format(settings.rss_url))
    return entries


def fetch_article_body(url: str, settings: Settings, session: Optional[requests.Session] = None) -> Dict[str, str]:
    """기사 페이지에서 본문/섹션/썸네일을 긁어온다."""
    session = session or _session(settings)
    try:
        resp = session.get(url, timeout=settings.request_timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise CollectError("기사 페이지 요청 실패: {} ({})".format(url, exc)) from exc

    resp.encoding = resp.encoding or "utf-8"
    soup = BeautifulSoup(resp.text, "lxml")

    body_node = None
    for selector in BODY_SELECTORS:
        body_node = soup.select_one(selector)
        if body_node is not None:
            break
    if body_node is None:
        raise CollectError("본문 영역을 찾지 못했습니다(HTML 구조 변경 가능): {}".format(url))

    body = _clean_body(body_node.get_text("\n", strip=True))
    if len(body) < 60:
        raise CollectError("본문이 너무 짧습니다({}자): {}".format(len(body), url))

    def meta(prop: str) -> str:
        tag = soup.select_one("meta[property='{}']".format(prop))
        return (tag.get("content") or "").strip() if tag else ""

    section = ""
    header = soup.select_one(".article-view-header")
    if header:
        crumbs = header.get_text(" ", strip=True).split()
        if len(crumbs) > 1 and crumbs[0] == "홈":
            section = crumbs[1]

    return {"body": body, "thumbnail_url": meta("og:image"), "section": section}


def collect_from_rss(settings: Settings, limit: Optional[int] = None) -> List[Article]:
    """RSS 최신순으로 limit건을 수집한다. 개별 기사 실패는 건너뛰고 계속 진행."""
    limit = limit or settings.article_count
    session = _session(settings)
    entries = fetch_rss_entries(settings, session)

    articles: List[Article] = []
    failures: List[str] = []
    for entry in entries:
        if len(articles) >= limit:
            break
        url = entry["link"]
        try:
            detail = fetch_article_body(url, settings, session)
        except CollectError as exc:
            log.warning("기사 스킵: %s", exc)
            failures.append(str(exc))
            continue
        articles.append(
            Article(
                article_id=_article_id_from_url(url),
                title=entry["title"].strip(),
                url=url,
                content=detail["body"],
                published_at=_parse_pubdate(entry["pub_date"]),
                author=entry["author"],
                section=detail["section"],
                thumbnail_url=detail["thumbnail_url"],
            )
        )

    if not articles:
        raise CollectError(
            "수집된 기사가 0건입니다. RSS 항목 {}건 모두 본문 추출에 실패했습니다. 최근 오류: {}".format(
                len(entries), failures[0] if failures else "없음"
            )
        )
    if len(articles) < settings.min_articles:
        raise CollectError(
            "수집 건수 미달: {}건 (최소 {}건 필요)".format(len(articles), settings.min_articles)
        )
    return articles


def collect_from_csv(path: Path, settings: Settings, limit: Optional[int] = None) -> List[Article]:
    """오프라인 테스트용. latest_articles.csv 같은 양식 파일에서 기사를 읽는다."""
    limit = limit or settings.article_count
    if not path.exists():
        raise CollectError("CSV 파일을 찾을 수 없습니다: {}".format(path))

    articles: List[Article] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        required = {"article_id", "title", "url", "content", "published_at"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise CollectError(
                "CSV 헤더가 양식과 다릅니다. 필요: {} / 실제: {}".format(
                    sorted(required), reader.fieldnames
                )
            )
        for row in reader:
            if len(articles) >= limit:
                break
            if not (row.get("url") or "").strip() or not (row.get("content") or "").strip():
                continue
            articles.append(
                Article(
                    article_id=(row.get("article_id") or "").strip() or _article_id_from_url(row["url"]),
                    title=(row.get("title") or "").strip(),
                    url=row["url"].strip(),
                    content=row["content"].strip(),
                    published_at=(row.get("published_at") or "").strip(),
                    # 선택 컬럼. 커버의 섹션 배지에 쓰인다(없으면 기본값으로 렌더링).
                    section=(row.get("section") or "").strip(),
                )
            )

    if not articles:
        raise CollectError("CSV에서 유효한 기사를 찾지 못했습니다: {}".format(path))
    if len(articles) < settings.min_articles:
        raise CollectError(
            "수집 건수 미달: {}건 (최소 {}건 필요)".format(len(articles), settings.min_articles)
        )
    return articles
