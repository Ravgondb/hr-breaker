import asyncio
import base64
import gc
import html as _html
import time
import traceback
import nest_asyncio
import streamlit as st

nest_asyncio.apply()

def run_async(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("loop closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)

def run_async_fresh(coro):
    """Создаёт свежий loop — для операций после длинных async-цепочек (перевод)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    nest_asyncio.apply(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

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
div[data-testid="stCheckbox"] { margin-bottom: 0 !important; }
div[data-testid="stCaptionContainer"] { margin-top: 0 !important; margin-bottom: 0 !important; }
div[data-testid="stSelectbox"] { margin-top: 0 !important; }
@media (max-width: 768px) {
    [data-testid="stSidebar"] { display: none !important; }
    [data-testid="collapsedControl"] { display: none !important; }
    .block-container { padding-left: 1rem !important; padding-right: 1rem !important; }
}
</style>
""", unsafe_allow_html=True)

@st.cache_data(show_spinner=False)
def cached_scrape_job(url: str) -> str:
    return scrape_job_posting(url)

@st.cache_data(show_spinner=False)
def cached_extract_name(content: str) -> tuple[str | None, str | None]:
    return run_async(extract_name(content))

@st.cache_data(show_spinner=False)
def cached_parse_job(text: str):
    return run_async(parse_job_posting(text))

FILTER_INFO = {
    "LLMChecker": {
        "name": "ATS-проверка",
        "fail_msg": "💡 Рекомендация по ATS-критериям",
        "color": "#fff9db",
        "border": "#ffc107",
        "explanation": "Наш ATS-фильтр строгий — он симулирует самые придирчивые системы отбора. Резюме не обязано проходить его идеально. Смотрите комментарии ниже — там конкретные причины срабатывания.",
        "advice_check": "Нажмите **«Оптимизировать резюме»** ниже — программа учтёт все замечания и постарается улучшить резюме под эти критерии.",
        "advice_optimize": "Программа не может добавить опыт которого нет в резюме — она работает только с тем что вы предоставили. Что можно сделать:\n\n• Посмотрите комментарии ниже — если какой-то опыт у вас есть но не отражён в резюме, допишите его в **Дополнительные инструкции** и запустите снова\n\n• Или попробуйте **Агрессивную оптимизацию** — программа переработает текст агрессивнее — на свой страх и риск",
    },
    "AIGeneratedChecker": {
        "name": "Проверка на ИИ-текст",
        "fail_msg": "🟡 Текст похож на сгенерированный ИИ",
        "color": "#fff9db",
        "border": "#ffc107",
        "explanation": "Некоторые HR-системы и рекрутеры отклоняют резюме которые звучат как написанные роботом — слишком гладко, шаблонно, без живой речи.",
        "advice": "Добавьте в инструкции: *«Пишите живым разговорным языком, избегайте канцеляризмов и шаблонных фраз»*",
    },
    "HallucinationChecker": {
        "name": "Проверка на выдумки",
        "fail_msg": "🟡 Обнаружены факты которых не было в оригинале",
        "color": "#fff9db",
        "border": "#ffc107",
        "explanation": "ИИ иногда добавляет информацию которой не было в вашем оригинальном резюме — выдуманные достижения, навыки или места работы.",
        "advice_check": "Добавьте в инструкции: *«Не добавляйте ничего чего нет в оригинале, только перефразируйте»*. Перед откликом на вакансию обязательно проверьте PDF.",
        "advice_optimize": "Внимание — ИИ ради оптимизации добавил факты которых не было в оригинале. Какие именно — смотрите в комментариях ниже. Перед откликом на вакансию обязательно проверьте PDF.",
    },
    "KeywordMatcher": {
        "name": "Ключевые слова",
        "fail_msg": "🔴 Мало ключевых слов из вакансии",
        "color": "#ffd6d6",
        "border": "#ff4b4b",
        "explanation": "ATS-системы ищут в резюме конкретные слова из вакансии. Если их нет — резюме отсеивается автоматически, даже если вы идеально подходите.",
        "advice_check": "Нажмите **«Оптимизировать резюме»** — программа постарается добавить нужную терминологию. Или добавьте в инструкции: *«Обязательно используйте терминологию из вакансии»*",
        "advice_optimize": "Программа не может добавить опыт которого нет в резюме — она работает только с тем что вы предоставили. Что можно сделать:\n\n• Посмотрите список в комментариях ниже — если какой-то опыт у вас есть но не отражён в резюме, допишите его в **Дополнительные инструкции** и запустите снова\n\n• Или попробуйте **Агрессивную оптимизацию** — программа переработает текст агрессивнее — на свой страх и риск",
    },
    "VectorSimilarityMatcher": {
        "name": "Соответствие вакансии",
        "fail_msg": "🔴 Резюме слабо соответствует вакансии",
        "color": "#ffd6d6",
        "border": "#ff4b4b",
        "explanation": "Система оценила насколько ваш опыт и навыки совпадают с требованиями вакансии. Совпадение недостаточное.",
        "advice": "Проверьте — правильную ли вакансию вы вставили? Если да — попробуйте добавить в инструкции конкретные навыки которые у вас есть но не отражены в резюме.",
    },
    "DataValidator": {
        "name": "Структура резюме",
        "fail_msg": "🔴 Ошибка структуры резюме",
        "color": "#ffd6d6",
        "border": "#ff4b4b",
        "explanation": "Возникла техническая проблема при создании резюме — структура документа некорректна.",
        "advice": "Попробуйте запустить ещё раз. Если ошибка повторяется — попробуйте другой формат резюме (например .txt вместо .pdf).",
    },
    "ContentLengthChecker": {
        "name": "Длина резюме",
        "fail_msg": "🔵 Резюме слишком длинное",
        "color": "#e8f4fd",
        "border": "#0984e3",
        "explanation": "Резюме не помещается на одну страницу. Большинство рекрутеров и ATS-систем предпочитают резюме на 1 страницу.",
        "advice": "Добавьте в инструкции: *«Сократи резюме до одной страницы, убери менее важный опыт»*",
    },
}

def display_filter_results(validation: ValidationResult, show_all: bool = False):
    is_check_mode = st.session_state.get("check_only_mode", False)
    for result in validation.results:
        # В режиме проверки галлюцинации невозможны — фильтр не показываем
        if is_check_mode and result.filter_name == "HallucinationChecker":
            continue

        if result.passed and not show_all:
            continue

        info = FILTER_INFO.get(result.filter_name, {})
        name = info.get("name", _html.escape(result.filter_name))

        if result.passed:
            st.success(f"✅ {name} пройдено")
            continue

        fail_msg = info.get("fail_msg", f"❌ {name}")
        explanation = info.get("explanation", "")
        if "advice_check" in info:
            advice = info["advice_check"] if is_check_mode else info["advice_optimize"]
        else:
            advice = info.get("advice", "")
        color = info.get("color", "#f8f9fa")
        border = info.get("border", "#ccc")

        st.markdown(f"""
        <div style="border-left: 4px solid {border}; background: {color}; border-radius: 8px; padding: 14px 16px; margin-bottom: 12px;">
            <div style="font-weight: 700; font-size: 15px; margin-bottom: 8px;">{fail_msg}</div>
            <div style="font-size: 13px; color: #444; margin-bottom: 8px;">{explanation}</div>
        </div>
        """, unsafe_allow_html=True)

        if advice:
            st.info(f"💡 **Что можно сделать:** {advice}")
        # Фильтруем технические сообщения — пользователю они не нужны
        _skip_fragments = ("без изображения", "изображени", "image", "rendering", "рендер", "конвертац")
        visible_issues = [
            issue for issue in (result.issues or [])
            if not any(f in issue.lower() for f in _skip_fragments)
        ]
        if visible_issues:
            with st.expander("💬 Комментарии", expanded=False):
                for issue in visible_issues:
                    st.write(f"- {issue}")

sequential_mode = False  # параллельный режим включён

_lang_options = [lang.code for lang in SUPPORTED_LANGUAGES]
_lang_labels = {lang.code: lang.native_name for lang in SUPPORTED_LANGUAGES}
_default_lang_idx = (
    _lang_options.index("ru")
    if "ru" in _lang_options
    else _lang_options.index(settings.default_language)
    if settings.default_language in _lang_options
    else 0
)
max_iterations = 3

# Main content
st.markdown("""
<div style="display:flex; align-items:center; gap:12px; margin-bottom:12px;">
    <div style="font-size:24px; font-weight:800; color:#1a1a1a; white-space:nowrap;">🎯 К Собесу</div>
    <div style="width:1px; height:18px; background:#ccc; flex-shrink:0;"></div>
    <div style="font-size:12px; color:#999; line-height:1.4;">Проверка резюме под вакансию.<br><b style="color:#555; font-weight:600;">Поможем обойти все ИИ HR-фильтры.</b></div>
</div>
<p style="font-size:13px; color:#666; margin-bottom:16px;">Загрузите резюме и вакансию — <b>бесплатно проверим</b> насколько оно подходит и дадим советы по улучшению. Хотите большего — оптимизируем резюме под вакансию и отдадим готовый PDF.</p>
""", unsafe_allow_html=True)

# Настройки и инструкции — читаем флаг, рендерим позже (перед кнопками)
show_optimize_options = st.session_state.get("show_optimize_options", False)
no_shame_mode = False
user_instructions = ""

selected_lang_code = st.session_state.get("selected_lang_code", "ru")
if selected_lang_code not in _lang_options:
    selected_lang_code = "ru" if "ru" in _lang_options else _lang_options[0]
selected_language = get_language(selected_lang_code)

# Two main columns: Resume | Job
col_resume, col_job = st.columns(2)

is_running = st.session_state.get("optimization_running", False)
has_pending_trigger = st.session_state.get("trigger_optimization", False)
if is_running and not has_pending_trigger:
    st.session_state["optimization_running"] = False
    st.session_state.pop("optimization_start_time", None)
    is_running = False
has_resume = "source_resume" in st.session_state

with col_resume:
    resume_header = "**Резюме ✓**" if has_resume else "**Резюме**"
    st.markdown(resume_header)
    if not has_resume:
        st.caption("Загрузите своё текущее резюме в любом формате — PDF, Word, TXT")

    if has_resume:
        src = st.session_state["source_resume"]
        name = f"{src.first_name or ''} {src.last_name or ''}".strip() or "Неизвестно"
        c1, c2 = st.columns([4, 1])
        with c1:
            st.success(f"✓ {name}")
        with c2:
            if st.button("Изменить", key="clear_resume", disabled=is_running):
                st.session_state.pop("source_resume", None)
                st.session_state.pop("last_result", None)
                st.session_state.pop("pasted_resume", None)
                st.session_state["resume_uploader_key"] = (
                    st.session_state.get("resume_uploader_key", 0) + 1
                )
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
                key="pasted_resume",
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
            if st.button("Изменить", key="clear_job", disabled=is_running):
                st.session_state.pop("job_text", None)
                st.session_state.pop("last_job_url", None)
                st.session_state.pop("last_result", None)
                st.session_state.pop("scrape_failed_url", None)
                st.session_state.pop("pasted_job", None)
                st.session_state.pop("job_url_input", None)
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
                "Ссылка на вакансию", label_visibility="collapsed", placeholder="https://...", key="job_url_input"
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
                key="pasted_job",
            )
            if pasted_job:
                st.session_state["job_text"] = pasted_job
                st.session_state.pop("scrape_failed_url", None)
                st.rerun()

# Настройки и инструкции — только для оптимизации, прямо перед кнопками
if show_optimize_options and not is_running:
    with st.expander("⚙️ Дополнительные настройки", expanded=True):
        set_col1, set_col2 = st.columns([1, 1])
        with set_col1:
            no_shame_mode = st.checkbox("Агрессивная оптимизация", value=False, key="no_shame_mode")
            st.caption("ИИ сильнее переработает текст — резюме может сильно отличаться от оригинала.")
        with set_col2:
            st.selectbox(
                "Язык резюме",
                options=_lang_options,
                index=_default_lang_idx,
                format_func=lambda code: _lang_labels[code],
                key="selected_lang_code",
            )
    if "user_instructions" not in st.session_state:
        st.session_state["user_instructions"] = ""
    user_instructions = st.text_area(
        "Дополнительные инструкции (необязательно)",
        placeholder="Например: сделай акцент на управлении командой, я перехожу из маркетинга в продакты...",
        help="Напиши пожелания для ИИ",
        key="user_instructions",
    )
    st.caption("💡 Необязательно, но помогает получить более точный результат")

# Две кнопки
can_check = has_resume and has_job and not is_running
can_optimize = has_resume and has_job and not is_running
btn_help = None
if not has_resume:
    btn_help = "Загрузите резюме"
elif not has_job:
    btn_help = "Добавь вакансию"

btn_col1, btn_col2 = st.columns(2)
with btn_col1:
    clicked_check = st.button(
        "🔍 Проверить резюме",
        key="btn_check",
        disabled=not can_check,
        use_container_width=True,
        help=btn_help,
    )
with btn_col2:
    clicked_optimize = st.button(
        "🚀 Оптимизировать резюме",
        key="btn_optimize",
        disabled=not can_optimize,
        use_container_width=True,
        help=btn_help,
    )

if clicked_check:
    # Проверка — скрываем настройки, запускаем сразу
    st.session_state["show_optimize_options"] = False
    st.session_state.pop("last_result", None)
    st.session_state["check_only_mode"] = True
    st.session_state["trigger_optimization"] = True
    st.rerun()

if clicked_optimize:
    if not show_optimize_options:
        # Первый клик — раскрываем настройки и инструкции
        st.session_state["show_optimize_options"] = True
        st.rerun()
    else:
        # Второй клик (настройки уже видны) — запускаем оптимизацию
        st.session_state.pop("last_result", None)
        gc.collect()
        st.session_state["show_optimize_options"] = False
        st.session_state["check_only_mode"] = False
        st.session_state["trigger_optimization"] = True
        st.rerun()

# Триггер запуска — по сохранённому флагу (клик или программный rerun-триггер)
should_run = st.session_state.pop("trigger_optimization", False)


# Оптимизация идёт прямо сейчас (should_run только что запустил её выше) — стопаем рендер
if is_running and not should_run:
    st.stop()


if should_run:
    if "source_resume" not in st.session_state:
        st.session_state["optimization_running"] = False
        st.rerun()

    source = st.session_state["source_resume"]
    instructions_value = st.session_state.get("user_instructions", "").strip() or None
    if instructions_value != source.instructions:
        source = source.model_copy(update={"instructions": instructions_value})
        cache.put(source)
        st.session_state["source_resume"] = source
    st.session_state["optimization_running"] = True
    st.session_state["optimization_start_time"] = time.time()
    error_occurred = None

    try:
        with st.spinner("Анализируем вакансию..."):
            job = cached_parse_job(job_text)

        idle_for_retries = 10
        last_idle_for_error = None
        is_check_only = st.session_state.get("check_only_mode", False)
        run_iterations = 1 if is_check_only else max_iterations

        # Плейсхолдер для live-статуса — обновляется из on_iteration
        status_box = st.empty()
        if is_check_only:
            status_box.info("🔍 Проверяем резюме... не закрывайте браузер!")
        else:
            status_box.info(f"🚀 Запускаем оптимизацию — {run_iterations} итерации. Это займёт **15–25 минут**, не закрывайте браузер!")

        with st.spinner("Работаем..."):
            for attempt in range(idle_for_retries + 1):
                try:
                    iteration_results = []

                    def on_iteration(i, opt, val):
                        # Сохраняем без pdf_bytes — они занимают много памяти
                        opt_light = opt.model_copy(update={"pdf_bytes": None})
                        iteration_results.append((i, opt_light, val))
                        passed = sum(1 for r in val.results if r.passed)
                        total = len(val.results)
                        if is_check_only:
                            status_box.info(f"🔍 Анализ завершён — пройдено {passed} из {total} проверок")
                        else:
                            if i + 1 < run_iterations:
                                status_box.info(f"✅ Итерация {i + 1} из {run_iterations} готова. Продолжаем...")
                            else:
                                status_box.info(f"✅ Все {run_iterations} итерации готовы — пройдено {passed}/{total} проверок. Генерируем PDF...")

                    def on_translation_status(msg):
                        pass  # не используется здесь — перевод делается отдельно ниже

                    # Сначала оптимизируем на английском без перевода
                    optimized, validation, job = run_async(
                        optimize_for_job(
                            source,
                            job_text,
                            max_iterations=run_iterations,
                            on_iteration=on_iteration,
                            job=job,
                            parallel=not sequential_mode,
                            no_shame=no_shame_mode,
                            user_instructions=instructions_value,
                            language=None,
                            on_translation_status=on_translation_status,
                        )
                    )

                    # Переводим и делаем PDF только если не режим проверки
                    if not is_check_only:
                        if selected_language.code != "en" and optimized and optimized.html:
                            status_box.info("🌐 Переводим резюме... не закрывайте браузер!")
                            def on_translation_status(msg):
                                if "Refining" in msg or "Reviewing" in msg:
                                    status_box.info("🌐 Проверяем качество перевода...")
                                else:
                                    status_box.info("🌐 Переводим резюме... не закрывайте браузер!")
                            try:
                                translated = run_async_fresh(
                                    translate_and_rerender(optimized, selected_language, job, on_status=on_translation_status)
                                )
                                if translated:
                                    optimized = translated
                            except Exception as tr_err:
                                # Перевод упал — используем английский вариант, не теряем результат
                                status_box.warning("⚠️ Перевод не удался — сохраняем английскую версию.")

                    pdf_path = None
                    if not is_check_only and optimized and optimized.pdf_bytes:
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

                    break
                except Exception as e:
                    if "idle_for" in str(e) and attempt < idle_for_retries:
                        last_idle_for_error = e
                        sleep_s = min(2 ** attempt, 15)
                        st.warning(f"Временный сбой ИИ-сервиса (idle_for). Повтор {attempt + 1}/{idle_for_retries} через {sleep_s}с...")
                        time.sleep(sleep_s)
                        continue
                    raise

        if last_idle_for_error and attempt == idle_for_retries:
            raise last_idle_for_error

        st.session_state["last_result"] = {
            "optimized": optimized,
            "validation": validation,
            "job": job,
            "iterations": iteration_results,
            "pdf_path": pdf_path,
        }
    except TimeoutError:
        error_occurred = TimeoutError("Превышено время ожидания ответа от ИИ. Попробуй ещё раз.")
    except Exception as e:
        error_occurred = e
        st.session_state["last_error_traceback"] = traceback.format_exc()
    finally:
        st.session_state["optimization_running"] = False
        st.session_state.pop("optimization_start_time", None)

    if error_occurred:
        if "idle_for" in str(error_occurred):
            st.error("Ошибка оптимизации: временный сбой ИИ-сервиса. Подождите 20–30 секунд и попробуйте снова.")
        else:
            st.error(f"Ошибка оптимизации: {error_occurred}")
        last_tb = st.session_state.pop("last_error_traceback", None)
        if last_tb:
            with st.expander("Технические детали ошибки", expanded=False):
                st.code(last_tb)
    else:
        st.session_state["idle_for_ui_retries"] = 0
        st.rerun()

# Цитата — только когда нет результата и нет активного запуска
if not should_run and not is_running and "last_result" not in st.session_state:
    st.markdown("""
    <div style="text-align: center; padding: 80px 20px 20px 20px;">
        <div style="font-family: Georgia, serif; font-size: 22px; font-style: italic; color: #555; line-height: 1.6; max-width: 600px; margin: 0 auto;">
            «Единственный способ делать великую работу —<br>любить то, что вы делаете»
        </div>
        <div style="margin-top: 16px; font-family: Georgia, serif; font-size: 14px; color: #aaa; letter-spacing: 1px;">
            Стив Джобс
        </div>
    </div>
    """, unsafe_allow_html=True)

# Display last result if exists
if "last_result" in st.session_state:
    result = st.session_state["last_result"]
    optimized = result["optimized"]
    validation = result["validation"]
    job = result["job"]
    iterations = result["iterations"]
    pdf_path = result["pdf_path"]

    st.markdown("---")
    st.markdown(f"### 🎯 Результат: {job.title} — {job.company}")

    # Если режим проверки — показываем только статус
    is_check_result = st.session_state.get("check_only_mode", False)

    # В режиме проверки HallucinationChecker не показываем и не считаем
    visible_results = [
        r for r in validation.results
        if not (is_check_result and r.filter_name == "HallucinationChecker")
    ]
    total_count = len(visible_results)
    failed_count = sum(1 for r in visible_results if not r.passed)
    passed_count = total_count - failed_count

    if validation.passed:
        st.success("✅ Все проверки пройдены!")
    else:
        if is_check_result:
            st.warning(
                f"Проверка завершена! Не пройдено критериев: {failed_count} из {total_count} — смотрите советы внизу."
            )
        else:
            st.warning(
                f"Резюме готово! Не пройдено проверок: {failed_count} из {total_count} — смотрите советы внизу."
            )



    # PDF download
    if pdf_path and pdf_path.exists():
        st.success("✅ Резюме оптимизировано!")
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        # Авто-скачивание через JavaScript
        b64 = base64.b64encode(pdf_bytes).decode()
        st.components.v1.html(f"""
            <script>
                const link = document.createElement('a');
                link.href = 'data:application/pdf;base64,{b64}';
                link.download = '{pdf_path.name}';
                document.body.appendChild(link);
                link.click();
                document.body.removeChild(link);
            </script>
        """, height=0)

        st.markdown("""
        <style>
        div[data-testid="stDownloadButton"] > button {
            background-color: #1a1a2e !important;
            color: white !important;
            border: none !important;
            font-weight: 600 !important;
        }
        div[data-testid="stDownloadButton"] > button:hover {
            background-color: #16213e !important;
            color: white !important;
        }
        </style>
        """, unsafe_allow_html=True)
        st.download_button(
            label="Скачать PDF",
            data=pdf_bytes,
            file_name=pdf_path.name,
            mime="application/pdf",
            key="download_pdf",
            use_container_width=True,
        )
    elif optimized and not st.session_state.get("check_only_mode", False) and (not pdf_path or not pdf_path.exists()):
        st.error("Не удалось создать PDF")

    # Translate — только если есть PDF (не в режиме проверки)
    if optimized and optimized.html and not st.session_state.get("check_only_mode", False):
        translate_targets = [lang for lang in SUPPORTED_LANGUAGES if lang.code != "en" and lang.code != selected_lang_code]
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
                            pass  # не вызываем Streamlit из async-колбэка

                        translated = run_async(
                            translate_and_rerender(optimized, translate_language, job, on_status=on_tr_status)
                        )
                        tr_status.update(label="Перевод завершён", state="complete")

                    if translated and translated.pdf_bytes:
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

    if st.button("Очистить результат", key="btn_clear", use_container_width=True):
        st.session_state.pop("last_result", None)
        st.session_state.pop("check_only_mode", None)
        st.rerun()

    # Детали проверки — все фильтры (пройденные и нет)
    failed_results = [r for r in visible_results if not r.passed]

    if is_check_result:
        st.markdown("---")
        st.markdown("#### 📋 Детали проверки")
        if iterations:
            _, _, last_val = iterations[-1]
            display_filter_results(last_val, show_all=True)

        if failed_results:
            st.markdown("---")
            st.markdown("""
            <div style="background: #f8f9fa; border-radius: 10px; padding: 16px; margin-bottom: 12px;">
                <div style="font-size: 15px; font-weight: 600; color: #333; margin-bottom: 6px;">🚀 Хотите чтобы программа помогла исправить это?</div>
                <div style="font-size: 13px; color: #666; line-height: 1.5;">Выше вы видите советы — можете внести правки сами. Или доверьте это нам: программа учтёт все замечания и поможет создать улучшенную версию резюме.</div>
            </div>
            """, unsafe_allow_html=True)
            st.caption("⏱ Займёт ещё ~25 минут")
            if st.button("🔄 Оптимизировать резюме", key="btn_improve", use_container_width=True):
                source = st.session_state["source_resume"]
                if iterations:
                    _, _, last_val = iterations[-1]
                    extra = []
                    for r in last_val.results:
                        if not r.passed and r.issues:
                            extra.extend(r.issues)
                    if extra:
                        combined = "Исправь следующие проблемы: " + "; ".join(extra[:5])
                        source = source.model_copy(update={"instructions": combined})
                        st.session_state["source_resume"] = source
                st.session_state.pop("last_result", None)
                gc.collect()
                st.session_state["check_only_mode"] = False
                st.session_state["show_optimize_options"] = False
                st.session_state["trigger_optimization"] = True
                st.rerun()

    elif failed_results:
        # После оптимизации — показываем только непройденные фильтры с советами
        st.markdown("---")
        st.markdown("#### 💡 Что можно улучшить")
        if iterations:
            _, _, last_val = iterations[-1]
            display_filter_results(last_val, show_all=False)
