"""수집 → 요약 → 카드 생성 → 발행 전체 흐름."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .accounts import AccountResolver, load_directory
from .collect import collect_from_csv, collect_from_rss
from .config import Settings
from .errors import CollectError, PipelineError
from .linkinbio import write_page as write_linkinbio
from .models import Article, PublishResult
from .overrides import load_overrides
from .publish import build_image_urls, make_publisher, notify
from .render import render_cardnews
from .store import Store
from .summarize import apply_directory, build_cardnews

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))


@dataclass
class RunReport:
    run_id: str
    status: str  # success | partial | failed
    mode: str  # live | dry-run
    source: str
    started_at: str
    collected: int = 0
    skipped_duplicate: int = 0
    processed: int = 0
    published: int = 0
    failed: int = 0
    error: Optional[str] = None
    items: List[Dict[str, Any]] = field(default_factory=list)
    unmatched_entities: List[str] = field(default_factory=list)
    resolved_accounts: List[Dict[str, Any]] = field(default_factory=list)
    overridden: List[str] = field(default_factory=list)  # 사람이 커버 문구를 덮어쓴 기사
    linkinbio_page: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    out_dir: Optional[str] = None

    @property
    def exit_code(self) -> int:
        return {"success": 0, "partial": 1, "failed": 2}.get(self.status, 2)

    def to_dict(self) -> Dict[str, Any]:
        data = dict(self.__dict__)
        data["exit_code"] = self.exit_code
        return data

    def summary_line(self) -> str:
        return (
            "[{run_id}] {status} | 모드={mode} 소스={source} | "
            "수집 {collected} · 중복스킵 {skipped} · 카드생성 {processed} · 발행 {published} · 실패 {failed}"
        ).format(
            run_id=self.run_id,
            status=self.status.upper(),
            mode=self.mode,
            source=self.source,
            collected=self.collected,
            skipped=self.skipped_duplicate,
            processed=self.processed,
            published=self.published,
            failed=self.failed,
        )


def new_run_id() -> str:
    return datetime.now(KST).strftime("run-%Y%m%d-%H%M%S")


def _collect(settings: Settings, source: str, csv_path: Optional[Path], limit: int) -> List[Article]:
    if source == "csv":
        if csv_path is None:
            raise CollectError("--source csv 를 쓰려면 --csv 경로가 필요합니다")
        return collect_from_csv(csv_path, settings, limit)
    return collect_from_rss(settings, limit)


def run_pipeline(
    settings: Settings,
    source: str = "rss",
    csv_path: Optional[Path] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
    force: bool = False,
    run_id: Optional[str] = None,
    resolve_accounts: bool = True,
) -> RunReport:
    limit = limit or settings.article_count
    run_id = run_id or new_run_id()
    mode = "dry-run" if dry_run else "live"
    out_dir = settings.out_dir / run_id
    report = RunReport(
        run_id=run_id,
        status="failed",
        mode=mode,
        source=source,
        started_at=datetime.now(KST).isoformat(timespec="seconds"),
        out_dir=str(out_dir),
    )

    store = Store(settings.db_path)
    store.start_run(run_id, mode, source)
    log.info("실행 시작 %s (모드=%s, 소스=%s, 목표 %d건)", run_id, mode, source, limit)

    try:
        # 발행기를 먼저 만든다. 자격증명이 없으면 카드를 만들기 전에 즉시 중단된다.
        publisher = make_publisher(settings, dry_run)

        articles = _collect(settings, source, csv_path, limit)
        report.collected = len(articles)
        log.info("기사 %d건 수집 완료", len(articles))

        if settings.ai_provider == "rule":
            report.warnings.append(
                "ANTHROPIC_API_KEY가 없어 폴백 요약으로 실행했습니다. "
                "기사에 있는 사실만 커버로 끌어올리므로 모집·공모 기사 외에는 "
                "헤드라인이 기사 제목에 가깝습니다."
            )
            log.warning("%s", report.warnings[-1])

        directory = load_directory(settings.accounts_path)
        if directory.load_error:
            # 로그로만 남기면 묻힌다. 쉼표 하나로 태그 전체가 조용히 꺼지는 상황이라 요약에 올린다.
            report.warnings.append(directory.load_error)
            log.warning("%s", directory.load_error)
        overrides = load_overrides(settings.overrides_path)
        resolver: Optional[AccountResolver] = None
        if resolve_accounts and settings.account_auto_resolve and settings.anthropic_api_key:
            resolver = AccountResolver(settings, directory)
        out_dir.mkdir(parents=True, exist_ok=True)

        pending: List[Article] = []
        for article in articles:
            if not force and store.is_published(article.url):
                report.skipped_duplicate += 1
                store.log_item(run_id, article.url, article.article_id, "skipped_duplicate")
                report.items.append(
                    PublishResult(
                        article_url=article.url,
                        status="skipped_duplicate",
                        detail="이미 발행된 기사",
                    ).to_dict()
                )
                log.info("중복 스킵: %s", article.title)
            else:
                pending.append(article)

        for position, article in enumerate(pending, start=1):
            try:
                cardnews = build_cardnews(article, settings, directory)

                # 사람이 적어둔 문구가 있으면 무엇으로 만들었든 그것이 최종본이다.
                changed = overrides.apply(cardnews)
                if changed:
                    report.overridden.append(article.article_id)
                    log.info("커버 문구 덮어씀(%s): %s", ", ".join(changed), article.article_id)

                # 매핑에 없는 기관은 웹 검색으로 공식 계정을 찾아 등록한 뒤 다시 태그한다.
                if resolver is not None and cardnews.unmatched_entities:
                    added = resolver.resolve(
                        cardnews.unmatched_entities,
                        context=article.title,
                        limit=settings.account_resolve_budget,
                    )
                    if added:
                        report.resolved_accounts.extend(added)
                        apply_directory(cardnews, directory, exclude_handle=settings.brand_handle)

                cardnews.link_url = settings.linkinbio_base_url or ""
                render_cardnews(cardnews, out_dir, settings)
                image_urls = build_image_urls(cardnews, settings.out_dir, settings)

                result = publisher.publish(cardnews, image_urls)
                (out_dir / "{}.json".format(article.article_id)).write_text(
                    json.dumps(cardnews.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
                )

                if result.status == "published":
                    report.published += 1
                    store.mark_published(
                        article_url=article.url,
                        article_id=article.article_id,
                        title=article.title,
                        published_at=article.published_at,
                        run_id=run_id,
                        mode=mode,
                        media_id=result.media_id,
                        permalink=result.permalink,
                    )
                store.log_item(
                    run_id, article.url, article.article_id, result.status, result.media_id, result.detail
                )
                item = result.to_dict()
                item.update({"title": article.title, "headline": cardnews.headline, "cards": len(cardnews.cards)})
                report.items.append(item)
                report.unmatched_entities.extend(cardnews.unmatched_entities)
                log.info("[%d/%d] %s — %s", position, len(pending), result.status, article.title)

            except PipelineError as exc:
                report.failed += 1
                store.log_item(run_id, article.url, article.article_id, "failed", None, str(exc))
                report.items.append(
                    PublishResult(
                        article_url=article.url, status="failed", detail="{}: {}".format(exc.stage, exc)
                    ).to_dict()
                )
                log.error("[%d/%d] 실패(%s): %s — %s", position, len(pending), exc.stage, article.title, exc)

            if not dry_run and position < len(pending) and settings.publish_delay_seconds > 0:
                time.sleep(settings.publish_delay_seconds)  # API 레이트리밋 여유

        # 드라이런에서는 카드까지 만들어진 건(dry_run)을 성공 처리한다.
        report.processed = sum(
            1 for item in report.items if item["status"] in ("published", "dry_run")
        )
        if report.processed == 0 and report.skipped_duplicate == 0:
            report.status = "failed"
            report.error = "처리된 기사가 0건입니다"
        elif report.failed:
            report.status = "partial"
        else:
            report.status = "success"

        # 프로필 바이오에 걸어둘 랜딩 페이지를 최신 발행 목록으로 다시 만든다.
        page = write_linkinbio(settings, store, settings.out_dir / "linkinbio")
        if page is not None:
            report.linkinbio_page = str(page)

        report.unmatched_entities = sorted(set(report.unmatched_entities))
        store.finish_run(
            run_id,
            report.status,
            collected=report.collected,
            skipped=report.skipped_duplicate,
            published=report.published,
            failed=report.failed,
            error=report.error,
            detail={
                "unmatched_entities": report.unmatched_entities,
                "resolved_accounts": report.resolved_accounts,
                "overridden": report.overridden,
                "out_dir": str(out_dir),
            },
        )

    except PipelineError as exc:
        report.status = "failed"
        report.error = "{}: {}".format(exc.stage, exc)
        store.finish_run(run_id, "failed", collected=report.collected, error=report.error)
        log.error("실행 중단(%s): %s", exc.stage, exc)
        notify(settings, "[소셜임팩트뉴스 카드뉴스] 실행 실패 {}".format(run_id), report.error)
    except Exception as exc:  # 예상 못한 오류도 실행 로그에 남긴다
        report.status = "failed"
        report.error = "unexpected: {}".format(exc)
        store.finish_run(run_id, "failed", collected=report.collected, error=report.error)
        log.exception("예상치 못한 오류")
        notify(settings, "[소셜임팩트뉴스 카드뉴스] 실행 실패 {}".format(run_id), report.error)
    finally:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "report.json").write_text(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            log.warning("리포트 파일 저장 실패")
        store.close()

    if report.status != "failed" and report.failed:
        notify(
            settings,
            "[소셜임팩트뉴스 카드뉴스] 일부 실패 {}".format(run_id),
            report.summary_line(),
            level="warning",
        )
    return report
