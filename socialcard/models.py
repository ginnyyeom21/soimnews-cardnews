"""파이프라인이 단계 사이에서 주고받는 데이터 구조."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class Article:
    article_id: str
    title: str
    url: str
    content: str
    published_at: str
    author: str = ""
    section: str = ""
    thumbnail_url: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Card:
    """카러셀 한 장에 들어가는 텍스트."""

    kind: str  # cover | body | outro
    title: str  # 커버=헤드라인, 본문=킥커, 아웃트로=CTA 제목
    body: str = ""
    highlight: str = ""  # 본문 카드의 핵심 숫자·일정 배지
    footnote: str = ""  # 아웃트로 카드의 '원문에 더 있는 것' 한 줄
    index: int = 0
    image_path: Optional[Path] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["image_path"] = str(self.image_path) if self.image_path else None
        return data


@dataclass
class Mention:
    name: str
    handle: str
    kind: str = "org"  # org | person


@dataclass
class CardNews:
    """기사 1건에서 만들어진 카드뉴스 한 세트."""

    article: Article
    headline: str
    hook: str
    cards: List[Card]
    caption: str
    read_more: str = ""
    hashtags: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)  # 기사에서 뽑은 기관/인물 이름
    mentions: List[Mention] = field(default_factory=list)
    # 이 기사에서만 태그하지 않을 계정. 매핑에는 남겨두고 이 건에서만 뺀다
    # (예: 본문에 스쳐 지나간 기관이라 알림을 보내는 게 어색한 경우).
    excluded_handles: List[str] = field(default_factory=list)
    unmatched_entities: List[str] = field(default_factory=list)
    # 커버에 적용된 후킹 유형. AI/폴백이 고른 deadline·scale·stake·scene,
    # 사람이 덮어쓴 경우 manual, 어느 것도 적용하지 못했으면 빈 값.
    hook_type: str = ""
    generator: str = "rule"
    cta: str = "프로필 링크에서 기사 전문 보기"
    link_url: str = ""  # 링크인바이오 페이지 주소(있으면 캡션에 함께 안내)
    include_mentions_in_caption: bool = False

    def mention_line(self) -> str:
        return " ".join(dict.fromkeys(m.handle for m in self.mentions))

    def usernames(self) -> List[str]:
        """업로드 시 user_tags로 넘길 계정 아이디(@ 없이)."""
        return list(dict.fromkeys(m.handle.lstrip("@") for m in self.mentions))

    def full_caption(self) -> str:
        """캡션 본문 + 원문 유도 + 링크 + 해시태그를 인스타그램 본문 하나로 합친다.

        인스타그램은 첫 두 줄만 펼쳐 보이므로 요약이 맨 앞에 오고, 링크 안내는 그 뒤에 둔다.
        """
        parts: List[str] = [self.caption.strip()]

        cta_block = []
        if self.read_more:
            cta_block.append("▶ " + self.read_more)
        cta_block.append(self.cta)
        cta_block.append(self.article.url)
        if self.link_url:
            cta_block.append("오늘의 기사 모음: " + self.link_url)
        parts.append("\n".join(cta_block))

        if self.include_mentions_in_caption and self.mentions:
            parts.append("함께 보기 " + self.mention_line())
        if self.hashtags:
            parts.append(" ".join(dict.fromkeys(self.hashtags)))

        text = "\n\n".join(p for p in parts if p)
        return text[:2200]  # 인스타그램 캡션 상한

    def to_dict(self) -> Dict[str, Any]:
        return {
            "article": self.article.to_dict(),
            "headline": self.headline,
            "hook": self.hook,
            "hook_type": self.hook_type,
            "cards": [c.to_dict() for c in self.cards],
            "caption": self.caption,
            "read_more": self.read_more,
            "full_caption": self.full_caption(),
            "user_tags": self.usernames(),
            "hashtags": self.hashtags,
            "entities": self.entities,
            "mentions": [asdict(m) for m in self.mentions],
            "unmatched_entities": self.unmatched_entities,
            "generator": self.generator,
        }


@dataclass
class PublishResult:
    article_url: str
    status: str  # published | dry_run | skipped_duplicate | failed
    media_id: Optional[str] = None
    permalink: Optional[str] = None
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
