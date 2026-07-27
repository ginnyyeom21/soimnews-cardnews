"""명령행 진입점.

  python -m socialcard run --dry-run
  python -m socialcard run --source csv --csv latest_articles.csv --limit 1 --dry-run
  python -m socialcard history
  python -m socialcard seed --from-csv latest_articles.csv
  python -m socialcard doctor
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

from .config import load_settings, missing_publish_config
from .pipeline import new_run_id, run_pipeline
from .store import Store


def _setup_logging(verbose: bool, log_file: Optional[Path]) -> None:
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def cmd_run(args: argparse.Namespace) -> int:
    settings = load_settings(Path(args.env) if args.env else None)
    if args.limit:
        settings.article_count = args.limit
    if args.min_articles is not None:
        settings.min_articles = args.min_articles
    if args.provider:
        settings.ai_provider = args.provider
    if args.target:
        settings.ig_publish_target = args.target

    run_id = args.run_id or new_run_id()
    _setup_logging(args.verbose, settings.out_dir / run_id / "run.log")

    report = run_pipeline(
        settings,
        source=args.source,
        csv_path=Path(args.csv) if args.csv else None,
        limit=settings.article_count,
        dry_run=args.dry_run,
        force=args.force,
        run_id=run_id,
        resolve_accounts=not args.no_resolve,
    )

    print("\n" + report.summary_line())
    if report.error:
        print("오류: {}".format(report.error))
    for warning in report.warnings:
        print("주의: {}".format(warning))
    if report.overridden:
        print("커버 문구를 손으로 덮어쓴 기사 {}건: {}".format(
            len(report.overridden), ", ".join(report.overridden)
        ))
    if report.linkinbio_page:
        print("링크인바이오 페이지: {}".format(report.linkinbio_page))
    if report.resolved_accounts:
        print("웹 검색으로 새로 등록한 계정 {}건:".format(len(report.resolved_accounts)))
        for entry in report.resolved_accounts:
            print("  {} → @{}  ({})".format(entry["name"], entry["handle"], entry.get("source", "")))
    if report.unmatched_entities:
        print(
            "계정 미등록 기관/인물 {}건 → config/accounts.json 에 추가하면 다음 실행부터 태그됩니다:".format(
                len(report.unmatched_entities)
            )
        )
        print("  " + ", ".join(report.unmatched_entities[:20]))
    print("결과물: {}".format(report.out_dir))
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return report.exit_code


def cmd_history(args: argparse.Namespace) -> int:
    settings = load_settings(Path(args.env) if args.env else None)
    with Store(settings.db_path) as store:
        runs = store.recent_runs(args.limit)
        if not runs:
            print("실행 이력이 없습니다.")
            return 0
        print("{:<22} {:<9} {:<8} {:<7} {:>4} {:>4} {:>4} {:>4}".format(
            "RUN ID", "STATUS", "MODE", "SOURCE", "수집", "중복", "발행", "실패"))
        for row in runs:
            print("{:<22} {:<9} {:<8} {:<7} {:>4} {:>4} {:>4} {:>4}".format(
                row["run_id"], row["status"], row["mode"], row["source"],
                row["collected"], row["skipped"], row["published"], row["failed"]))
            if row["error"]:
                print("    └ 오류: {}".format(row["error"]))
        if args.run_id:
            print("\n[{}] 항목별 로그".format(args.run_id))
            for item in store.run_items(args.run_id):
                print("  {:<18} {} {}".format(item["status"], item["article_url"], item["detail"] or ""))
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    """이미 수동 발행한 기사를 '발행됨'으로 등록해 중복 발행을 막는다."""
    settings = load_settings(Path(args.env) if args.env else None)
    urls: List[str] = list(args.url or [])
    titles = {}
    if args.from_csv:
        import csv as _csv

        with Path(args.from_csv).open(encoding="utf-8-sig", newline="") as fh:
            for row in _csv.DictReader(fh):
                url = (row.get("url") or "").strip()
                if url:
                    urls.append(url)
                    titles[url] = (row.get("title") or "").strip()
    if args.limit:
        urls = urls[: args.limit]
    if not urls:
        print("등록할 URL이 없습니다. --url 또는 --from-csv 를 지정하세요.")
        return 2

    run_id = "seed-" + new_run_id()
    with Store(settings.db_path) as store:
        for url in urls:
            store.mark_published(
                article_url=url,
                article_id="SEED",
                title=titles.get(url, "(수동 등록)"),
                published_at="",
                run_id=run_id,
                mode="seed",
            )
    print("발행 이력에 {}건 등록했습니다. (run_id={})".format(len(urls), run_id))
    return 0


def cmd_accounts(args: argparse.Namespace) -> int:
    """인스타그램 계정 매핑 조회/추가."""
    from .accounts import load_directory

    settings = load_settings(Path(args.env) if args.env else None)
    directory = load_directory(settings.accounts_path)

    if args.name and args.handle:
        handle = args.handle.strip().lstrip("@")
        entry = {
            "name": args.name.strip(),
            "handle": handle,
            "kind": args.kind,
            "aliases": args.alias or [],
        }
        if args.source:
            entry["source"] = args.source
        if directory.add(entry) is None:
            print("계정 형식이 올바르지 않습니다: @{}".format(handle))
            return 2
        directory.save()
        print("등록 완료: {} → @{}".format(entry["name"], handle))
        return 0

    if not len(directory):
        print("등록된 계정이 없습니다. ({})".format(settings.accounts_path))
        return 0
    print("등록 계정 {}건 ({})".format(len(directory), settings.accounts_path))
    for entry in directory.entries:
        mark = "자동" if entry.get("auto") else "수동"
        print("  [{}] {:<24} @{}".format(mark, entry.get("name", ""), entry.get("handle", "")))
        if entry.get("source"):
            print("       근거: {}".format(entry["source"]))
    return 0


def cmd_overrides(args: argparse.Namespace) -> int:
    """커버 문구 덮어쓰기 조회 / 실행 결과에서 템플릿 만들기."""
    import json

    from .overrides import load_overrides, write_template

    settings = load_settings(Path(args.env) if args.env else None)
    path = settings.overrides_path

    if args.from_run:
        run_dir = settings.out_dir / args.from_run
        if not run_dir.is_dir():
            print("실행 결과를 찾을 수 없습니다: {}".format(run_dir))
            return 2
        rows = []
        for card_path in sorted(run_dir.glob("*.json")):
            data = json.loads(card_path.read_text(encoding="utf-8"))
            if "headline" not in data:
                continue  # report.json 등 카드가 아닌 파일
            rows.append(
                {
                    "article_id": data["article"]["article_id"],
                    "url": data["article"]["url"],
                    "headline": data["headline"],
                    "hook": data["hook"],
                    "read_more": data.get("read_more", ""),
                }
            )
        if not rows:
            print("카드 JSON이 없습니다: {}".format(run_dir))
            return 2
        added = write_template(path, rows)
        print("{}: 기사 {}건 중 {}건을 새로 추가했습니다.".format(path, len(rows), added))
        if added:
            print("현재 문구가 채워져 있으니 고칠 줄만 손보세요. 빈 칸은 덮어쓰지 않습니다.")
        else:
            print("모두 이미 파일에 있습니다.")
        return 0

    book = load_overrides(path)
    if not len(book):
        print("덮어쓸 문구가 없습니다. ({})".format(path))
        print("드라이런 뒤 다음으로 템플릿을 만드세요:")
        print("  python -m socialcard overrides --from-run <run-id>")
        return 0

    seen = set()
    print("덮어쓰기 {}건 ({})".format(len(set(map(id, book.rows.values()))), path))
    for key, row in book.rows.items():
        if id(row) in seen:
            continue  # 같은 줄이 url·article_id 두 키로 들어있다
        seen.add(id(row))
        print("  {}".format(row.get("article_id") or row.get("url")))
        for field in ("headline", "hook", "read_more"):
            if row.get(field):
                print("    {:<10} {}".format(field, row[field]))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = load_settings(Path(args.env) if args.env else None)
    ok = True
    print("■ 설정 점검")
    print("  AI 제공자        : {}{}".format(
        settings.ai_provider,
        "  (ANTHROPIC_API_KEY 없음 → 규칙 기반 요약으로 동작)" if settings.ai_provider == "rule" else "",
    ))
    print("  발행 어댑터      : {} / 대상 {}".format(settings.publisher, settings.ig_publish_target))
    missing = missing_publish_config(settings)
    if missing:
        print("  실발행 설정      : 누락 {} → --dry-run 만 가능".format(", ".join(missing)))
    else:
        print("  실발행 설정      : OK")
    print("  발행 계정        : {}".format(settings.brand_handle))
    print("  바이라인         : {}  {}".format(settings.brand_name, settings.brand_email))
    print("  로고             : {}{}".format(
        settings.logo_path, "" if settings.logo_path.exists() else "  (파일 없음 → 마크 생략)"))
    print("  DB               : {}".format(settings.db_path))
    print("  결과물 디렉터리  : {}".format(settings.out_dir))

    print("■ 폰트")
    try:
        from .render import load_font

        for weight in ("bold", "medium", "regular"):
            font = load_font(weight, 40)
            print("  {:<8}: {}".format(weight, getattr(font, "path", "?")))
    except Exception as exc:
        ok = False
        print("  폰트 로드 실패: {}".format(exc))

    print("■ 계정 매핑")
    from .accounts import load_directory

    directory = load_directory(settings.accounts_path)
    print("  등록 계정 {}건 ({})".format(len(directory), settings.accounts_path))
    if directory.load_error:
        ok = False
        print("  ✗ {}".format(directory.load_error))
        print("    JSON 문법을 확인하세요. 항목 사이 쉼표 하나가 빠져도 전체가 무시됩니다.")
        print("    손으로 고치는 대신 accounts 명령으로 추가하면 이런 오류가 없습니다.")

    if not args.offline:
        print("■ RSS 연결")
        try:
            from .collect import fetch_rss_entries

            entries = fetch_rss_entries(settings)
            print("  OK · {}건 · 최신: {}".format(len(entries), entries[0]["title"][:40]))
        except Exception as exc:
            ok = False
            print("  실패: {}".format(exc))

    print("\n결과: {}".format("정상" if ok else "확인 필요"))
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="socialcard", description="소셜임팩트뉴스 인스타그램 카드뉴스 자동 발행"
    )
    parser.add_argument("--env", help="설정 파일 경로 (기본: config/settings.env)")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="수집→요약→카드생성→발행 실행")
    run.add_argument("--dry-run", action="store_true", help="실제 발행 없이 카드/캡션까지만 생성")
    run.add_argument("--source", choices=["rss", "csv"], default="rss")
    run.add_argument("--csv", help="--source csv 일 때 읽을 CSV 경로")
    run.add_argument("--limit", type=int, help="처리할 기사 수 (기본 10)")
    run.add_argument("--min-articles", type=int, help="이 건수 미만이면 실패 처리 (기본 1)")
    run.add_argument("--provider", choices=["anthropic", "rule"], help="요약 방식 강제 지정")
    run.add_argument("--target", choices=["carousel", "story"], help="게시물 형태")
    run.add_argument("--force", action="store_true", help="중복 검사 무시하고 다시 발행")
    run.add_argument(
        "--no-resolve", action="store_true", help="미등록 기관의 인스타 계정 웹 검색을 건너뜀"
    )
    run.add_argument("--run-id", help="실행 ID 직접 지정")
    run.add_argument("--json", action="store_true", help="리포트 JSON을 표준출력에 함께 출력")
    run.add_argument("-v", "--verbose", action="store_true")
    run.set_defaults(func=cmd_run)

    history = sub.add_parser("history", help="실행 이력 조회")
    history.add_argument("--limit", type=int, default=10)
    history.add_argument("--run-id", help="해당 실행의 기사별 로그도 출력")
    history.set_defaults(func=cmd_history)

    seed = sub.add_parser("seed", help="이미 발행한 기사 URL을 발행 이력에 등록")
    seed.add_argument("--url", action="append", help="여러 번 지정 가능")
    seed.add_argument("--from-csv", help="url 컬럼이 있는 CSV에서 일괄 등록")
    seed.add_argument("--limit", type=int)
    seed.set_defaults(func=cmd_seed)

    accounts = sub.add_parser("accounts", help="인스타그램 계정 매핑 조회/추가")
    accounts.add_argument("--name", help="기관/인물 이름")
    accounts.add_argument("--handle", help="인스타그램 계정 (@ 없이)")
    accounts.add_argument("--kind", choices=["org", "person"], default="org")
    accounts.add_argument("--alias", action="append", help="기사에서 달리 쓰일 수 있는 표기")
    accounts.add_argument("--source", help="계정을 확인한 근거 URL")
    accounts.set_defaults(func=cmd_accounts)

    overrides = sub.add_parser("overrides", help="커버 문구를 손으로 덮어쓰기")
    overrides.add_argument(
        "--from-run", help="이 실행 결과의 현재 문구로 템플릿 만들기 (예: --from-run rule-v3)"
    )
    overrides.set_defaults(func=cmd_overrides)

    doctor = sub.add_parser("doctor", help="설정/폰트/RSS 상태 점검")
    doctor.add_argument("--offline", action="store_true", help="네트워크 점검 생략")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "run":
        _setup_logging(False, None)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
