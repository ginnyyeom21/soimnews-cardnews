"""파이프라인 단계별 예외. 실패 신호를 단계 단위로 구분하기 위해 분리한다."""
from __future__ import annotations


class PipelineError(Exception):
    """모든 파이프라인 오류의 베이스."""

    stage = "pipeline"


class CollectError(PipelineError):
    """RSS/스크래핑 실패 또는 수집 건수 미달."""

    stage = "collect"


class SummarizeError(PipelineError):
    """AI 요약 생성 실패 또는 스키마 위반."""

    stage = "summarize"


class RenderError(PipelineError):
    """카드 이미지 생성 실패."""

    stage = "render"


class PublishError(PipelineError):
    """인스타그램 발행 실패 또는 필수 설정 누락."""

    stage = "publish"
