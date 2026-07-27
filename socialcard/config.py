"""설정 로딩. .env 파일과 환경변수를 병합한다(환경변수 우선)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = ROOT / "config" / "settings.env"

RSS_URL = "https://www.socialimpactnews.net/rss/allArticle.xml"
SITE_BASE = "https://www.socialimpactnews.net"


def _read_env_file(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    data: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        data[key.strip()] = value
    return data


def _as_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int(value: Optional[str], default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    # 수집
    rss_url: str = RSS_URL
    article_count: int = 10
    min_articles: int = 1
    request_timeout: int = 20
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) socialcard-bot/1.0"
    )

    # AI
    ai_provider: str = "anthropic"  # anthropic | rule
    anthropic_api_key: Optional[str] = None
    anthropic_model: str = "claude-opus-5"
    ai_max_retries: int = 2

    # 카드 렌더링 — 색상은 소임뉴 로고에서 추출한 값
    card_size: int = 1080  # 가로
    # 세로. 인스타그램 프로필 격자가 4:5로 잘라 보여주므로 처음부터 4:5로 만든다.
    # 정사각형(1080)으로 두면 격자에서 좌우가 각각 108px씩 잘려 글자가 사라진다.
    card_height: int = 1350
    body_card_count: int = 3
    brand_name: str = "소셜임팩트뉴스"
    brand_email: str = "press@soimnews.net"
    brand_handle: str = "@soimnews"  # 발행 대상 인스타그램 계정
    # 아웃트로 카드에 노출할 매체 주소. 인스타그램 이미지는 클릭되지 않으므로,
    # 프로필 링크에 기대지 않고 독자가 눈으로 읽고 찾아갈 수 있는 주소를 적는다.
    brand_site: str = "socialimpactnews.net"
    brand_color: str = "#00A76B"  # 로고 에메랄드
    accent_color: str = "#89F394"  # 로고 마크의 라이트 그린
    ink_color: str = "#3C3C3C"  # 로고 차콜
    paper_color: str = "#FBFCFB"
    logo_path: Path = field(default_factory=lambda: ROOT / "소임뉴2.png")

    # 원문 유도(랜딩) — 인스타그램 캡션의 URL은 클릭되지 않으므로 프로필 링크로 보낸다
    link_cta: str = "프로필 링크에서 기사 전문 보기"
    linkinbio_base_url: Optional[str] = None  # 생성된 링크 페이지가 배포될 주소
    linkinbio_limit: int = 12  # 페이지에 노출할 최근 기사 수

    # 계정 태그 — 카드 이미지에는 찍지 않고 업로드 시 user_tags로 붙인다
    caption_mentions: bool = False  # 캡션에도 @멘션을 남길지
    account_auto_resolve: bool = True
    account_resolve_model: str = "claude-opus-5"
    account_resolve_budget: int = 6  # 실행 1회당 새로 검색할 기관 수 상한

    # 발행
    publisher: str = "graph"  # graph | webhook | console
    ig_user_id: Optional[str] = None
    ig_access_token: Optional[str] = None
    ig_api_version: str = "v21.0"
    ig_publish_target: str = "carousel"  # carousel | story
    publish_webhook_url: Optional[str] = None
    public_image_base_url: Optional[str] = None
    publish_delay_seconds: int = 5

    # 알림 / 저장
    alert_webhook_url: Optional[str] = None
    out_dir: Path = field(default_factory=lambda: ROOT / "out")
    db_path: Path = field(default_factory=lambda: ROOT / "state" / "socialcard.sqlite3")
    accounts_path: Path = field(default_factory=lambda: ROOT / "config" / "accounts.json")
    # 편집자가 커버 문구를 손으로 덮어쓰는 파일. 없으면 덮어쓰기 없이 그대로 돈다.
    overrides_path: Path = field(default_factory=lambda: ROOT / "config" / "overrides.csv")

    @property
    def total_cards(self) -> int:
        """커버 1장 + 본문 N장 + 아웃트로 1장."""
        return self.body_card_count + 2


def load_settings(env_file: Optional[Path] = None) -> Settings:
    file_env = _read_env_file(env_file or DEFAULT_ENV_FILE)

    def get(key: str, default: Optional[str] = None) -> Optional[str]:
        value = os.environ.get(key)
        if value is None or value == "":
            value = file_env.get(key)
        return value if value not in (None, "") else default

    settings = Settings()
    settings.rss_url = get("RSS_URL", RSS_URL) or RSS_URL
    settings.article_count = _as_int(get("ARTICLE_COUNT"), settings.article_count)
    settings.min_articles = _as_int(get("MIN_ARTICLES"), settings.min_articles)
    settings.request_timeout = _as_int(get("REQUEST_TIMEOUT"), settings.request_timeout)

    settings.ai_provider = (get("AI_PROVIDER", settings.ai_provider) or "").lower()
    settings.anthropic_api_key = get("ANTHROPIC_API_KEY")
    settings.anthropic_model = get("ANTHROPIC_MODEL", settings.anthropic_model) or settings.anthropic_model
    settings.ai_max_retries = _as_int(get("AI_MAX_RETRIES"), settings.ai_max_retries)

    settings.body_card_count = _as_int(get("BODY_CARD_COUNT"), settings.body_card_count)
    settings.brand_name = get("BRAND_NAME", settings.brand_name) or settings.brand_name
    settings.brand_email = get("BRAND_EMAIL", settings.brand_email) or settings.brand_email
    settings.brand_handle = get("BRAND_HANDLE", settings.brand_handle) or settings.brand_handle
    settings.brand_site = get("BRAND_SITE", settings.brand_site) or settings.brand_site
    settings.brand_color = get("BRAND_COLOR", settings.brand_color) or settings.brand_color
    settings.accent_color = get("ACCENT_COLOR", settings.accent_color) or settings.accent_color
    settings.ink_color = get("INK_COLOR", settings.ink_color) or settings.ink_color
    settings.paper_color = get("PAPER_COLOR", settings.paper_color) or settings.paper_color
    logo = get("LOGO_PATH")
    if logo:
        settings.logo_path = Path(logo).expanduser()

    settings.link_cta = get("LINK_CTA", settings.link_cta) or settings.link_cta
    settings.linkinbio_base_url = get("LINKINBIO_BASE_URL")
    settings.linkinbio_limit = _as_int(get("LINKINBIO_LIMIT"), settings.linkinbio_limit)

    settings.caption_mentions = _as_bool(get("CAPTION_MENTIONS"), settings.caption_mentions)
    settings.account_auto_resolve = _as_bool(get("ACCOUNT_AUTO_RESOLVE"), settings.account_auto_resolve)
    settings.account_resolve_model = (
        get("ACCOUNT_RESOLVE_MODEL", settings.account_resolve_model) or settings.account_resolve_model
    )
    settings.account_resolve_budget = _as_int(
        get("ACCOUNT_RESOLVE_BUDGET"), settings.account_resolve_budget
    )

    settings.publisher = (get("PUBLISHER", settings.publisher) or "").lower()
    settings.ig_user_id = get("IG_USER_ID")
    settings.ig_access_token = get("IG_ACCESS_TOKEN")
    settings.ig_api_version = get("IG_API_VERSION", settings.ig_api_version) or settings.ig_api_version
    settings.ig_publish_target = (get("IG_PUBLISH_TARGET", settings.ig_publish_target) or "").lower()
    settings.publish_webhook_url = get("PUBLISH_WEBHOOK_URL")
    settings.public_image_base_url = get("PUBLIC_IMAGE_BASE_URL")
    settings.publish_delay_seconds = _as_int(get("PUBLISH_DELAY_SECONDS"), settings.publish_delay_seconds)

    settings.alert_webhook_url = get("ALERT_WEBHOOK_URL")

    out_dir = get("OUT_DIR")
    if out_dir:
        settings.out_dir = Path(out_dir).expanduser()
    db_path = get("DB_PATH")
    if db_path:
        settings.db_path = Path(db_path).expanduser()
    accounts_path = get("ACCOUNTS_PATH")
    if accounts_path:
        settings.accounts_path = Path(accounts_path).expanduser()
    overrides_path = get("OVERRIDES_PATH")
    if overrides_path:
        settings.overrides_path = Path(overrides_path).expanduser()

    # AI 키가 없으면 규칙 기반 요약으로 자동 강등(오프라인/테스트 환경 대비).
    if settings.ai_provider == "anthropic" and not settings.anthropic_api_key:
        settings.ai_provider = "rule"

    return settings


def missing_publish_config(settings: Settings) -> List[str]:
    """실제 발행에 필요한데 비어 있는 설정 키 목록."""
    missing: List[str] = []
    if settings.publisher == "graph":
        if not settings.ig_user_id:
            missing.append("IG_USER_ID")
        if not settings.ig_access_token:
            missing.append("IG_ACCESS_TOKEN")
        if not settings.public_image_base_url:
            missing.append("PUBLIC_IMAGE_BASE_URL")
    elif settings.publisher == "webhook":
        if not settings.publish_webhook_url:
            missing.append("PUBLISH_WEBHOOK_URL")
    return missing
