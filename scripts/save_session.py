"""
Однократный скрипт: открывает видимый браузер, чтобы ты вручную залогинился
в корпоративный Яндекс-аккаунт. Сохраняет сессию ДВУМЯ способами:

1) storage_state.json (cookies+localStorage в JSON) — ОСНОВНОЙ, кроссплатформенный
   способ. Именно его использует бот, в т.ч. в Docker на Linux, даже если логинишься
   на Windows. Это чинит вход "гостем": cookies внутри профиля Chromium шифруются
   ключом ОС и при переносе Windows->Linux не расшифровываются, а storage_state
   Playwright вливает куки заново в любом окружении.
2) постоянный профиль browser_profile/ — запасной, работает только на той же ОС.

Запускать НЕ в Docker без GUI — нужно реально увидеть окно и ввести логин/пароль/2FA.
Проще всего — обычный python на своём Windows/Mac.

ВАЖНО: после входа НЕ закрывай окно браузера сам — вернись в терминал и нажми Enter,
иначе скрипт не успеет сохранить сессию.

Запуск:
    pip install playwright
    playwright install chromium
    python scripts/save_session.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402
import config  # noqa: E402
from utils import get_logger  # noqa: E402

log = get_logger("save_session")


async def main():
    log.info(f"Профиль браузера: {config.PROFILE_DIR}")
    log.info(f"Портативная сессия будет сохранена в: {config.STORAGE_STATE}")
    log.info("Открываю Chromium в видимом режиме...")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(config.PROFILE_DIR),
            headless=False,
            args=["--window-size=1400,900"],
        )
        page = await context.new_page()
        log.info("Перехожу на telemost.yandex.ru")
        await page.goto("https://telemost.yandex.ru")
        log.info(">>> Залогинься вручную в корпоративный Яндекс-аккаунт в открывшемся окне <<<")
        log.info(">>> Когда ВОЙДЁШЬ — вернись сюда, в терминал, и нажми Enter                 <<<")
        log.info(">>> НЕ закрывай окно браузера сам, иначе сессия не успеет сохраниться       <<<")

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, input, "Нажми Enter после входа в аккаунт... ")
        except (EOFError, KeyboardInterrupt):
            log.warning("Ввод прерван — всё равно пробую сохранить текущую сессию")

        # Основное: портативный storage_state (его использует бот, в т.ч. в Docker на Linux).
        try:
            await context.storage_state(path=str(config.STORAGE_STATE))
            log.info(f"[OK] Портативная сессия сохранена: {config.STORAGE_STATE}")
        except Exception as e:
            log.error(
                f"Не удалось сохранить storage_state ({e}). Похоже, окно закрыли раньше времени — "
                f"запусти скрипт заново и нажми Enter, НЕ закрывая окно браузера."
            )

        try:
            await context.close()
        except Exception:
            pass

    log.info("Готово! Теперь можно запускать bot.py (в т.ч. в Docker).")


if __name__ == "__main__":
    asyncio.run(main())
