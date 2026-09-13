import os
from pathlib import Path

from dotenv import load_dotenv

# Подхватываем .env при обычном запуске `python bot.py`.
# В Docker он и так приходит через `env_file` в docker-compose.yml,
# но load_dotenv() здесь не мешает — если файла нет, просто ничего не делает.
_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))  # 0 = не ограничивать чат, бот отвечает в любом чате
# Тема (topic) в супергруппе-форуме. Узнать через /id (thread_id).
# 0 = не ограничивать темой. Если задан — бот работает только в этой теме,
# а в остальных темах того же чата подсказывает перейти в нужную.
GROUP_THREAD_ID = int(os.getenv("GROUP_THREAD_ID", "0"))

# --- Каталоги ---
BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "browser_profile"       # постоянный профиль Chromium (запасной способ хранить сессию)
RECORDINGS_DIR = BASE_DIR / "recordings"
DEBUG_DIR = BASE_DIR / "debug_screenshots"

# Портативная сессия (cookies+localStorage) в JSON — ОСНОВНОЙ и кроссплатформенный способ
# переиспользовать логин. В отличие от профиля Chromium (его cookies шифруются ключом ОС и
# при переносе Windows->Linux не расшифровываются -> бот заходит гостем), storage_state
# Playwright переинъецирует куки заново в любом окружении. Создаётся scripts/save_session.py.
# Лежит внутри browser_profile/ — значит уже покрыт .gitignore и volume'ом docker-compose.
STORAGE_STATE = PROFILE_DIR / "storage_state.json"

for d in (PROFILE_DIR, RECORDINGS_DIR, DEBUG_DIR):
    d.mkdir(parents=True, exist_ok=True)

# --- Режим захвата ---
# Браузер ВСЕГДА запускается в видимом (headed) режиме — настройки HEADLESS больше нет.
# В Docker окно рисуется в виртуальный дисплей Xvfb (DISPLAY=:99, см. entrypoint.sh),
# и именно этот дисплей захватывает ffmpeg. В настоящем headless-режиме Chromium ничего
# не рисует в X11 — из-за этого запись получалась чёрной (чёрный экран + курсор-крестик),
# а Яндекс сбрасывал сохранённую сессию в "гостя". Подробнее — в README.
#
# RECORD_ENABLED=false — тестировать логику подключения без запуска ffmpeg
# (например, на Windows без Xvfb/PulseAudio, где запись всё равно не работает).
RECORD_ENABLED = os.getenv("RECORD_ENABLED", "true").lower() == "true"

# Удалять локальную копию записи после успешной отправки в Telegram (экономия места на диске).
# Файл удаляется ТОЛЬКО если он прошёл проверку целостности (ffprobe) и реально отправился.
DELETE_AFTER_SEND = os.getenv("DELETE_AFTER_SEND", "true").lower() == "true"

DISPLAY_NUM = os.getenv("DISPLAY", ":99")
VIDEO_SIZE = os.getenv("VIDEO_SIZE", "1920x1080")
FRAMERATE = os.getenv("FRAMERATE", "25")
PULSE_SINK = os.getenv("PULSE_SINK", "MeetingSink")

# Ключевые слова на странице, по которым бот понимает, что встреча завершилась.
# Совпадение — подстрокой по всему HTML (в нижнем регистре), поэтому короткий маркер
# "организатор завершил встречу" ловит и полный тост "Организатор завершил встречу для всех".
# Важно держать здесь хотя бы один СТОЙКИЙ признак: тост наверху пропадает через пару
# секунд и при опросе раз в 10с его легко проскочить, а модалка "оцените качество связи"
# висит на экране после звонка — она и служит надёжным бэкапом.
# Если Телемост покажет что-то другое — добавь сюда через .env (см. README).
END_CALL_MARKERS = [
    m.strip().lower()
    for m in os.getenv(
        "END_CALL_MARKERS",
        "звонок завершён,звонок завершен,встреча завершена,"
        "организатор завершил встречу,вы вышли из встречи,"
        "оцените качество связи,you have left",
    ).split(",")
    if m.strip()
]

LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG")

# --- Напоминания во время записи ---
# Каждые сколько минут слать "бот всё ещё работает". 0 — отключить.
STATUS_REMINDER_MINUTES = int(os.getenv("STATUS_REMINDER_MINUTES", "20"))
# Через сколько минут после подключения напоминать, что "стандартная" длительность
# лекции истекла и, возможно, пора отключаться. 0 — отключить.
LECTURE_DURATION_MINUTES = int(os.getenv("LECTURE_DURATION_MINUTES", "90"))
