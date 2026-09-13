import asyncio
import time
from typing import List, Optional

from playwright.async_api import async_playwright, Page, BrowserContext, Browser, Playwright

import config
from utils import get_logger

log = get_logger("browser")

# Реальные селекторы, снятые с живой страницы Яндекс Телемоста (сентябрь 2026).
# Если Телемост поменяет вёрстку — смотри debug_screenshots/*.png и правь тут.

# Шаг 1: промежуточный экран "Вы подключаетесь к видеовстрече" -> "Продолжить в браузере"
CONTINUE_IN_BROWSER_SELECTORS = [
    "[data-testid='orb-button']:has-text('Продолжить в браузере')",
    "button:has-text('Продолжить в браузере')",
]

# Шаг 2: панель с превью камеры — выключить микрофон/камеру ДО входа
MUTE_MIC_SELECTORS = [
    "[data-testid='turn-off-mic-button']",
]
MUTE_CAM_SELECTORS = [
    "[data-testid='turn-off-camera-button']",
]

# Шаг 3: сама кнопка входа в конференцию
JOIN_BUTTON_SELECTORS = [
    "[data-testid='enter-conference-button']",
    "button:has-text('Подключиться')",
]


class TelemostWorker:
    def __init__(self):
        self.playwright: Optional[Playwright] = None
        self.context: Optional[BrowserContext] = None
        self._browser: Optional[Browser] = None  # задан только в режиме storage_state (не persistent)
        self.page: Optional[Page] = None
        self._stop_event = asyncio.Event()

    async def start_browser(self) -> None:
        log.info("Запускаю Playwright/Chromium...")

        # Диагностика: если нет ни портативной сессии, ни профиля — Телемост откроется "гостем".
        if not config.STORAGE_STATE.exists() and not (config.PROFILE_DIR / "Default").exists():
            log.warning(
                "Нет ни %s, ни профиля в %s — сохранённой сессии Яндекса ещё нет. "
                "Сначала запусти scripts/save_session.py и залогинься вручную, иначе бот "
                "зайдёт в Телемост как гость.",
                config.STORAGE_STATE, config.PROFILE_DIR,
            )

        self.playwright = await async_playwright().start()

        # Аргументы запуска Chromium — общие для обоих режимов.
        # headless=False ВСЕГДА (настройки HEADLESS больше нет): в настоящем headless-режиме
        # Chromium ничего не рисует в X11, поэтому ffmpeg писал чёрный экран. В Docker
        # headed-окно рисуется в виртуальный дисплей Xvfb (DISPLAY=:99).
        launch_args = [
            "--use-fake-ui-for-media-stream",  # не спрашивать разрешение камеры/микро всплывающим окном
            "--disable-blink-features=AutomationControlled",
            "--window-size=1920,1080",
            "--window-position=0,0",           # окно в левом верхнем углу — целиком попадает в кадр
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-infobars",              # убрать плашку "браузером управляет автотест"
            "--disable-session-crashed-bubble",  # не показывать "восстановить страницы?" поверх записи
            "--hide-crash-restore-bubble",
        ]
        context_kwargs = dict(
            viewport={"width": 1920, "height": 1080},
            permissions=["camera", "microphone"],
        )

        if config.STORAGE_STATE.exists():
            # ОСНОВНОЙ путь: портативная сессия storage_state.json (cookies+localStorage в JSON).
            # Кроссплатформенно: логинишься на Windows, а бот использует её в Linux/Docker.
            # Именно это чинит вход "гостем" — cookies из профиля Chromium шифруются ключом ОС
            # и на другой ОС не расшифровываются, а storage_state Playwright вливает куки заново.
            log.info(f"Использую портативную сессию: {config.STORAGE_STATE}")
            self._browser = await self.playwright.chromium.launch(headless=False, args=launch_args)
            self.context = await self._browser.new_context(
                storage_state=str(config.STORAGE_STATE), **context_kwargs
            )
        else:
            # ЗАПАСНОЙ путь: постоянный профиль Chromium. Работает только на той же ОС, где
            # логинились; при переносе Windows->Linux (Docker) cookies не расшифруются -> гость.
            log.warning(
                "storage_state.json не найден (%s) — откатываюсь на постоянный профиль %s. "
                "Если в Docker бот заходит ГОСТЕМ, это ожидаемо (cookies с Windows не читаются "
                "на Linux). Запусти scripts/save_session.py — он создаст storage_state.json.",
                config.STORAGE_STATE, config.PROFILE_DIR,
            )
            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(config.PROFILE_DIR),
                headless=False,
                args=launch_args,
                **context_kwargs,
            )
        log.debug(f"Профиль: {config.PROFILE_DIR}, storage_state: {config.STORAGE_STATE}")

        self.page = await self.context.new_page()
        self.page.on("console", lambda m: log.debug(f"[browser console:{m.type}] {m.text}"))
        self.page.on("pageerror", lambda err: log.error(f"[browser page error] {err}"))
        self.page.on("crash", lambda: log.error("[browser] СТРАНИЦА УПАЛА (crash)"))
        log.info("Браузер запущен (headed-режим; в Docker рисуется в виртуальный дисплей Xvfb)")

    async def join_meeting(self, url: str) -> bool:
        log.info(f"Открываю ссылку встречи: {url}")
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            log.error(f"Не удалось открыть страницу ({e})")
            await self._debug_screenshot("goto_failed")
            return False

        log.debug(f"Текущий URL после перехода: {self.page.url}")

        if "passport.yandex" in self.page.url:
            log.error(
                "Telemost перенаправил на страницу логина Яндекса — сохранённая сессия "
                "не найдена/протухла. Запусти scripts/save_session.py и залогинься вручную."
            )
            await self._debug_screenshot("not_logged_in")
            return False

        # Таймаут небольшой: под сохранённой сессией этот экран часто НЕ показывается
        # (сразу попадаем на превью камеры). Ждать по 10с на каждый селектор = ~20с зря
        # потерянного времени и записи на каждом /connect. required=False — нет экрана,
        # молча идём дальше.
        log.info("Шаг 1/3: экран 'Вы подключаетесь к видеовстрече' -> 'Продолжить в браузере'")
        continued = await self._click_first_matching(
            CONTINUE_IN_BROWSER_SELECTORS, timeout=4000, required=False
        )
        if continued:
            log.info("Клик по 'Продолжить в браузере' выполнен")
        else:
            log.debug("Экран 'Продолжить в браузере' не появился — возможно, сразу попали на превью камеры")
        await asyncio.sleep(1.5)

        # Кнопка "выключить микрофон/камеру" в превью существует, ТОЛЬКО если устройство
        # включено. В Docker реальных камеры/микрофона нет -> они и так выключены -> кнопок
        # turn-off-* просто нет, и селекторы законно не находятся (это НЕ ошибка). Таймауты
        # небольшие, чтобы не ждать зря перед входом.
        log.info("Шаг 2/3: выключаю микрофон и камеру на превью перед входом")
        mic_off = await self._click_first_matching(MUTE_MIC_SELECTORS, timeout=4000, required=False)
        cam_off = await self._click_first_matching(MUTE_CAM_SELECTORS, timeout=2500, required=False)
        if mic_off and cam_off:
            log.debug("Микрофон и камера выключены на превью")
        else:
            log.info(
                "Кнопки выключения не найдены (микрофон=%s, камера=%s) — скорее всего, реальных "
                "устройств нет (типично для Docker), поэтому вход и так без звука/видео.",
                mic_off, cam_off,
            )

        log.info("Шаг 3/3: жму 'Подключиться'")
        joined = await self._click_first_matching(JOIN_BUTTON_SELECTORS, timeout=15000, required=True)
        if not joined:
            log.error(
                "Кнопка 'Подключиться' не найдена — возможно, вёрстка изменилась "
                "или мы не дошли до экрана превью камеры"
            )
            await self._debug_screenshot("join_button_not_found")
            return False

        await asyncio.sleep(2)
        await self._debug_screenshot("after_join_attempt")
        log.info("Подключение к встрече выполнено")
        return True

    async def _click_first_matching(self, selectors: List[str], timeout: int = 10000, required: bool = True) -> bool:
        for sel in selectors:
            try:
                locator = self.page.locator(sel).first
                await locator.wait_for(state="visible", timeout=timeout)
                await locator.click()
                log.info(f"Клик сработал по селектору: {sel}")
                return True
            except Exception as e:
                log.debug(f"Селектор не сработал ({sel}): {type(e).__name__}: {e}")
                continue
        (log.error if required else log.debug)(f"Ни один селектор не сработал из списка: {selectors}")
        return False

    async def _debug_screenshot(self, tag: str) -> None:
        if not self.page:
            return
        try:
            path = config.DEBUG_DIR / f"{tag}_{int(time.time())}.png"
            await self.page.screenshot(path=str(path))
            log.debug(f"Скриншот сохранён: {path}")
        except Exception as e:
            log.error(f"Не удалось сделать скриншот ({tag}): {e}")

    async def wait_until_call_ends(self, poll_interval: int = 10) -> str:
        """
        Опрашивает страницу пока не найдёт признак конца звонка,
        либо пока кто-то не вызовет request_stop() (команда /stop).
        """
        log.info(f"Начинаю мониторинг звонка (проверка каждые {poll_interval}с)")
        while not self._stop_event.is_set():
            try:
                if self.page.is_closed():
                    log.warning("Вкладка была закрыта — считаю звонок завершённым")
                    return "page_closed"
                content = (await self.page.content()).lower()
                for marker in config.END_CALL_MARKERS:
                    if marker in content:
                        log.info(f"Найден признак завершения звонка на странице: '{marker}'")
                        return "end_marker"
            except Exception as e:
                log.warning(f"Ошибка при опросе страницы ({e}) — считаю, что звонок завершился")
                return "page_error"

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                pass  # обычный цикл опроса, идём дальше

        return "manual_stop"

    def request_stop(self) -> None:
        # Вызывается при ЛЮБОМ завершении: и по /stop, и по авто-детекту конца встречи —
        # поэтому формулировка нейтральная (раньше лог всегда писал "/stop", что путало).
        log.info("Получен сигнал остановки мониторинга встречи")
        self._stop_event.set()

    async def leave_and_close(self) -> None:
        log.info("Закрываю встречу и браузер...")
        for label, coro in (
            ("страница", self.page.close() if self.page and not self.page.is_closed() else None),
            ("контекст", self.context.close() if self.context else None),
            ("браузер", self._browser.close() if self._browser else None),
        ):
            if coro is None:
                continue
            try:
                await coro
                log.debug(f"Закрыто: {label}")
            except Exception as e:
                log.debug(f"Ошибка при закрытии ({label}): {e}")
        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception as e:
            log.debug(f"Ошибка при остановке playwright: {e}")
        log.info("Браузер закрыт")
