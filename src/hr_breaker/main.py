import asyncio
import base64
import threading
import time
import traceback

import streamlit as st

from hr_breaker.agents import extract_name, parse_job_posting
from hr_breaker.config import get_settings
from hr_breaker.models import (
    GeneratedPDF,
    ResumeSource,
    ValidationResult,
    SUPPORTED_LANGUAGES,
    get_language,
)
from hr_breaker.orchestration import optimize_for_job, translate_and_rerender
from hr_breaker.services import (
    PDFStorage,
    ResumeCache,
    scrape_job_posting,
    CloudflareBlockedError,
)
from hr_breaker.services.pdf_parser import load_resume_content_from_upload


# -----------------------------
# Async helper without nest_asyncio/session loop
# -----------------------------
def run_async(coro, timeout_sec: int = 900):
    wrapped = asyncio.wait_for(coro, timeout=timeout_sec)

    try:
        asyncio.get_running_loop()
        has_running_loop = True
    except RuntimeError:
        has_running_loop = False

    if not has_running_loop:
        return asyncio.run(wrapped)

    # Rare fallback: if loop already running in current thread
    result_holder = {}
    error_holder = {}

    def _runner():
        try:
            result_holder["result"] = asyncio.run(wrapped)
        except Exception as e:
            error_holder["error"] = e

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout_sec + 5)

    if t.is_alive():
        raise TimeoutError(f"Async task exceeded {timeout_sec} seconds")
    if "error" in error_holder:
        raise error_holder["error"]
    return result_holder.get("result")


# Initialize services
cache = ResumeCache()
pdf_storage = PDFStorage()
settings = get_settings()

st.set_page_config(page_title="К Собесу", page_icon="🎯", layout="wide")

st.markdown(
    """
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
""",
    unsafe_allow_html=True,
)


@st.cache_data(show_spinner=False)
def cached_scrape_job(url: str) -> str:
    return scrape_job_posting(url)


@st.cache_data(show_spinner=False)
def cached_extract_name(content: str) -> tuple[str | None, str | None]:
    return run_async(extract_name(content), timeout_sec=120)


@st.cache_data(show_spinner=False)
def cached_parse_job(text: str):
    return run_async(parse_job_posting(text), timeout_sec=180)


FILTER_INFO = {
    "LLMChecker": {
        "name": "ATS-проверка",
        "fail_msg": "🔴 Резюме не прошло автоматический отбор",
        "color": "#ffd6d6",
        "border": "#ff4b4b",
        "explanation": "ATS-роботы оценивают резюме по десяткам критериев — структура, ключевые слова, соответствие должности. Это не значит что твоё резюме плохое — просто оно пока не оптимизировано под этот формат. Так происходит с большинством резюме с первого раза.",
        "advice": "Включи **Агрессивную оптимизацию** в настройках и запусти снова — это позволит ИИ сильнее переработать текст и лучше попасть под критерии ATS. Или добавь в инструкции: *«Переформулируй опыт используя глаголы достижений: увеличил, сократил, внедрил, запустил»*",
    },
    "AIGeneratedChecker": {
        "name": "Проверка на ИИ-текст",
        "fail_msg": "🟡 Текст похож на сгенерированный ИИ",
        "color": "#fff9db",
        "border": "#ffc107",
        "explanation": "Некоторые HR-системы и рекрутеры отклоняют резюме которые звучат как написанные роботом — слишком гладко, шаблонно, без живой речи.",
        "advice": "Добавь в инструкции: *«Пиши живым разговорным языком, избегай канцеляризмов и шаблонных фраз»*",
    },
    "HallucinationChecker": {
        "name": "Проверка на выдумки",
        "fail_msg": "🟡 Обнаружены факты которых не было в оригинале",
        "color": "#fff9db",
        "border": "#ffc107",
        "explanation": "ИИ иногда добавляет информацию которой не было в твоём оригинальном резюме — выдуманные достижения, навыки или места работы.",
        "advice": "Добавь в инструкции: *«Не добавляй ничего чего нет в оригинале, только перефразируй»*. Перед отправкой обязательно проверь PDF.",
    },
    "KeywordMatcher": {
        "name": "Ключевые слова",
        "fail_msg": "🔴 Мало ключевых слов из вакансии",
        "color": "#ffd6d6",
        "border": "#ff4b4b",
        "explanation": "ATS-системы ищут в резюме конкретные слова из вакансии. Если их нет — резюме отсеивается автоматически, даже если ты идеально подходишь.",
        "advice": "Добавь в инструкции: *«Обязательно используй терминологию из вакансии»*. Или вставь текст вакансии полностью — чем больше деталей, тем лучше.",
    },
    "VectorSimilarityMatcher": {
        "name": "Соответствие вакансии",
        "fail_msg": "🔴 Резюме слабо соответствует вакансии",
        "color": "#ffd6d6",
        "border": "#ff4b4b",
        "explanation": "Система оценила насколько твой опыт и навыки совпадают с требованиями вакансии. Совпадение недостаточное.",
        "advice": "Проверь — правильную ли вакансию ты вставил? Если да — попробуй добавить в инструкции конкретные навыки которые у тебя есть но не отражены в резюме.",
    },
    "DataValidator": {
        "name": "Структура резюме",
        "fail_msg": "🔴 Ошибка структуры резюме",
        "color": "#ffd6d6",
        "border": "#ff4b4b",
        "explanation": "Возникла техническая проблема при создании резюме — структура документа некорректна.",
        "advice": "Попробуй запустить ещё раз. Если ошибка повторяется — попробуй другой формат резюме (например .txt вместо .pdf).",
    },
    "ContentLengthChecker": {
        "name": "Длина резюме",
        "fail_msg": "🔵 Резюме слишком длинное",
        "color": "#e8f4fd",
        "border": "#0984e3",
        "explanation": "Резюме не помещается на одну страницу. Большинство рекрутеров и ATS-систем предпочитают резюме на 1 страницу.",
        "advice": "Добавь в инструкции: *«Сократи резюме до одной страницы, убери менее важный опыт»*",
    },
}


def display_filter_results(validation: ValidationResult):
    for result in validation.results:
        if result.passed:
            continue
        info = FILTER_INFO.get(result.filter_name, {})
        name = info.get("name", result.filter_name)
        fail_msg = info.get("fail_msg", f"❌ {name}")
        explanation = info.get("explanation", "")
        advice = info.get("advice", "")
        color = info.get("color", "#f8f9fa")
        border = info.get("border", "#ccc")

        st.markdown(
            f"""
        <div style="border-left: 4px solid {border}; background: {color}; border-radius: 8px; padding: 14px 16px; margin-bottom: 12px;">
            <div style="font-weight: 700; font-size: 15px; margin-bottom: 8px;">{fail_msg}</div>
            <div style="font-size: 13px; color: #444; margin-bottom: 8px;">{explanation}</div>
        </div>
        """,
            unsafe_allow_html=True,
        )

        if advice:
            st.info(f"💡 **Что можно сделать:** {advice}")
        if result.issues:
            with st.expander("💬 Комментарии", expanded=False):
                for issue in result.issues:
                    st.write(f"- {issue}")


sequential_mode = False
debug_mode = False

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

st.markdown(
    """
<div style="display:flex; align-items:center; gap:12px; margin-bottom:12px;">
    <div style="font-size:24px; font-weight:800; color:#1a1a1a; white-space:nowrap;">🎯 К Собесу</div>
    <div style="width:1px; height:18px; background:#ccc; flex-shrink:0;"></div>
    <div style="font-size:12px; color:#999; line-height:1.4;">Проверка резюме под вакансию.<br><b style="color:#555; font-weight:600;">Поможем обойти все ИИ HR-фильтры.</b></div>
</div>
<p style="font-size:13px; color:#666; margin-bottom:16px;">Загрузи резюме и вакансию — <b>бесплатно проверим</b> насколько оно подходит и дадим советы по улучшению. Хочешь большего — оптимизируем резюме под вакансию и отдадим готовый PDF.</p>
""",
    unsafe_allow_html=True,
)

with st.expander("⚙️ Дополнительные настройки"):
    set_col1, set_col2 = st.columns([1, 1])
    with set_col1:
        no_shame_mode = st.checkbox("Агрессивная оптимизация", value=False)
        st.caption(
            "ИИ сильнее переработает текст — резюме может сильно отличаться от оригинала. Проверь резюме перед отправкой."
        )
    with set_col2:
        selected_lang_code = st.selectbox(
            "Язык резюме",
            options=_lang_options,
            index=_default_lang_idx,
            format_func=lambda code: _lang_labels[code],
            key="selected_lang_code",
        )

selected_lang_code = st.session_state.get("selected_lang_code", "ru")
if selected_lang_code not in _lang_options:
    selected_lang_code = "ru" if "ru" in _lang_options else _lang_options[0]
selected_language = get_language(selected_lang_code)

col_resume, col_job = st.columns(2)

is_running = st.session_state.get("optimization_running", False)
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
            if st.button("Изменить", key="clear_resume", disabled=is_running):
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
            uploader_key = f"resume_uploader_{st.session_state.get('resume_uploader_key', 0)}"
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
            if st.button("Изменить", key="clear_job", disabled=is_running):
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
                "Ссылка на вакансию",
                label_visibility="collapsed",
                placeholder="https://...",
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
                        st.warning(
                            "Сайт защищён от ботов. Скопируй текст вакансии вручную."
                        )
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

if "user_instructions" not in st.session_state:
    st.session_state["user_instructions"] = ""

user_instructions = st.text_area(
    "Дополнительные инструкции (необязательно)",
    placeholder="Например: сделай акцент на управлении командой, я перехожу из маркетинга в продакты...",
    help="Напиши пожелания для ИИ — например: «сделай акцент на управлении командой» или «я перехожу из маркетинга в продакты»",
    key="user_instructions",
)
st.caption("💡 Необязательно, но помогает получить более точный результат")

can_optimize = has_resume and has_job and not is_running
btn_help = None
if not has_resume:
    btn_help = "Загрузи резюме"
elif not has_job:
    btn_help = "Добавь вакансию"

btn_col1, btn_col2 = st.columns(2)
with btn_col1:
    clicked_check = st.button(
        "🔍 Проверить резюме",
        disabled=not can_optimize,
        use_container_width=True,
        help=btn_help,
    )
with btn_col2:
    clicked_optimize = st.button(
        "🚀 Оптимизировать резюме — Бесплатно",
        disabled=not can_optimize,
        use_container_width=True,
        help=btn_help,
    )

clicked = clicked_check or clicked_optimize
check_only = clicked_check and not clicked_optimize

if is_running:
    if "optimization_start_time" not in st.session_state:
        st.session_state["optimization_start_time"] = time.time()

    elapsed = time.time() - st.session_state.get("optimization_start_time", time.time())

    if elapsed > 45 * 60:
        st.session_state["optimization_running"] = False
        st.session_state.pop("optimization_start_time", None)
        st.error("⚠️ Превышено время ожидания (45 минут). Попробуй снова.")
        st.rerun()
    else:
        is_check = st.session_state.get("check_only_mode", False)
        if is_check:
            st.markdown(
                """
            <style>
            @keyframes spin { 0%{transform:rotate(0deg)} 100%{transform:rotate(360deg)} }
            .loader { width:28px; height:28px; border:3px solid #ffc107; border-top:3px solid transparent; border-radius:50%; animation:spin 0.9s linear infinite; margin:0 auto 10px; }
            </style>
            <div style="background:#fff3cd; border:1px solid #ffc107; border-radius:10px; padding:18px; text-align:center; margin-top:8px;">
                <div class="loader"></div>
                <div style="font-weight:600; font-size:15px; color:#856404;">Проверяем резюме...</div>
                <div style="font-size:13px; color:#856404; margin-top:4px;">Анализируем соответствие вакансии · Не закрывай браузер!</div>
            </div>
            """,
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f"""
            <style>
            @keyframes pulse {{ 0%{{transform:scale(1)}} 50%{{transform:scale(1.2)}} 100%{{transform:scale(1)}} }}
            .rocket {{ font-size:28px; animation:pulse 1.2s ease-in-out infinite; display:block; margin-bottom:8px; }}
            </style>
            <div style="background:#e8f4fd; border:1px solid #0984e3; border-radius:10px; padding:18px; text-align:center; margin-top:8px;">
                <span class="rocket">🚀</span>
                <div style="font-weight:600; font-size:15px; color:#0984e3;">Оптимизируем резюме...</div>
                <div style="font-size:13px; color:#0984e3; margin-top:4px;">Итерация 1 из {max_iterations} · Это может занять несколько минут · Не закрывай браузер!</div>
            </div>
            """,
                unsafe_allow_html=True,
            )

if clicked:
    source = st.session_state["source_resume"]
    instructions_value = user_instructions.strip() if user_instructions else None

    if instructions_value != source.instructions:
        source = source.model_copy(update={"instructions": instructions_value})
        cache.put(source)
        st.session_state["source_resume"] = source

    st.session_state["optimization_running"] = True
    st.session_state["optimization_start_time"] = time.time()
    st.session_state["check_only_mode"] = check_only
    error_occurred = None

    try:
        with st.spinner("Анализируем вакансию..."):
            job = cached_parse_job(job_text)

        debug_dir = None
        if debug_mode:
            debug_dir = pdf_storage.generate_debug_dir(job.company, job.title)

        iteration_results = []
        progress_placeholder = st.empty()
        is_check_only = st.session_state.get("check_only_mode", False)
        run_iterations = 1 if is_check_only else max_iterations

        def update_banner(title, subtitle):
            if is_check_only:
                progress_placeholder.markdown(
                    f"""
                <style>
                @keyframes spin {{ 0%{{transform:rotate(0deg)}} 100%{{transform:rotate(360deg)}} }}
                .loader2 {{ width:24px; height:24px; border:3px solid #ffc107; border-top:3px solid transparent; border-radius:50%; animation:spin 0.9s linear infinite; margin:0 auto 8px; }}
                </style>
                <div style="background:#fff3cd; border:1px solid #ffc107; border-radius:10px; padding:14px; text-align:center;">
                    <div class="loader2"></div>
                    <div style="font-weight:600; font-size:14px; color:#856404;">{title}</div>
                    <div style="font-size:12px; color:#856404; margin-top:4px;">{subtitle}</div>
                </div>
                """,
                    unsafe_allow_html=True,
                )
            else:
                progress_placeholder.markdown(
                    f"""
                <style>
                @keyframes pulse2 {{ 0%{{transform:scale(1)}} 50%{{transform:scale(1.2)}} 100%{{transform:scale(1)}} }}
                .rocket2 {{ font-size:24px; animation:pulse2 1.2s ease-in-out infinite; display:block; margin-bottom:6px; }}
                </style>
                <div style="background:#e8f4fd; border:1px solid #0984e3; border-radius:10px; padding:14px; text-align:center;">
                    <span class="rocket2">🚀</span>
                    <div style="font-weight:600; font-size:14px; color:#0984e3;">{title}</div>
                    <div style="font-size:12px; color:#0984e3; margin-top:4px;">{subtitle}</div>
                </div>
                """,
                    unsafe_allow_html=True,
                )

        def on_iteration(i, opt, val):
            iteration_results.append((i, opt, val))
            if is_check_only:
                update_banner(
                    "Проверяем резюме...",
                    "Анализируем соответствие вакансии · Не закрывай браузер!",
                )
            else:
                next_msg = (
                    f"Готовим итерацию {i + 2} из {run_iterations} · Не закрывай браузер!"
                    if i + 1 < run_iterations
                    else "Финальная обработка · Не закрывай браузер!"
                )
                update_banner(f"Итерация {i + 1} из {run_iterations} завершена", next_msg)

        def on_translation_status(_msg):
            update_banner("Финальная обработка...", "Переводим на русский · Почти готово!")

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
            ),
            timeout_sec=60 * 40,
        )

        if not is_check_only and selected_language.code != "en" and optimized and optimized.html:
            on_translation_status("translating")
            optimized = run_async(
                translate_and_rerender(
                    optimized, selected_language, job, on_status=on_translation_status
                ),
                timeout_sec=60 * 20,
            )

        progress_placeholder.empty()

        pdf_path = None
        if not is_check_only and optimized and optimized.pdf_bytes:
            pdf_path = pdf_storage.generate_path(
                source.first_name,
                source.last_name,
                job.company,
                job.title,
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

    except TimeoutError as e:
        error_occurred = e
    except Exception:
        error_occurred = traceback.format_exc()
    finally:
        st.session_state["optimization_running"] = False
        st.session_state.pop("optimization_start_time", None)

    if error_occurred:
        st.error(f"Ошибка оптимизации:\n{error_occurred}")
    else:
        st.rerun()

if "last_result" in st.session_state:
    result = st.session_state["last_result"]
    optimized = result["optimized"]
    validation = result["validation"]
    job = result["job"]
    iterations = result["iterations"]
    pdf_path = result["pdf_path"]

    st.markdown("---")
    st.markdown(f"### Результат: {job.title} — {job.company}")

    is_check_result = st.session_state.get("check_only_mode", False)
    if is_check_result and not is_running:
        st.info(
            "👆 Это результат **проверки** — советы ниже. Хотите получить оптимизированное резюме в PDF?"
        )
        if st.button(
            "🚀 Оптимизировать резюме — Бесплатно",
            key="optimize_after_check",
            use_container_width=True,
        ):
            st.session_state["check_only_mode"] = False
            st.session_state.pop("last_result", None)
            st.session_state["optimization_running"] = True
            st.session_state["optimization_start_time"] = time.time()
            st.rerun()

    if validation.passed:
        st.success("✅ Все проверки пройдены!")
    else:
        passed = [r.filter_name for r in validation.results if r.passed]
        st.warning(
            f"Резюме готово! Некоторые проверки не пройдены ({len(passed)}/{len(validation.results)}) — смотри советы внизу."
        )

    if pdf_path:
        st.success("✅ Резюме оптимизировано!")
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        b64 = base64.b64encode(pdf_bytes).decode()
        st.components.v1.html(
            f"""
            <script>
                const link = document.createElement('a');
                link.href = 'data:application/pdf;base64,{b64}';
                link.download = '{pdf_path.name}';
                document.body.appendChild(link);
                link.click();
                document.body.removeChild(link);
            </script>
        """,
            height=0,
        )

        st.info("📥 PDF скачивается автоматически. Если не началось — нажми кнопку ниже.")
        st.download_button(
            label="⬇️ Скачать PDF вручную",
            data=pdf_bytes,
            file_name=pdf_path.name,
            mime="application/pdf",
            use_container_width=True,
        )
    elif optimized and not st.session_state.get("check_only_mode", False):
        st.error("Не удалось создать PDF")

    if optimized and optimized.html:
        translate_targets = [lang for lang in SUPPORTED_LANGUAGES if lang.code != "en"]
        if translate_targets:
            tr_col1, tr_col2 = st.columns([2, 1])
            with tr_col1:
                translate_lang_code = st.selectbox(
                    "Перевести на...",
                    options=[lang.code for lang in translate_targets],
                    format_func=lambda c: next(
                        lg.native_name for lg in translate_targets if lg.code == c
                    ),
                    key="translate_target_lang",
                    help="Перевести результат без повторной оптимизации",
                )
            with tr_col2:
                translate_clicked = st.button(
                    "🌐 Перевести", use_container_width=True, key="translate_btn"
                )

            if translate_clicked and translate_lang_code:
                translate_language = get_language(translate_lang_code)
                try:
                    with st.status(
                        f"Переводим на {translate_language.native_name}...", expanded=True
                    ) as tr_status:

                        def on_tr_status(msg):
                            tr_status.update(label=msg)
                            tr_status.write(msg)

                        translated = run_async(
                            translate_and_rerender(
                                optimized, translate_language, job, on_status=on_tr_status
                            ),
                            timeout_sec=60 * 20,
                        )
                        tr_status.update(label="Перевод завершён", state="complete")

                    if translated.pdf_bytes:
                        source = st.session_state["source_resume"]
                        tr_pdf_path = pdf_storage.generate_path(
                            source.first_name,
                            source.last_name,
                            job.company,
                            job.title,
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
                except Exception:
                    st.error(f"Ошибка перевода:\n{traceback.format_exc()}")

    if st.button("Очистить результат", use_container_width=True):
        st.session_state.pop("last_result", None)
        st.rerun()

    failed_results = [r for r in validation.results if not r.passed]
    if failed_results:
        st.markdown("---")
        st.markdown("#### 💡 Как улучшить результат")

        if iterations:
            _, _, last_val = iterations[-1]
            display_filter_results(last_val)

        st.markdown("---")
        st.markdown(
            """
        <div style="background: #f8f9fa; border-radius: 10px; padding: 16px; margin-bottom: 12px;">
            <div style="font-size: 15px; font-weight: 600; color: #333; margin-bottom: 6px;">🚀 Хотите чтобы программа помогла исправить это?</div>
            <div style="font-size: 13px; color: #666; line-height: 1.5;">Выше вы видите советы — можете внести правки сами. Или доверьте это нам: программа учтёт все замечания и поможет создать улучшенную версию резюме.</div>
        </div>
        """,
            unsafe_allow_html=True,
        )
        st.caption("⏱ Займёт ещё ~10 минут")

        if st.button("🔄 Оптимизировать резюме — Бесплатно", use_container_width=True):
            source = st.session_state["source_resume"]
            if iterations:
                _, _, last_val = iterations[-1]
                extra = []
                for r in last_val.results:
                    if not r.passed and r.issues:
                        extra.extend(r.issues)
                if extra:
                    existing = source.instructions or ""
                    combined = (
                        (existing + "\n" if existing else "")
                        + "Исправь следующие проблемы: "
                        + "; ".join(extra[:5])
                    )
                    source = source.model_copy(update={"instructions": combined})
                    st.session_state["source_resume"] = source

            st.session_state.pop("last_result", None)
            st.session_state["check_only_mode"] = False
            st.session_state["optimization_running"] = True
            st.session_state["optimization_start_time"] = time.time()
            st.rerun()
