import asyncio
import subprocess
import sys
import nest_asyncio
import streamlit as st

nest_asyncio.apply()

# Event loop setup
if "event_loop" not in st.session_state:
    st.session_state.event_loop = asyncio.new_event_loop()
asyncio.set_event_loop(st.session_state.event_loop)

from hr_breaker.agents import extract_name, parse_job_posting
from hr_breaker.config import get_settings
from hr_breaker.models import GeneratedPDF, ResumeSource, ValidationResult, SUPPORTED_LANGUAGES, get_language
from hr_breaker.orchestration import optimize_for_job, translate_and_rerender
from hr_breaker.services import (
    PDFStorage,
    ResumeCache,
    scrape_job_posting,
    CloudflareBlockedError,
)
from hr_breaker.services.pdf_parser import load_resume_content_from_upload

# Initialize services
cache = ResumeCache()
pdf_storage = PDFStorage()
settings = get_settings()

st.set_page_config(page_title="К Собесу", page_icon="🎯", layout="wide")

st.markdown("""
<style>
header[data-testid="stHeader"] { display: none; }
#MainMenu { display: none; }
footer { display: none; }
.block-container { padding-top: 1rem !important; }
a[href^="#"] { display: none !important; }
h1 a, h2 a, h3 a { display: none !important; }
.stMarkdown a[data-testid="stMarkdownAnchorLink"] { display: none !important; }
[data-testid="stToolbar"] { display: none !important; }
[data-testid="stDecoration"] { display: none !important; }
[data-testid="stStatusWidget"] { display: none !important; }
.__web-inspector-hide-shortcut__ { display: none !important; }
</style>
""", unsafe_allow_html=True)

# Скрываем лишние элементы Streamlit
st.markdown("""
<style>
    header[data-testid="stHeader"] { display: none; }
    #MainMenu { display: none; }
    footer { display: none; }
    [data-testid="stToolbar"] { display: none; }
    [data-testid="stDecoration"] { display: none; }
</style>
""", unsafe_allow_html=True)


def run_async(coro):
    loop = st.session_state.event_loop
    return loop.run_until_complete(coro)


@st.cache_data(show_spinner=False)
def cached_scrape_job(url: str) -> str:
    return scrape_job_posting(url)


@st.cache_data(show_spinner=False)
def cached_extract_name(content: str) -> tuple[str | None, str | None]:
    return run_async(extract_name(content))


@st.cache_resource(show_spinner=False)
def cached_parse_job(text: str):
    return run_async(parse_job_posting(text))


FILTER_INFO = {
    "LLMChecker": {
        "name": "ATS-проверка",
        "fail_msg": "⚠️ Резюме не прошло автоматический отбор",
        "explanation": "Компании используют роботов (ATS) которые автоматически отсеивают резюме до того как их увидит человек. Твоё резюме не прошло эту проверку.",
        "advice": "Попробуй снова — каждый раз результат разный. Или добавь в инструкции: *«Пиши конкретно и по делу, без воды»*",
    },
    "AIGeneratedChecker": {
        "name": "Проверка на ИИ-текст",
        "fail_msg": "⚠️ Текст похож на сгенерированный ИИ",
        "explanation": "Некоторые HR-системы и рекрутеры отклоняют резюме которые звучат как написанные роботом — слишком гладко, шаблонно, без живой речи.",
        "advice": "Добавь в инструкции: *«Пиши живым разговорным языком, избегай канцеляризмов и шаблонных фраз»*",
    },
    "HallucinationChecker": {
        "name": "Проверка на выдумки",
        "fail_msg": "⚠️ Обнаружены факты которых не было в оригинале",
        "explanation": "ИИ иногда добавляет информацию которой не было в твоём оригинальном резюме — выдуманные достижения, навыки или места работы. Это опасно — на собесе могут спросить.",
        "advice": "Добавь в инструкции: *«Не добавляй ничего чего нет в оригинале, только перефразируй»*. Перед отправкой обязательно проверь PDF.",
    },
    "KeywordMatcher": {
        "name": "Ключевые слова",
        "fail_msg": "⚠️ Мало ключевых слов из вакансии",
        "explanation": "ATS-системы ищут в резюме конкретные слова из вакансии. Если их нет — резюме отсеивается автоматически, даже если ты идеально подходишь.",
        "advice": "Добавь в инструкции: *«Обязательно используй терминологию из вакансии»*. Или вставь текст вакансии полностью — чем больше деталей, тем лучше.",
    },
    "VectorSimilarityMatcher": {
        "name": "Соответствие вакансии",
        "fail_msg": "⚠️ Резюме слабо соответствует вакансии",
        "explanation": "Система оценила насколько твой опыт и навыки совпадают с требованиями вакансии. Совпадение недостаточное.",
        "advice": "Проверь — правильную ли вакансию ты вставил? Если да — попробуй добавить в инструкции конкретные навыки которые у тебя есть но не отражены в резюме.",
    },
    "DataValidator": {
        "name": "Структура резюме",
        "fail_msg": "⚠️ Ошибка структуры резюме",
        "explanation": "Возникла техническая проблема при создании резюме — структура документа некорректна.",
        "advice": "Попробуй запустить ещё раз. Если ошибка повторяется — попробуй другой формат резюме (например .txt вместо .pdf).",
    },
    "ContentLengthChecker": {
        "name": "Длина резюме",
        "fail_msg": "⚠️ Резюме слишком длинное",
        "explanation": "Резюме не помещается на одну страницу. Большинство рекрутеров и ATS-систем предпочитают резюме на 1 страницу.",
        "advice": "Добавь в инструкции: *«Сократи резюме до одной страницы, убери менее важный опыт»*",
    },
}

def display_filter_results(validation: ValidationResult):
    for result in validation.results:
        if result.passed:
            continue  # Не показываем пройденные — только проблемы
        info = FILTER_INFO.get(result.filter_name, {})
        name = info.get("name", result.filter_name)
        fail_msg = info.get("fail_msg", f"❌ {name}")
        explanation = info.get("explanation", "")
        advice = info.get("advice", "")

        with st.expander(fail_msg, expanded=True):
            if explanation:
                st.write(explanation)
            if advice:
                st.info(f"💡 **Что делать:** {advice}")
            if result.issues:
                with st.expander("Технические детали", expanded=False):
                    for issue in result.issues:
                        st.write(f"- {issue}")


# Sidebar
with st.sidebar:
    st.markdown("**Настройки**")
    sequential_mode = False
    debug_mode = False
    no_shame_mode = st.checkbox(
        "Агрессивная оптимизация",
        value=False,
        help="ИИ будет активнее переформулировать твоё резюме. Результат может сильно отличаться от оригинала — проверь PDF перед отправкой.",
    )

    _lang_options = [lang.code for lang in SUPPORTED_LANGUAGES]
    _lang_labels = {lang.code: lang.native_name for lang in SUPPORTED_LANGUAGES}
    _default_lang_idx = (
        _lang_options.index("ru")
        if "ru" in _lang_options
        else _lang_options.index(settings.default_language)
        if settings.default_language in _lang_options
        else 0
    )
    selected_lang_code = st.selectbox(
        "Язык резюме",
        options=_lang_options,
        index=_default_lang_idx,
        format_func=lambda code: _lang_labels[code],
        help="Язык итогового резюме. Оптимизация идёт на английском, затем переводится.",
    )
    selected_language = get_language(selected_lang_code)

    max_iterations = st.number_input(
        "Максимум итераций", min_value=1, max_value=10, value=2
    )

# Main content
st.markdown("### 🎯 К Собесу")
st.markdown("<p style='margin-bottom: 8px; color: #555;'>Адаптируем твоё резюме под конкретную вакансию. Поможем обойти все ИИ HR-фильтры.</p>", unsafe_allow_html=True)

# Two main columns: Resume | Job
col_resume, col_job = st.columns(2)

has_resume = "source_resume" in st.session_state

with col_resume:
    resume_header = "**Резюме ✓**" if has_resume else "**Резюме**"
    st.markdown(resume_header)
    if not has_resume:
        st.caption("Загрузи своё текущее резюме в любом формате — PDF, Word, TXT")

    if has_resume:
        src = st.session_state["source_resume"]
        name = f"{src.first_name or ''} {src.last_name or ''}".strip() or "Неизвестно"
        c1, c2 = st.columns([4, 1])
        with c1:
            st.success(f"✓ {name}")
        with c2:
            if st.button("Изменить", key="clear_resume"):
                st.session_state.pop("source_resume", None)
                st.session_state.pop("last_result", None)
                st.session_state["resume_uploader_key"] = (
                    st.session_state.get("resume_uploader_key", 0) + 1
                )
                st.session_state["resume_cleared"] = True
                st.rerun()
        with st.expander("Предпросмотр", expanded=False):
            st.text(src.content)
    else:
        resume_method = st.radio(
            "Способ загрузки резюме",
            ["Загрузить файл", "Вставить текст"],
            horizontal=True,
            key="resume_method",
            label_visibility="collapsed",
        )

        resume_content = None
        if resume_method == "Загрузить файл":
            uploader_key = (
                f"resume_uploader_{st.session_state.get('resume_uploader_key', 0)}"
            )
            uploaded_file = st.file_uploader(
                "Загрузить (.tex, .md, .txt, .pdf)",
                type=["tex", "md", "txt", "pdf"],
                label_visibility="collapsed",
                key=uploader_key,
            )
            if uploaded_file:
                resume_content = load_resume_content_from_upload(
                    uploaded_file.name, uploaded_file.read()
                )
        else:
            pasted_resume = st.text_area(
                "Вставить резюме",
                height=100,
                label_visibility="collapsed",
                placeholder="Вставьте текст резюме...",
            )
            if pasted_resume:
                resume_content = pasted_resume

        if resume_content:
            with st.spinner("Загрузка..."):
                first_name, last_name = cached_extract_name(resume_content)
            source = ResumeSource(
                content=resume_content, first_name=first_name, last_name=last_name
            )
            cache.put(source)
            st.session_state["source_resume"] = source
            st.session_state.pop("resume_cleared", None)
            st.rerun()

with col_job:
    job_text = st.session_state.get("job_text", "")
    has_job = bool(job_text)
    job_header = "**Вакансия ✓**" if has_job else "**Вакансия**"
    st.markdown(job_header)
    if not has_job:
        st.caption("Вставь ссылку на вакансию с HH.ru или любого другого сайта")

    if has_job:
        preview = (
            job_text[:80].replace("\n", " ") + "..."
            if len(job_text) > 80
            else job_text.replace("\n", " ")
        )
        c1, c2 = st.columns([4, 1])
        with c1:
            st.success(f"✓ {preview}")
        with c2:
            if st.button("Изменить", key="clear_job"):
                st.session_state.pop("job_text", None)
                st.session_state.pop("last_job_url", None)
                st.session_state.pop("last_result", None)
                st.rerun()
        with st.expander("Предпросмотр", expanded=False):
            st.text(job_text)
    else:
        job_input_method = st.radio(
            "Способ ввода вакансии",
            ["Ссылка", "Вставить текст"],
            horizontal=True,
            key="job_method",
            label_visibility="collapsed",
        )

        if job_input_method == "Ссылка":
            job_url = st.text_input(
                "Ссылка на вакансию", label_visibility="collapsed", placeholder="https://..."
            )

            if job_url and job_url != st.session_state.get("last_job_url"):
                st.session_state["last_job_url"] = job_url
                with st.spinner("Загружаем вакансию..."):
                    try:
                        job_text = cached_scrape_job(job_url)
                        st.session_state["job_text"] = job_text
                        st.session_state.pop("scrape_failed_url", None)
                        st.rerun()
                    except CloudflareBlockedError:
                        st.session_state["scrape_failed_url"] = job_url
                        st.warning("Сайт защищён от ботов. Скопируй текст вакансии вручную.")
                    except Exception as e:
                        st.error(f"Ошибка: {e}")

            if st.session_state.get("scrape_failed_url"):
                st.markdown(
                    f"[Открыть вакансию в браузере]({st.session_state['scrape_failed_url']})"
                )
        else:
            pasted_job = st.text_area(
                "Вставить вакансию",
                height=100,
                label_visibility="collapsed",
                placeholder="Вставьте текст вакансии...",
            )
            if pasted_job:
                st.session_state["job_text"] = pasted_job
                st.session_state.pop("scrape_failed_url", None)
                st.rerun()

# User instructions
if "user_instructions" not in st.session_state:
    st.session_state["user_instructions"] = ""
user_instructions = st.text_area(
    "Дополнительные инструкции (необязательно)",
    placeholder="Например: сделай акцент на управлении командой, я перехожу из маркетинга в продакты...",
    help="Напиши пожелания для ИИ — например: «сделай акцент на управлении командой» или «я перехожу из маркетинга в продакты»",
    key="user_instructions",
)
st.caption("💡 Необязательно, но помогает получить более точный результат")

# Optimize button
is_running = st.session_state.get("optimization_running", False)
can_optimize = has_resume and has_job and not is_running
btn_help = None
if not has_resume:
    btn_help = "Загрузи резюме"
elif not has_job:
    btn_help = "Добавь вакансию"
elif is_running:
    btn_help = "Оптимизация в процессе..."
clicked = st.button(
    "🚀 Оптимизировать резюме", disabled=not can_optimize, use_container_width=True, help=btn_help
)

if clicked:
    source = st.session_state["source_resume"]
    instructions_value = user_instructions.strip() if user_instructions else None
    if instructions_value != source.instructions:
        source = source.model_copy(update={"instructions": instructions_value})
        cache.put(source)
        st.session_state["source_resume"] = source
    st.session_state["optimization_running"] = True
    error_occurred = None

    try:
        with st.spinner("Анализируем вакансию..."):
            job = cached_parse_job(job_text)

        debug_dir = None
        if debug_mode:
            debug_dir = pdf_storage.generate_debug_dir(job.company, job.title)

        iteration_results = []

        with st.status("Оптимизируем резюме...", expanded=True) as status_container:

            def on_iteration(i, opt, val):
                iteration_results.append((i, opt, val))
                status_container.update(label=f"Итерация {i + 1}/{max_iterations}")
                status_container.write(f"Итерация {i + 1} завершена")

                if debug_mode and debug_dir:
                    if opt.html:
                        (debug_dir / f"iteration_{i + 1}.html").write_text(opt.html, encoding="utf-8")
                    if opt.pdf_bytes:
                        (debug_dir / f"iteration_{i + 1}.pdf").write_bytes(opt.pdf_bytes)

            def on_translation_status(msg):
                status_container.update(label="Финальная обработка...")

            target_lang = selected_language if selected_language.code != "en" else None

            optimized, validation, job = run_async(
                optimize_for_job(
                    source,
                    job_text,
                    max_iterations=max_iterations,
                    on_iteration=on_iteration,
                    job=job,
                    parallel=not sequential_mode,
                    no_shame=no_shame_mode,
                    user_instructions=instructions_value,
                    language=target_lang,
                    on_translation_status=on_translation_status,
                )
            )
            status_container.update(label="Готово!", state="complete")

        pdf_path = None
        if optimized and optimized.pdf_bytes:
            pdf_path = pdf_storage.generate_path(
                source.first_name, source.last_name, job.company, job.title,
                lang_code=selected_lang_code,
            )
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            pdf_path.write_bytes(optimized.pdf_bytes)

            pdf_record = GeneratedPDF(
                path=pdf_path,
                source_checksum=source.checksum,
                company=job.company,
                job_title=job.title,
                first_name=source.first_name,
                last_name=source.last_name,
            )
            pdf_storage.save_record(pdf_record)

        st.session_state["last_result"] = {
            "optimized": optimized,
            "validation": validation,
            "job": job,
            "iterations": iteration_results,
            "pdf_path": pdf_path,
            "debug_dir": debug_dir,
        }
    except Exception as e:
        error_occurred = e
    finally:
        st.session_state["optimization_running"] = False

    if error_occurred:
        st.error(f"Ошибка оптимизации: {error_occurred}")
    else:
        st.rerun()

# Display last result if exists
if "last_result" in st.session_state:
    result = st.session_state["last_result"]
    optimized = result["optimized"]
    validation = result["validation"]
    job = result["job"]
    iterations = result["iterations"]
    pdf_path = result["pdf_path"]
    debug_dir = result["debug_dir"]

    st.markdown("---")
    st.markdown(f"### Результат: {job.title} — {job.company}")

    if validation.passed:
        st.success("✅ Все проверки пройдены!")
    else:
        passed = [r.filter_name for r in validation.results if r.passed]
        failed = [r.filter_name for r in validation.results if not r.passed]
        st.warning(
            f"Достигнут лимит итераций ({len(passed)}/{len(validation.results)} проверок пройдено)."
        )

    # PDF download
    if pdf_path:
        st.success("✅ Резюме оптимизировано!")
        with open(pdf_path, "rb") as f:
            st.download_button(
                label="⬇️ Скачать PDF",
                data=f.read(),
                file_name=pdf_path.name,
                mime="application/pdf",
                use_container_width=True,
            )
    elif optimized:
        st.error("Не удалось создать PDF")

    # Translate
    if optimized and optimized.html:
        translate_targets = [lang for lang in SUPPORTED_LANGUAGES if lang.code != "en"]
        if translate_targets:
            tr_col1, tr_col2 = st.columns([2, 1])
            with tr_col1:
                translate_lang_code = st.selectbox(
                    "Перевести на...",
                    options=[lang.code for lang in translate_targets],
                    format_func=lambda c: next(lg.native_name for lg in translate_targets if lg.code == c),
                    key="translate_target_lang",
                    help="Перевести результат без повторной оптимизации",
                )
            with tr_col2:
                translate_clicked = st.button("🌐 Перевести", use_container_width=True, key="translate_btn")
            if translate_clicked and translate_lang_code:
                translate_language = get_language(translate_lang_code)
                try:
                    with st.status(f"Переводим на {translate_language.native_name}...", expanded=True) as tr_status:
                        def on_tr_status(msg):
                            tr_status.update(label=msg)
                            tr_status.write(msg)

                        translated = run_async(
                            translate_and_rerender(optimized, translate_language, job, on_status=on_tr_status)
                        )
                        tr_status.update(label="Перевод завершён", state="complete")

                    if translated.pdf_bytes:
                        source = st.session_state["source_resume"]
                        tr_pdf_path = pdf_storage.generate_path(
                            source.first_name, source.last_name, job.company, job.title,
                            lang_code=translate_language.code,
                        )
                        tr_pdf_path.parent.mkdir(parents=True, exist_ok=True)
                        tr_pdf_path.write_bytes(translated.pdf_bytes)

                        pdf_record = GeneratedPDF(
                            path=tr_pdf_path,
                            source_checksum=source.checksum,
                            company=job.company,
                            job_title=job.title,
                            first_name=source.first_name,
                            last_name=source.last_name,
                        )
                        pdf_storage.save_record(pdf_record)

                        if "english_html" not in st.session_state["last_result"]:
                            st.session_state["last_result"]["english_html"] = optimized.html

                        st.session_state["last_result"] = {
                            **st.session_state["last_result"],
                            "optimized": translated,
                            "pdf_path": tr_pdf_path,
                        }
                        st.rerun()
                except Exception as e:
                    st.error(f"Ошибка перевода: {e}")

    # Iteration details — скрыты от пользователя
    # (технические детали не показываем)

    if st.button("Очистить результат", use_container_width=True):
        st.session_state.pop("last_result", None)
        st.rerun()

    # Советы по улучшению — вынесены вниз
    failed_results = [r for r in validation.results if not r.passed]
    if failed_results:
        st.markdown("---")
        st.markdown("#### 💡 Как улучшить результат")
        for i, opt, val in iterations:
            display_filter_results(val)
