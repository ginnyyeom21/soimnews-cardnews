"""기사 원문 → 카드뉴스 텍스트.

카드뉴스는 그 자체로 완결된 콘텐츠가 아니라 **기사 전문으로 데려가는 홍보물**이다.
그래서 이 모듈이 만드는 텍스트는 다음 원칙을 따른다.

1. 커버는 후킹이다. 기사 제목을 그대로 옮기지 않고 "이건 나한테 필요한 정보다"를 만든다.
2. 한 장에 메시지 하나. 카드마다 킥커(무슨 일이/핵심은/왜 중요하냐면)로 역할을 고정한다.
3. 숫자·일정·대상 같은 구체적 사실을 앞세운다. 추상적 수식어는 정보가 아니다.
4. 문장은 잘리지 않는다. 중간에 끊긴 문장은 정보가 아니라 노이즈다.
5. 마지막은 "원문에 더 있는 것"을 알려 클릭 이유를 만든다.

provider="anthropic" 이면 Claude가 위 구조에 맞춰 새로 쓰고,
provider="rule" 이면 API 없이 기사 리드와 사실 밀도가 높은 문장을 골라 같은 구조를 채운다.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence

from .accounts import AccountDirectory
from .config import Settings
from .errors import SummarizeError
from .models import Article, Card, CardNews

log = logging.getLogger(__name__)

MAX_HEADLINE = 40
MAX_HOOK = 60
MAX_KICKER = 12
MAX_CARD_BODY = 110
MAX_HIGHLIGHT = 18
MAX_READ_MORE = 60
BASE_HASHTAGS = ["#소셜임팩트뉴스", "#소셜임팩트", "#임팩트뉴스"]
DEFAULT_KICKERS = ["무슨 일이", "핵심은", "왜 중요하냐면", "앞으로는", "함께 보면"]

# 커버 후킹 유형. 언론사 계정이라 '주제 은폐형' 낚시는 쓸 수 없으므로,
# 없는 궁금증을 만드는 대신 기사에 이미 있는 사실 중 무엇을 앞으로 당길지를 고르게 한다.
HOOK_TYPES = ("deadline", "scale", "stake", "scene")

SYSTEM_PROMPT = """너는 비영리·사회적경제 전문 매체 '소셜임팩트뉴스'의 인스타그램 카드뉴스 에디터다.
카드뉴스의 목적은 정보를 다 주는 것이 아니라, 독자가 기사 전문을 읽고 싶게 만드는 것이다.

작성 원칙:
1. 커버 헤드라인은 기사 제목을 옮겨 적지 않는다. 아래 네 가지 후킹 유형 중 이 기사에 맞는 것을
   하나 골라 hook_type에 적고, 그 유형에 맞춰 20자 안팎으로 쓴다.
   - deadline(마감형) · 모집·공모·신청 기사. 언제까지 신청해야 하는지를 맨 앞으로 당긴다.
     예) 신청은 이달 31일까지
   - scale(규모형) · 숫자 자체가 뉴스인 기사. 선정 규모·인원·금액·비율을 헤드라인에 그대로 쓴다.
     예) 82팀이 지원해 8팀이 남았다
   - stake(이해관계 전환형) · 협약·MOU·제휴 기사. 기관이 무엇을 했는지가 아니라 독자가 무엇을
     얻게 되는지로 바꿔 쓴다.
     예) 충남 청년정책, 전국에서 검색된다
   - scene(장면형) · 인터뷰·기고·현장 기사. 기사에서 가장 구체적인 장면이나 수치 하나를 꺼낸다.
     예) 폐지 1kg에 50원, 시급 1226원
   낚시성 과장과 주제 은폐는 금지다. 궁금증을 지어내지 말고 기사 안에 이미 있는 사실을 앞으로 당긴다.
2. 한 카드에는 메시지 하나만 담는다. 카드 본문은 완성된 문장 1~2개로 끝낸다. 절대 문장을 중간에 끊지 않는다.
3. 숫자, 기간, 대상, 금액, 규모 같은 구체적 사실을 우선한다. '다양한', '적극적인' 같은 수식어는 쓰지 않는다.
4. 기사 원문에 없는 사실·숫자·고유명사를 만들지 않는다. 확실하지 않으면 쓰지 않는다.
5. 기관명과 사업명은 기사 표기를 그대로 따른다.
6. read_more에는 카드에 담지 못한, 원문에만 있는 내용을 한 줄로 알려 클릭 이유를 만든다.
   실제로 기사에 있는 내용만 언급한다.
7. 이모지와 느낌표를 쓰지 않는다.
8. entities에는 기사에 등장한 기관/단체/기업과 인물 이름만 넣는다. 인스타그램 계정은 추측하지 않는다.
"""

CARD_TOOL: Dict[str, Any] = {
    "name": "emit_cardnews",
    "description": "기사 1건을 인스타그램 카드뉴스용 텍스트로 재구성해 제출한다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "hook_type": {
                "type": "string",
                "enum": list(HOOK_TYPES),
                "description": (
                    "이 기사에 적용한 후킹 유형. deadline=마감형(모집·공모), scale=규모형(숫자가 뉴스), "
                    "stake=이해관계 전환형(협약·MOU), scene=장면형(인터뷰·기고·현장)."
                ),
            },
            "headline": {
                "type": "string",
                "description": (
                    "커버 후킹 문구. 20자 안팎(최대 40자). 기사 제목 복사 금지. "
                    "hook_type으로 고른 유형에 맞춰 쓴다."
                ),
            },
            "hook": {
                "type": "string",
                "description": "커버 부제. 헤드라인이 무슨 뜻인지 알려주는 맥락 한 줄. 최대 52자, 완성된 문장.",
            },
            "cards": {
                "type": "array",
                "description": "본문 카드. 무슨 일이 → 핵심은 → 왜 중요하냐면 순서로 하나씩.",
                "items": {
                    "type": "object",
                    "properties": {
                        "kicker": {
                            "type": "string",
                            "description": "카드 역할 라벨. 최대 12자. 예: 무슨 일이 / 핵심은 / 왜 중요하냐면",
                        },
                        "body": {
                            "type": "string",
                            "description": "완성된 문장 1~2개. 최대 110자. 기사에 있는 사실만.",
                        },
                        "highlight": {
                            "type": "string",
                            "description": "이 카드의 핵심 숫자나 일정(예: '15개 기업', '9월 5~6일'). 없으면 빈 문자열.",
                        },
                    },
                    "required": ["kicker", "body", "highlight"],
                },
            },
            "read_more": {
                "type": "string",
                "description": "카드에 못 담았지만 원문에는 있는 내용 한 줄. 최대 60자. 예: '신청 자격과 심사 일정은 원문에'",
            },
            "caption": {
                "type": "string",
                "description": "인스타그램 캡션 본문. 200~350자 한국어. 링크와 해시태그는 넣지 않는다.",
            },
            "hashtags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "'#'으로 시작하는 한국어 해시태그 4~6개.",
            },
            "entities": {
                "type": "array",
                "description": "기사에 실제로 등장한 기관/기업/단체 및 인물 이름.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "kind": {"type": "string", "enum": ["org", "person"]},
                    },
                    "required": ["name", "kind"],
                },
            },
        },
        "required": [
            "hook_type",
            "headline",
            "hook",
            "cards",
            "read_more",
            "caption",
            "hashtags",
            "entities",
        ],
    },
}


def _schema_for(settings: Settings) -> Dict[str, Any]:
    schema = json.loads(json.dumps(CARD_TOOL))
    cards = schema["input_schema"]["properties"]["cards"]
    cards["minItems"] = settings.body_card_count
    cards["maxItems"] = settings.body_card_count
    return schema


def _user_prompt(article: Article, settings: Settings) -> str:
    return (
        "다음 기사를 인스타그램 카드뉴스로 재구성해 emit_cardnews 도구로 제출해라.\n"
        "커버 1장 + 본문 {n}장 + 아웃트로 1장 구성이고, 너는 커버와 본문 {n}장의 텍스트를 쓴다.\n"
        "독자는 이 카드뉴스를 보고 기사 전문을 읽으러 갈지 말지 3초 안에 결정한다.\n\n"
        "[제목] {title}\n[섹션] {section}\n[발행] {published}\n[원문]\n{content}"
    ).format(
        n=settings.body_card_count,
        title=article.title,
        section=article.section or "미분류",
        published=article.published_at,
        content=article.content[:7000],
    )


# --------------------------------------------------------------------------- 텍스트 정리

_PAREN = re.compile(r"[（(\[][^)）\]]{0,60}[)）\]]")
_SPACES = re.compile(r"\s+")
_SENTENCE_END = re.compile(r"(?<=[다요][.!?])\s+|(?<=[.!?])\s+(?=[가-힣A-Z])|\n+")
_UNIT = r"%|퍼센트|원|명|개사|개|건|곳|팀|권|가정|일간|일|박|년|개월|월|주|시간|배|㎡|톤|kg"
# 만·억이 낀 수를 먼저 본다. 앞을 놓치면 '1만 2000원'에서 '2000원'만 잡혀 배지가 틀린다.
_NUMBER = re.compile(
    r"\d[\d,.]*\s*[만억]\s*\d[\d,.]*\s*(?:{u})"  # 1만 2000원
    r"|\d[\d,.]*\s*[만억]\s*(?:{u})"  # 63억 원
    r"|\d[\d,.]*\s*(?:{u})".format(u=_UNIT)
)
_DATE = re.compile(r"\d{1,2}월\s*\d{1,2}(?:~\d{1,2})?일|\d{4}년\s*\d{1,2}월|\d{1,2}월\s*\d{1,2}일")
_FUTURE = re.compile(r"(계획|예정|기대|전망|방침|추진|목표|나선다|밝혔다|확대|마련)")
# 폴백이 커버로 끌어올릴 마감 표현. 모집·공모 기사에서만 쓴다(아래 _RECRUIT와 같은 문장일 때).
_DEADLINE = re.compile(r"(?:오는\s*)?((?:\d{1,2}월\s*)?\d{1,2}일|이달\s*말|이번\s*달\s*말)\s*까지")
_RECRUIT = re.compile(r"모집|공모|접수|신청|참가|응모")
_TRIM_TAIL = re.compile(r"[\s,·]+$")


def strip_asides(text: str) -> str:
    """괄호 안 직함·영문 병기처럼 카드에서 읽기 방해가 되는 삽입구를 걷어낸다."""
    return _SPACES.sub(" ", _PAREN.sub("", text or "")).strip()


def _sentences(text: str) -> List[str]:
    out = []
    for raw in _SENTENCE_END.split(text or ""):
        s = strip_asides(raw)
        if len(s) >= 15:
            out.append(s)
    return out


def _clip_sentence(text: str, limit: int) -> str:
    """문장 경계를 지키며 길이를 맞춘다. 문장 중간에서는 절대 끊지 않는다."""
    text = _SPACES.sub(" ", (text or "").strip())
    if len(text) <= limit:
        return text
    # 여러 문장이면 앞에서부터 통째로 담을 수 있는 만큼만 담는다.
    parts = [p for p in _SENTENCE_END.split(text) if p.strip()]
    if len(parts) > 1:
        kept = ""
        for part in parts:
            candidate = (kept + " " + part).strip()
            if len(candidate) > limit:
                break
            kept = candidate
        if kept:
            return kept
    # 한 문장인데 너무 길면 절 경계에서 끊고 말줄임을 붙인다.
    cut = text[:limit]
    for mark in (", ", "며 ", "고 ", " "):
        pos = cut.rfind(mark)
        if pos > limit * 0.55:
            return _TRIM_TAIL.sub("", cut[:pos]) + "…"
    return _TRIM_TAIL.sub("", cut) + "…"


def _clip_phrase(text: str, limit: int) -> str:
    """제목·라벨처럼 문장이 아닌 짧은 구절용. 어절 경계를 지킨다."""
    text = _SPACES.sub(" ", (text or "").strip())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    pos = cut.rfind(" ")
    if pos > limit * 0.5:
        cut = cut[:pos]
    return _TRIM_TAIL.sub("", cut) + "…"


def find_highlight(text: str) -> str:
    """문장에서 눈에 띄는 숫자·일정 하나를 뽑는다."""
    for pattern in (_DATE, _NUMBER):
        match = pattern.search(text or "")
        if match:
            value = _SPACES.sub(" ", match.group(0)).strip()
            if len(value) <= MAX_HIGHLIGHT:
                return value
    return ""


# --------------------------------------------------------------------------- 검증


def validate_payload(payload: Dict[str, Any], settings: Settings) -> Dict[str, Any]:
    """AI 응답을 스키마에 맞게 검증하고 길이를 다듬는다. 구조가 어긋나면 예외."""
    if not isinstance(payload, dict):
        raise SummarizeError("AI 응답이 객체가 아닙니다: {}".format(type(payload).__name__))
    for key in ("headline", "hook", "caption"):
        if not str(payload.get(key, "")).strip():
            raise SummarizeError("필수 필드 누락 또는 공백: {}".format(key))

    cards = payload.get("cards")
    if not isinstance(cards, list) or len(cards) != settings.body_card_count:
        raise SummarizeError(
            "본문 카드 수가 맞지 않습니다: 기대 {}장 / 실제 {}".format(
                settings.body_card_count, len(cards) if isinstance(cards, list) else "없음"
            )
        )

    clean_cards = []
    for i, card in enumerate(cards, start=1):
        if not isinstance(card, dict):
            raise SummarizeError("{}번 카드 형식 오류".format(i))
        kicker = _clip_phrase(str(card.get("kicker", "")), MAX_KICKER) or DEFAULT_KICKERS[
            (i - 1) % len(DEFAULT_KICKERS)
        ]
        body = _clip_sentence(str(card.get("body", "")), MAX_CARD_BODY)
        if len(body) < 10:
            raise SummarizeError("{}번 카드 본문이 너무 짧습니다".format(i))
        highlight = _clip_phrase(str(card.get("highlight", "")), MAX_HIGHLIGHT)
        clean_cards.append({"kicker": kicker, "body": body, "highlight": highlight})

    hashtags = [
        h if str(h).startswith("#") else "#" + str(h).lstrip("#")
        for h in payload.get("hashtags") or []
        if str(h).strip()
    ]
    entities = []
    for ent in payload.get("entities") or []:
        if isinstance(ent, dict) and str(ent.get("name", "")).strip():
            entities.append({"name": str(ent["name"]).strip(), "kind": str(ent.get("kind", "org"))})
        elif isinstance(ent, str) and ent.strip():
            entities.append({"name": ent.strip(), "kind": "org"})

    # 유형 라벨이 어긋났다고 기사 1건을 통째로 버리지는 않는다. 라벨은 감사용이고 본문은 이미 검증됐다.
    hook_type = str(payload.get("hook_type", "")).strip().lower()
    if hook_type and hook_type not in HOOK_TYPES:
        log.warning("알 수 없는 후킹 유형이라 비워둡니다: %s", hook_type)
        hook_type = ""

    return {
        "hook_type": hook_type,
        "headline": _clip_phrase(str(payload["headline"]), MAX_HEADLINE),
        "hook": _clip_sentence(str(payload["hook"]), MAX_HOOK),
        "cards": clean_cards,
        "read_more": _clip_phrase(str(payload.get("read_more", "")), MAX_READ_MORE),
        "caption": re.sub(r"\n{3,}", "\n\n", str(payload["caption"]).strip())[:900],
        "hashtags": (hashtags + BASE_HASHTAGS)[:8],
        "entities": entities,
    }


# --------------------------------------------------------------------------- provider


def _generate_anthropic(article: Article, settings: Settings) -> Dict[str, Any]:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise SummarizeError("anthropic 패키지가 설치되어 있지 않습니다") from exc

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    tool = _schema_for(settings)
    last_error: Optional[Exception] = None

    for attempt in range(1, settings.ai_max_retries + 2):
        try:
            message = client.messages.create(
                model=settings.anthropic_model,
                max_tokens=2500,
                system=SYSTEM_PROMPT,
                tools=[tool],
                tool_choice={"type": "tool", "name": "emit_cardnews"},
                messages=[{"role": "user", "content": _user_prompt(article, settings)}],
            )
            for block in message.content:
                if getattr(block, "type", "") == "tool_use":
                    return validate_payload(dict(block.input), settings)
            raise SummarizeError("AI가 도구 호출을 반환하지 않았습니다")
        except SummarizeError as exc:
            last_error = exc
            log.warning("AI 응답 검증 실패(%d/%d): %s", attempt, settings.ai_max_retries + 1, exc)
        except Exception as exc:
            last_error = exc
            log.warning("AI 호출 실패(%d/%d): %s", attempt, settings.ai_max_retries + 1, exc)

    raise SummarizeError("AI 요약 생성 실패: {}".format(last_error))


_ORG_TAIL = re.compile(
    r"[가-힣A-Za-z0-9·]+(?:센터|재단|협동조합|법인|연구소|연구원|진흥원|뮤지움|미술관|박물관|"
    r"위원회|협회|공사|공단|시청|구청|도청|대학교|스타트업|컴퍼니|파운데이션|네트워크|랩|㈜)"
)
_QUOTED = re.compile(r"[‘'“\"]([^’'”\"]{2,20})[’'”\"]")
_PERSON = re.compile(r"([가-힣]{2,4})\s*(?:대표|이사장|센터장|교수|회장|사무국장|팀장)")
_NOT_A_NAME = re.compile(r"(면서|으로|에서|하며|것이|이라|라고|한다|했다|입니다|같은)$")


def _looks_like_name(name: str) -> bool:
    """따옴표 추출 결과에서 기관·사업명이 아닌 인용 구절을 걸러낸다."""
    name = name.strip().strip(" ,.·…!?")
    if not (2 <= len(name) <= 20):
        return False
    if _NOT_A_NAME.search(name):
        return False
    if name.count(" ") >= 2:
        return False
    return True


def _fact_score(sentence: str) -> float:
    """구체적 사실이 얼마나 담긴 문장인지. 숫자·일정·나열이 있으면 카드에 쓸 값어치가 있다."""
    score = 0.0
    score += 3.0 * len(_NUMBER.findall(sentence))
    score += 3.5 * len(_DATE.findall(sentence))
    if "▲" in sentence and len(sentence) <= MAX_CARD_BODY:
        # 나열문은 사실 밀도가 높지만 대개 길다. 카드에 통째로 들어갈 때만 가점한다.
        score += 1.5 * sentence.count("▲")
    if _FUTURE.search(sentence):
        score += 1.0
    score += min(len(sentence), MAX_CARD_BODY) / float(MAX_CARD_BODY)  # 너무 짧은 문장은 정보가 적다
    if len(sentence) > MAX_CARD_BODY:
        # 카드에 통째로 들어가지 못하면 말줄임으로 잘린다. 잘린 문장은 정보가 아니라 노이즈다.
        # 감점이 약하면 '▲…▲…' 나열문이 길이만으로 이기므로 넉넉히 깎는다.
        score -= (len(sentence) - MAX_CARD_BODY) / 12.0
    return score


def _topic_phrase(title: str) -> str:
    """제목에서 기관명 주어를 걷어내고 핵심 구절만 남긴다.

    지면 제목은 관행상 기관을 주어로 세우지만('제주센터, ○○ 모집'), 카드뉴스 독자에게 먼저
    읽혀야 하는 것은 '무엇을'이다. 기관명은 본문 카드와 계정 태그로 남으므로 커버에서는 뺀다.
    """
    text = strip_asides(title)
    head, sep, tail = text.partition(",")
    if sep and _ORG_TAIL.fullmatch(head.strip()) and len(tail.strip()) >= 8:
        text = tail
    return text.split("…")[0].strip(" ·-")


def _rule_headline(article: Article, sentences: Sequence[str]) -> tuple:
    """폴백이 만들 수 있는 후킹은 '기사에 이미 있는 사실을 앞으로 당기는' 것뿐이다.

    없는 문구를 지어내지 않으므로 마감형(모집 기사의 신청 마감)과 규모형(제목이 이미 숫자를
    앞세운 경우)만 다룬다. 이해관계 전환형·장면형은 판단이 필요해 AI 경로에만 있다.
    """
    topic = _topic_phrase(article.title)

    for sentence in sentences:
        match = _DEADLINE.search(sentence)
        if match and _RECRUIT.search(sentence):
            suffix = ", {}까지".format(_SPACES.sub(" ", match.group(1)).strip())
            headline = _clip_phrase(topic, MAX_HEADLINE - len(suffix)) + suffix
            return headline, "deadline"

    if _NUMBER.search(topic) or _DATE.search(topic):
        return _clip_phrase(topic, MAX_HEADLINE), "scale"
    return _clip_phrase(topic, MAX_HEADLINE), ""


def _pick_hook(sentences: Sequence[str]) -> str:
    """커버 부제. 말줄임으로 끊긴 문장은 훅이 되지 못하므로 통째로 들어가는 문장을 우선한다."""
    # 나열문은 길이가 맞아도 커버 부제로는 읽히지 않는다(항목만 훑고 지나간다).
    fits = [s for s in sentences if len(s) <= MAX_HOOK and "▲" not in s]
    if fits:
        with_fact = [s for s in fits if _NUMBER.search(s) or _DATE.search(s)]
        return max(with_fact or fits, key=len)  # 들어가는 것 중 가장 정보가 많은 문장
    return _clip_sentence(sentences[0], MAX_HOOK)


def _pick_body_sentences(sentences: Sequence[str], count: int) -> List[str]:
    """리드 → 사실 밀도 높은 문장 → 전망/계획 문장 순으로 서로 다른 문장을 고른다."""
    if not sentences:
        return []
    def choose(pool: Sequence[str], prefer_last: bool = False) -> str:
        """카드에 통째로 들어가는 문장을 우선한다. 사실이 많아도 잘리면 절반은 사라진다."""
        fits = [s for s in pool if len(s) <= MAX_CARD_BODY]
        candidates = fits or list(pool)
        return candidates[-1] if prefer_last else max(candidates, key=_fact_score)

    picked: List[str] = [sentences[0]]  # 리드 문단이 기사의 핵심 요약이다
    rest = list(sentences[1:])

    if rest:
        best = choose(rest)
        picked.append(best)
        rest.remove(best)

    while len(picked) < count and rest:
        future = [s for s in rest if _FUTURE.search(s)]
        nxt = choose(future, prefer_last=True) if future else choose(rest)
        picked.append(nxt)
        rest.remove(nxt)

    while len(picked) < count:  # 원문이 짧으면 마지막 문장을 재사용한다
        picked.append(picked[-1])
    return picked[:count]


def _generate_rule(article: Article, settings: Settings) -> Dict[str, Any]:
    """API 없이 동작하는 폴백. 원문 문장만 사용하므로 사실 왜곡이 없다."""
    sentences = _sentences(article.content) or [strip_asides(article.content)]
    lead = sentences[0]
    headline, hook_type = _rule_headline(article, sentences)
    bodies = _pick_body_sentences(sentences, settings.body_card_count)

    cards = []
    for i, body in enumerate(bodies):
        cards.append(
            {
                "kicker": DEFAULT_KICKERS[i % len(DEFAULT_KICKERS)],
                "body": _clip_sentence(body, MAX_CARD_BODY),
                "highlight": find_highlight(body),
            }
        )

    entities: List[Dict[str, str]] = []
    seen = set()
    candidates: List[str] = []
    head = article.title.split(",")[0].strip()
    if head and head != article.title.strip():
        candidates.append(head)
    candidates += _ORG_TAIL.findall(article.content) + _QUOTED.findall(article.content)
    for name in candidates:
        name = name.strip().strip(" ,.·…!?")
        if not _looks_like_name(name) or name in seen:
            continue
        seen.add(name)
        entities.append({"name": name, "kind": "org"})
    for name in _PERSON.findall(article.content):
        if name not in seen:
            seen.add(name)
            entities.append({"name": name, "kind": "person"})

    return validate_payload(
        {
            # 폴백은 없는 문구를 지어내지 않는다. 기사에 이미 있는 사실만 커버로 끌어올린다.
            "hook_type": hook_type,
            "headline": headline,
            "hook": _pick_hook(sentences),
            "cards": cards,
            # 폴백은 원문에 무엇이 더 있는지 단정할 수 없으므로 중립적으로 안내한다.
            "read_more": "기사 전문에서 더 자세한 내용을 확인하세요",
            "caption": "\n\n".join([_clip_sentence(lead, 220), _clip_sentence(bodies[-1], 220)]),
            "hashtags": ["#" + (article.section or "사회적경제")],
            "entities": entities[:8],
        },
        settings,
    )


def generate_payload(article: Article, settings: Settings) -> Dict[str, Any]:
    if settings.ai_provider == "anthropic":
        return _generate_anthropic(article, settings)
    if settings.ai_provider == "rule":
        return _generate_rule(article, settings)
    raise SummarizeError("알 수 없는 AI_PROVIDER: {}".format(settings.ai_provider))


# --------------------------------------------------------------------------- 조립


def apply_directory(
    cardnews: CardNews, directory: AccountDirectory, exclude_handle: Optional[str] = None
) -> CardNews:
    """계정 매핑이 바뀐 뒤 멘션을 다시 계산한다.

    멘션은 카드 이미지에 찍지 않는다. 업로드 시 user_tags로 붙고, 캡션에는 설정에 따라 들어간다.
    AI가 뽑은 기관 목록과 본문 직접 스캔을 합치고, 자기 계정은 제외한다.
    """
    mentions, unmatched = directory.resolve(cardnews.entities)
    known = {m.handle for m in mentions}
    for mention in directory.scan(cardnews.article.title + "\n" + cardnews.article.content):
        if mention.handle not in known:
            known.add(mention.handle)
            mentions.append(mention)
    if exclude_handle:
        target = exclude_handle if exclude_handle.startswith("@") else "@" + exclude_handle
        mentions = [m for m in mentions if m.handle.lower() != target.lower()]

    # 편집자가 이 기사에서만 빼기로 한 계정. 계정 자동 등록 뒤 이 함수가 다시 호출돼도
    # 제외가 풀리지 않도록 CardNews에 남은 목록을 기준으로 매번 걸러낸다.
    if cardnews.excluded_handles:
        dropped = {h.lstrip("@").lower() for h in cardnews.excluded_handles}
        mentions = [m for m in mentions if m.handle.lstrip("@").lower() not in dropped]

    cardnews.mentions, cardnews.unmatched_entities = mentions, unmatched
    return cardnews


def build_cardnews(
    article: Article, settings: Settings, directory: Optional[AccountDirectory] = None
) -> CardNews:
    payload = generate_payload(article, settings)
    names: Sequence[str] = [e["name"] for e in payload["entities"]]

    cards: List[Card] = [
        Card(kind="cover", title=payload["headline"], body=payload["hook"], index=1)
    ]
    for i, card in enumerate(payload["cards"], start=2):
        cards.append(
            Card(
                kind="body",
                title=card["kicker"],
                body=card["body"],
                highlight=card["highlight"],
                index=i,
            )
        )
    cards.append(
        Card(
            kind="outro",
            title="기사 전문 보기",
            # 이미지는 클릭되지 않는다. 프로필 링크에만 기대면 바이오를 안 걸어둔 날은
            # 갈 곳 없는 안내가 되므로, 눈으로 읽고 찾아갈 수 있는 주소를 함께 노출한다.
            body=settings.brand_site or "프로필 링크 {}".format(settings.brand_handle),
            footnote=payload["read_more"],
            index=len(cards) + 1,
        )
    )

    cardnews = CardNews(
        article=article,
        headline=payload["headline"],
        hook=payload["hook"],
        hook_type=payload.get("hook_type", ""),
        cards=cards,
        caption=payload["caption"],
        read_more=payload["read_more"],
        hashtags=payload["hashtags"],
        entities=list(names),
        unmatched_entities=list(names),
        generator=settings.ai_provider,
        cta=settings.link_cta,
        include_mentions_in_caption=settings.caption_mentions,
    )
    if directory is not None:
        apply_directory(cardnews, directory, exclude_handle=settings.brand_handle)
    return cardnews
