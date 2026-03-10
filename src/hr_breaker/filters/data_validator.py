"""Validates OptimizedResume completeness before rendering."""
import re
from hr_breaker.filters.base import BaseFilter
from hr_breaker.filters.registry import FilterRegistry
from hr_breaker.models import FilterResult, JobPosting, OptimizedResume, ResumeSource


def validate_html(html: str) -> tuple[bool, list[str]]:
    issues = []
    if not re.search(r'<header[^>]*class="header"', html):
        issues.append("Отсутствует элемент header с class='header'")
    elif not re.search(r'<h1[^>]*class="name"[^>]*>', html):
        issues.append("Отсутствует имя (h1 с class='name') в заголовке")
    if not re.search(r'<section[^>]*class="section"', html):
        issues.append("В резюме нет разделов с содержимым")
    if re.search(r'<script', html, re.IGNORECASE):
        issues.append("Script-теги не допускаются")
    return len(issues) == 0, issues


def validate_resume_data(optimized: OptimizedResume) -> tuple[bool, list[str]]:
    issues = []
    data = optimized.data
    if data is None:
        issues.append("Данные резюме отсутствуют")
        return False, issues
    if not data.contact.name:
        issues.append("Отсутствует имя в контактах")
    if not data.contact.email:
        issues.append("Отсутствует email в контактах")
    has_content = any([data.summary, data.experience, data.education, data.skills, data.projects, data.certifications, data.publications])
    if not has_content:
        issues.append("В резюме нет разделов с содержимым")
    for i, exp in enumerate(data.experience):
        if not exp.company:
            issues.append(f"Опыт #{i+1}: отсутствует название компании")
        if not exp.title:
            issues.append(f"Опыт #{i+1}: отсутствует должность")
        if not exp.start_date:
            issues.append(f"Опыт #{i+1}: отсутствует дата начала")
    for i, edu in enumerate(data.education):
        if not edu.institution:
            issues.append(f"Образование #{i+1}: отсутствует учебное заведение")
        if not edu.degree:
            issues.append(f"Образование #{i+1}: отсутствует степень")
    return len(issues) == 0, issues


@FilterRegistry.register
class DataValidator(BaseFilter):
    name = "DataValidator"
    priority = 1
    threshold = 1.0

    async def evaluate(self, optimized: OptimizedResume, job: JobPosting, source: ResumeSource) -> FilterResult:
        if optimized.html is not None:
            valid, issues = validate_html(optimized.html)
        elif optimized.data is not None:
            valid, issues = validate_resume_data(optimized)
        else:
            valid, issues = False, ["Содержимое резюме отсутствует"]
        score = 1.0 if valid else 0.0
        return FilterResult(
            filter_name=self.name, passed=score >= self.threshold, score=score, threshold=self.threshold,
            issues=issues,
            suggestions=["Исправьте обязательные поля в резюме"] if issues else [],
        )
