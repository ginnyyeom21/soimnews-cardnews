"""기사에서 뽑은 기관/인물 이름을 인스타그램 계정으로 매핑한다.

계정 태그는 잘못 붙으면 무관한 제3자를 소환하게 되므로 이 모듈은 절대 계정을
'그럴듯하게 지어내지' 않는다. 계정이 붙는 경로는 둘뿐이다.

1) config/accounts.json 에 사람이 직접 등록한 항목
2) AccountResolver 가 웹 검색으로 찾아낸 뒤, 근거 URL이 실제 instagram.com 프로필이고
   확신도가 high 인 경우에만 등록한 항목 (source/auto 필드로 감사 가능)

둘 다 아니면 unmatched로 보고만 하고 태그하지 않는다.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import Mention

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
_LEGAL_SUFFIX = re.compile(r"(주식회사|㈜|\(주\)|\(사\)|사단법인|재단법인|사회적협동조합|협동조합)")
_NON_WORD = re.compile(r"[^0-9A-Za-z가-힣]+")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{2,30}$")
_PROFILE_URL = re.compile(r"^https?://(www\.)?instagram\.com/([A-Za-z0-9._]{2,30})/?", re.I)


def normalize(name: str) -> str:
    name = _LEGAL_SUFFIX.sub("", name or "")
    return _NON_WORD.sub("", name).lower()


class AccountDirectory:
    def __init__(self, entries: Sequence[Dict[str, Any]], path: Optional[Path] = None):
        self.path = path
        self._entries: List[Dict[str, Any]] = []
        self._index: Dict[str, Mention] = {}
        for entry in entries:
            self._register(entry)

    def _register(self, entry: Dict[str, Any]) -> Optional[Mention]:
        handle = str(entry.get("handle", "")).strip().lstrip("@")
        name = str(entry.get("name", "")).strip()
        if not handle or not name or not _HANDLE_RE.match(handle):
            return None
        mention = Mention(name=name, handle="@" + handle, kind=str(entry.get("kind", "org")))
        for key in [name] + [str(a) for a in (entry.get("aliases") or [])]:
            norm = normalize(key)
            if norm:
                self._index[norm] = mention
        self._entries.append(entry)
        return mention

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> List[Dict[str, Any]]:
        return list(self._entries)

    def lookup(self, name: str) -> Tuple[List[Mention], List[str]]:
        norm = normalize(name)
        if norm and norm in self._index:
            return [self._index[norm]], []
        return [], [name]

    def resolve(self, names: Sequence[str]) -> Tuple[List[Mention], List[str]]:
        found: List[Mention] = []
        missing: List[str] = []
        seen = set()
        for name in names:
            mentions, unmatched = self.lookup(name)
            for mention in mentions:
                if mention.handle not in seen:
                    seen.add(mention.handle)
                    found.append(mention)
            missing.extend(unmatched)
        return found, list(dict.fromkeys(missing))

    def scan(self, text: str) -> List[Mention]:
        """등록된 기관 이름이 기사 본문에 실제로 등장하는지 직접 훑는다.

        AI가 뽑은 목록에서 빠지더라도, 이미 계정을 아는 기관은 놓치지 않기 위한 보완 경로다.
        """
        found: List[Mention] = []
        seen = set()
        for entry in self._entries:
            keys = [str(entry.get("name", ""))] + [str(a) for a in (entry.get("aliases") or [])]
            for key in keys:
                if len(key) >= 3 and key in text:
                    mention = self._index.get(normalize(key))
                    if mention and mention.handle not in seen:
                        seen.add(mention.handle)
                        found.append(mention)
                    break
        return found

    def add(self, entry: Dict[str, Any]) -> Optional[Mention]:
        return self._register(entry)

    def save(self) -> None:
        """자동 등록분을 포함해 매핑 파일을 다시 쓴다."""
        if self.path is None:
            return
        payload: Dict[str, Any] = {"accounts": self._entries}
        if self.path.exists():
            try:
                existing = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(existing, dict) and "_readme" in existing:
                    payload = {"_readme": existing["_readme"], "accounts": self._entries}
            except json.JSONDecodeError:
                pass
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def load_directory(path: Path) -> AccountDirectory:
    if not path.exists():
        log.warning("계정 매핑 파일이 없습니다(%s). 멘션 없이 진행합니다.", path)
        return AccountDirectory([], path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.warning("계정 매핑 파일 파싱 실패(%s): %s. 멘션 없이 진행합니다.", path, exc)
        return AccountDirectory([], path)
    entries = data.get("accounts", []) if isinstance(data, dict) else data
    if not isinstance(entries, list):
        log.warning("계정 매핑 형식이 올바르지 않습니다(%s).", path)
        return AccountDirectory([], path)
    return AccountDirectory(entries, path)


# --------------------------------------------------------------------------- 자동 검색

RESOLVE_TOOL = {
    "name": "emit_account",
    "description": "웹 검색으로 확인한 인스타그램 공식 계정을 제출한다. 확인하지 못했으면 found=false.",
    "input_schema": {
        "type": "object",
        "properties": {
            "found": {"type": "boolean", "description": "공식 계정을 확실히 확인했는가"},
            "handle": {"type": "string", "description": "@ 없이 계정 아이디만. 못 찾았으면 빈 문자열"},
            "profile_url": {
                "type": "string",
                "description": "https://www.instagram.com/<handle>/ 형태의 실제 프로필 URL",
            },
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "reason": {"type": "string", "description": "판단 근거를 한 문장으로"},
        },
        "required": ["found", "handle", "profile_url", "confidence", "reason"],
    },
}

RESOLVE_SYSTEM = """너는 한국 사회적경제·비영리 분야 기관의 공식 인스타그램 계정을 확인하는 조사원이다.
반드시 web_search로 검색해 실제 프로필을 확인한 뒤 emit_account 도구로 결과를 제출한다.

판정 규칙:
- 검색 결과에 instagram.com/<handle> 프로필이 실제로 존재하고, 그 계정이 질문한 그 기관 본인이라고
  확신할 수 있을 때만 found=true, confidence=high로 답한다.
- 동명이인·유사 이름·개인 계정·팬 계정·지역 지부가 헷갈리면 confidence를 낮추거나 found=false로 답한다.
- 계정을 못 찾는 것은 정상이다. 추측해서 만들어내면 무관한 사람이 태그되므로 절대 금지다.
"""


class AccountResolver:
    """미등록 기관명을 웹 검색으로 조회해 계정 매핑에 추가한다.

    같은 이름을 매일 다시 검색하지 않도록 결과(실패 포함)를 캐시에 남긴다.
    """

    def __init__(self, settings, directory: AccountDirectory, cache_path: Optional[Path] = None):
        self.settings = settings
        self.directory = directory
        self.cache_path = cache_path or (settings.accounts_path.parent / "accounts.cache.json")
        self.cache: Dict[str, Any] = {}
        if self.cache_path.exists():
            try:
                self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.cache = {}
        self.searched = 0

    # ------------------------------------------------------------------ 내부
    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(self.cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _search(self, name: str, context: str) -> Optional[Dict[str, Any]]:
        import anthropic

        client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        prompt = (
            "'{name}' 의 공식 인스타그램 계정을 찾아라.\n"
            "이 이름은 다음 기사 맥락에서 등장했다: {context}\n"
            "web_search로 확인한 뒤 emit_account 도구로 결과를 제출하라."
        ).format(name=name, context=context[:300])

        message = client.messages.create(
            model=self.settings.account_resolve_model,
            max_tokens=2000,
            system=RESOLVE_SYSTEM,
            tools=[
                {"type": "web_search_20250305", "name": "web_search", "max_uses": 4},
                RESOLVE_TOOL,
            ],
            messages=[{"role": "user", "content": prompt}],
        )
        for block in message.content:
            if getattr(block, "type", "") == "tool_use" and block.name == "emit_account":
                return dict(block.input)
        return None

    @staticmethod
    def _accept(result: Dict[str, Any]) -> Optional[str]:
        """등록해도 되는 결과인지 판정하고, 통과하면 handle을 돌려준다."""
        if not result.get("found") or result.get("confidence") != "high":
            return None
        match = _PROFILE_URL.match(str(result.get("profile_url", "")).strip())
        if not match:
            return None
        handle = str(result.get("handle", "")).strip().lstrip("@")
        # 모델이 말한 handle과 실제 프로필 URL의 handle이 일치할 때만 신뢰한다.
        if not handle or handle.lower() != match.group(2).lower():
            return None
        if not _HANDLE_RE.match(handle):
            return None
        return handle

    # ------------------------------------------------------------------ 공개 API
    def resolve(self, names: Sequence[str], context: str = "", limit: int = 5) -> List[Dict[str, Any]]:
        """미등록 이름들을 검색해 새로 등록된 항목 목록을 돌려준다."""
        if not self.settings.anthropic_api_key:
            return []

        added: List[Dict[str, Any]] = []
        for name in names:
            if len(added) >= limit or self.searched >= limit:
                break
            key = normalize(name)
            if not key or key in self.cache:
                continue  # 이미 찾아봤던 이름(성공/실패 모두)은 건너뛴다

            self.searched += 1
            try:
                result = self._search(name, context)
            except Exception as exc:
                log.warning("계정 검색 실패(%s): %s", name, exc)
                continue
            if result is None:
                log.debug("계정 검색 결과 없음: %s", name)
                self.cache[key] = {"name": name, "found": False, "checked_at": _now()}
                continue

            handle = self._accept(result)
            if handle is None:
                log.info(
                    "계정 미확정 → 태그하지 않음: %s (%s / %s)",
                    name, result.get("confidence"), result.get("reason", "")[:60],
                )
                self.cache[key] = {
                    "name": name, "found": False, "checked_at": _now(),
                    "reason": result.get("reason", ""),
                }
                continue

            entry = {
                "name": name,
                "handle": handle,
                "kind": "org",
                "source": result.get("profile_url"),
                "auto": True,
                "resolved_at": _now(),
                "note": result.get("reason", "")[:200],
            }
            if self.directory.add(entry) is not None:
                added.append(entry)
                log.info("계정 자동 등록: %s → @%s", name, handle)
            self.cache[key] = {"name": name, "found": True, "handle": handle, "checked_at": _now()}

        if added:
            self.directory.save()
        self._save_cache()
        return added


def _now() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")
