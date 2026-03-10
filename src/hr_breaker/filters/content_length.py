"""Content length checker - runs first to fail fast on oversized content."""
import fitz
from hr_breaker.config import get_settings, logger
from hr_breaker.filters.base import BaseFilter
from hr_breaker.filters.registry import FilterRegistry
from hr_breaker.models import FilterResult, JobPosting, OptimizedResume, ResumeSource
from hr_breaker.services.length_estimator import estimate_content_length
from hr_breaker.services.renderer import get_renderer, RenderError


def check_page2_overflow(pdf_bytes: bytes) -> str | None:
    settings = get_settings()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if len(doc) < 2:
        return None
    page2_text = doc[1].get_text().strip()
    if len(page2_text) > 0 and len(page2_text) < settings.resume_page2_overflow_chars:
        logger.debug(f"check_page2_overflow: page 2 len {len(page2_text)} - overflow from page 1")
        return f"Страница 2 содержит только {len(page2_text)} символов — контент не помещается на 1 страницу"
    return None


@FilterRegistry.register
class ContentLengthChecker(BaseFilter):
    name = "ContentLengthChecker"
    priority = 0
    threshold = 1.0

    async def evaluate(self, optimized: OptimizedResume, job: JobPosting, source: ResumeSource) -> FilterResult:
        if optimized.html is None:
            return FilterResult(filter_name=self.name, passed=True, score=1.0, threshold=self.threshold, issues=[], suggestions=[])

        try:
            renderer = get_renderer()
            render_result = renderer.render(optimized.html)
            page_count = render_result.page_count
            pdf_bytes = render_result.pdf_bytes
        except RenderError as e:
            return FilterResult(
                filter_name=self.name, passed=False, score=0.0, threshold=self.threshold,
                issues=[f"Ошибка рендеринга: {str(e)}"],
                suggestions=["Проверьте структуру HTML резюме"],
            )

        if page_count > 2:
            return FilterResult(
                filter_name=self.name, passed=False, score=0.0, threshold=self.threshold,
                issues=[f"Резюме занимает {page_count} страницы — должно быть не более 1 страницы"],
                suggestions=["Сократите содержимое чтобы уложиться в 1 страницу"],
            )

        if page_count == 2:
            overflow_issue = check_page2_overflow(pdf_bytes)
            if overflow_issue:
                return FilterResult(
                    filter_name=self.name, passed=False, score=0.0, threshold=self.threshold,
                    issues=[overflow_issue],
                    suggestions=["Сократите содержимое — текст не помещается на 1 страницу"],
                )

        return FilterResult(filter_name=self.name, passed=True, score=1.0, threshold=self.threshold, issues=[], suggestions=[])
