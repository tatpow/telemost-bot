import asyncio
import time

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, FSInputFile

import config
from utils import get_logger
from browser_worker import TelemostWorker
from recorder import Recorder

log = get_logger("bot")

if not config.BOT_TOKEN:
    log.error(
        "BOT_TOKEN пустой! Проверь, что рядом с bot.py лежит файл .env "
        "(не .env.example) с заполненной строкой BOT_TOKEN=..., "
        f"ожидаемый путь: {config._ENV_PATH}"
    )
    raise SystemExit(1)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

# Упрощённо: одна активная встреча одновременно на весь бот-процесс.
state = {
    "worker": None,
    "recorder": None,
    "monitor_task": None,
    "status_task": None,
    "duration_task": None,
    "active": False,
    "started_at": None,  # time.monotonic() момента успешного подключения
    "thread_id": None,   # тема форума, где дали /connect — туда шлём статусы/видео (а не в General)
}


def _thread_kwargs() -> dict:
    """message_thread_id для ФОНОВЫХ сообщений бота (статусы, автостоп, отправка видео).

    message.reply(...) в хендлерах наследует тему входящего сообщения сам, а вот
    bot.send_message/bot.send_video по голому chat_id уходят в General форума —
    из-за этого запись и статусы попадали не в ту тему. Тему берём ту, где была
    дана /connect (сохранили при подключении). thread_id=None (обычная группа или
    General) -> ничего не добавляем.
    """
    tid = state.get("thread_id")
    return {"message_thread_id": tid} if tid else {}


async def is_admin(message: Message) -> bool:
    if config.GROUP_CHAT_ID and message.chat.id != config.GROUP_CHAT_ID:
        log.debug(f"Сообщение из чужого чата {message.chat.id} (ожидался {config.GROUP_CHAT_ID}), игнорирую")
        return False
    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        is_adm = member.status in ("administrator", "creator")
        log.debug(f"Проверка прав: user_id={message.from_user.id}, status={member.status}, is_admin={is_adm}")
        return is_adm
    except Exception as e:
        log.error(f"Не удалось проверить права администратора: {e}")
        return False


async def _wrong_context(message: Message, check_thread: bool = True) -> bool:
    """Возвращает True, если сообщение НЕ в разрешённом чате/теме — тогда handler просто выходит.

    Логика:
      - GROUP_CHAT_ID=0 -> ограничений нет (режим настройки; /id работает где угодно).
      - Чужой чат или личка -> молча игнорируем (ничего не отвечаем, чтобы не спамить).
      - Нужный чат, но не та тема (если задан GROUP_THREAD_ID и check_thread=True) ->
        подсказываем перейти в тему и выходим.
    """
    if config.GROUP_CHAT_ID == 0:
        return False

    if message.chat.id != config.GROUP_CHAT_ID:
        log.debug(f"Игнорирую сообщение из чужого чата/лички {message.chat.id}")
        return True

    if check_thread and config.GROUP_THREAD_ID != 0 and (message.message_thread_id or 0) != config.GROUP_THREAD_ID:
        cid = str(message.chat.id)
        internal = cid[4:] if cid.startswith("-100") else cid.lstrip("-")
        link = f"https://t.me/c/{internal}/{config.GROUP_THREAD_ID}"
        log.debug(f"Команда в неверной теме {message.message_thread_id} (нужна {config.GROUP_THREAD_ID})")
        try:
            await message.reply(
                f"⛔ Я работаю только в теме с thread_id <code>{config.GROUP_THREAD_ID}</code>.\n"
                f"Перейди в неё и повтори команду: {link}",
                parse_mode="HTML",
            )
        except Exception as e:
            log.debug(f"Не смог отправить подсказку про тему: {e}")
        return True

    return False


@dp.message(Command("connect"))
async def cmd_connect(message: Message):
    if await _wrong_context(message):
        return
    log.info(
        f"/connect от user_id={message.from_user.id} в chat_id={message.chat.id}, "
        f"thread_id={message.message_thread_id}"
    )

    if not await is_admin(message):
        await message.reply("⛔ Только админы группы могут подключать бота.")
        return

    if state["active"]:
        await message.reply("⚠️ Бот уже подключён к встрече. Сначала отправь /stop.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply("Использование: /connect <ссылка на Телемост>")
        return
    url = parts[1].strip()
    log.info(f"Ссылка на встречу: {url}")

    await message.reply("🔄 Подключаюсь к встрече, это может занять до минуты...")

    worker = TelemostWorker()
    recorder = Recorder(meeting_tag=f"chat{message.chat.id}")
    state.update(worker=worker, recorder=recorder, active=True, thread_id=message.message_thread_id)

    try:
        await worker.start_browser()
        ok = await worker.join_meeting(url)

        if not ok:
            await message.reply(
                "❌ Не удалось подключиться автоматически.\n"
                "Смотри debug_screenshots/ и консольные логи — там подробности."
            )
            await worker.leave_and_close()
            state.update(active=False, worker=None, recorder=None)
            return

        recorder.start()
        await message.reply("✅ Подключился к встрече и начал запись.\nДля остановки: /stop")

        state["started_at"] = time.monotonic()
        state["monitor_task"] = asyncio.create_task(_monitor_and_autostop(message.chat.id))
        state["status_task"] = asyncio.create_task(_status_reminder_loop(message.chat.id))
        state["duration_task"] = asyncio.create_task(_lecture_duration_reminder_loop(message.chat.id))

    except Exception as e:
        log.exception(f"Необработанная ошибка при подключении: {e}")
        await message.reply(f"❌ Ошибка при подключении: {e}")
        try:
            await worker.leave_and_close()
        except Exception:
            pass
        state.update(active=False, worker=None, recorder=None)


async def _monitor_and_autostop(chat_id: int):
    worker: TelemostWorker = state["worker"]
    reason = await worker.wait_until_call_ends(poll_interval=10)
    log.info(f"Мониторинг встречи завершён, причина: {reason}")
    if state["active"]:
        await _finish_session(chat_id, reason=reason)


async def _status_reminder_loop(chat_id: int):
    """Каждые STATUS_REMINDER_MINUTES минут напоминает в чат, что бот всё ещё пишет запись."""
    interval_sec = config.STATUS_REMINDER_MINUTES * 60
    if interval_sec <= 0:
        log.debug("STATUS_REMINDER_MINUTES=0 — авто-напоминания о статусе отключены")
        return
    log.debug(f"Запущен цикл авто-статуса, интервал {config.STATUS_REMINDER_MINUTES} мин")
    try:
        while state["active"]:
            await asyncio.sleep(interval_sec)
            if not state["active"]:
                break
            elapsed_min = int((time.monotonic() - state["started_at"]) / 60)
            log.debug(f"Авто-статус: бот работает уже {elapsed_min} мин")
            await bot.send_message(chat_id, f"🤖 Бот всё ещё подключён и пишет запись (уже {elapsed_min} мин).", **_thread_kwargs())
    except asyncio.CancelledError:
        log.debug("_status_reminder_loop отменён — сессия завершена")
    except Exception as e:
        log.error(f"Ошибка в _status_reminder_loop: {e}")


async def _lecture_duration_reminder_loop(chat_id: int):
    """
    Через LECTURE_DURATION_MINUTES (и затем повторно с тем же шагом) напоминает,
    что прошла стандартная длительность лекции и, возможно, пора отключаться.
    """
    interval_sec = config.LECTURE_DURATION_MINUTES * 60
    if interval_sec <= 0:
        log.debug("LECTURE_DURATION_MINUTES=0 — напоминание о завершении лекции отключено")
        return
    log.debug(f"Запущен цикл напоминания о длительности лекции, порог {config.LECTURE_DURATION_MINUTES} мин")
    try:
        while state["active"]:
            await asyncio.sleep(interval_sec)
            if not state["active"]:
                break
            elapsed_min = int((time.monotonic() - state["started_at"]) / 60)
            log.info(f"Напоминание о длительности: прошло {elapsed_min} мин")
            await bot.send_message(
                chat_id,
                f"⏰ Прошло уже {elapsed_min} мин — это стандартная длительность лекции "
                f"({config.LECTURE_DURATION_MINUTES} мин). Возможно, она уже закончилась — "
                "если так, не забудьте отправить /stop.",
                **_thread_kwargs(),
            )
    except asyncio.CancelledError:
        log.debug("_lecture_duration_reminder_loop отменён — сессия завершена")
    except Exception as e:
        log.error(f"Ошибка в _lecture_duration_reminder_loop: {e}")


async def _finish_session(chat_id: int, reason: str = "manual"):
    if not state["active"]:
        log.debug("_finish_session вызван без активной сессии — пропускаю")
        return

    log.info(f"Завершаю сессию встречи (причина: {reason})")
    worker: TelemostWorker = state["worker"]
    recorder: Recorder = state["recorder"]

    # Помечаем неактивной СРАЗУ — это условие выхода из циклов-напоминаний выше
    state["active"] = False

    # Отменяем фоновые задачи, кроме текущей (если finish_session вызван из одной из них же)
    current = asyncio.current_task()
    for task_key in ("monitor_task", "status_task", "duration_task"):
        task = state.get(task_key)
        if task and not task.done() and task is not current:
            task.cancel()

    worker.request_stop()
    output_path = recorder.stop()
    await worker.leave_and_close()

    state.update(worker=None, recorder=None, started_at=None)

    if not (output_path and output_path.exists()):
        await bot.send_message(chat_id, "⚠️ Файл записи не найден — что-то пошло не так, смотри консольные логи.", **_thread_kwargs())
        return

    size_mb = output_path.stat().st_size / (1024 * 1024)

    # Проверяем целостность записи через ffprobe ДО отправки/удаления, чтобы случайно
    # не удалить единственную (пусть и битую) копию. Если файл битый — оставляем на сервере.
    if not recorder.is_output_valid():
        await bot.send_message(
            chat_id,
            f"⚠️ Файл записи ({output_path.name}, {size_mb:.1f} МБ) выглядит битым "
            "(ffprobe не увидел корректной длительности). НЕ удаляю его — забери с сервера "
            "из папки recordings/ и проверь вручную.",
            **_thread_kwargs(),
        )
        return

    await bot.send_message(chat_id, f"⏹ Запись остановлена. Файл: {output_path.name} ({size_mb:.1f} МБ)", **_thread_kwargs())

    if size_mb >= 45:  # запас от лимита Bot API (~50 МБ на файл)
        await bot.send_message(
            chat_id,
            "Файл больше 45 МБ — не отправляю его в Telegram напрямую (лимит Bot API). "
            "Забери его с сервера из папки recordings/. Файл НЕ удалён.",
            **_thread_kwargs(),
        )
        return

    try:
        await bot.send_video(chat_id, FSInputFile(str(output_path)), **_thread_kwargs())
        log.info("Файл отправлен в чат")
    except Exception as e:
        log.error(f"Не удалось отправить видео в Telegram: {e}")
        await bot.send_message(
            chat_id,
            "Не смог отправить файл в чат (см. логи) — файл НЕ удалён, забери его вручную с сервера.",
            **_thread_kwargs(),
        )
        return

    # Дошли сюда — значит запись валидна и успешно отправлена. Теперь можно освободить место.
    if config.DELETE_AFTER_SEND:
        try:
            output_path.unlink()
            log.info(f"Локальная копия удалена после успешной отправки: {output_path}")
            await bot.send_message(chat_id, "🧹 Локальная копия удалена с сервера (место освобождено).", **_thread_kwargs())
        except Exception as e:
            log.error(f"Не удалось удалить файл {output_path}: {e}")
            await bot.send_message(chat_id, "Отправил, но не смог удалить локальную копию (см. логи).", **_thread_kwargs())


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    if await _wrong_context(message):
        return
    log.info(f"/stop от user_id={message.from_user.id}")
    if not await is_admin(message):
        await message.reply("⛔ Только админы группы могут останавливать запись.")
        return
    if not state["active"]:
        await message.reply("Сейчас нет активного подключения.")
        return
    await message.reply("🛑 Останавливаю запись и отключаюсь...")
    await _finish_session(message.chat.id, reason="manual_stop")


@dp.message(Command("id"))
async def cmd_id(message: Message):
    """Утилита настройки: показать ID чата/темы и user_id.

    Ограничена только по чату (в чужих чатах и личке молчит, если GROUP_CHAT_ID задан),
    но НЕ по теме — иначе нельзя было бы узнать thread_id новой темы. Пока GROUP_CHAT_ID=0,
    работает где угодно.
    """
    if await _wrong_context(message, check_thread=False):
        return
    log.info(f"/id от user_id={message.from_user.id} в chat_id={message.chat.id}")
    chat = message.chat
    lines = [
        "🆔 <b>Идентификаторы</b>",
        f"chat_id: <code>{chat.id}</code>",
        f"тип чата: <code>{chat.type}</code>",
    ]
    if message.message_thread_id:
        lines.append(f"thread_id (тема): <code>{message.message_thread_id}</code>")
    if message.from_user:
        lines.append(f"твой user_id: <code>{message.from_user.id}</code>")
    lines.append("")
    lines.append(
        "Чтобы бот работал только здесь, впиши <code>chat_id</code> в "
        "<code>GROUP_CHAT_ID</code> (а при желании ограничить темой — <code>thread_id</code> "
        "в <code>GROUP_THREAD_ID</code>) в файле <code>.env</code> и перезапусти бота."
    )
    await message.reply("\n".join(lines), parse_mode="HTML")


HELP_TEXT = (
    "🤖 <b>Команды бота</b>\n\n"
    "Только для админов группы:\n"
    "/connect &lt;ссылка&gt; — подключиться к встрече Телемоста и начать запись\n"
    "/stop — остановить запись и выйти из встречи\n"
    "/status — активна ли сейчас сессия записи\n"
    "/help — это сообщение\n\n"
    "Доступно всем (для настройки):\n"
    "/id — показать ID этого чата/темы и твой user_id (нужно для GROUP_CHAT_ID/GROUP_THREAD_ID в .env)\n\n"
    "Пока запись идёт, бот сам иногда пишет в чат:\n"
    f"• каждые {config.STATUS_REMINDER_MINUTES} мин — что всё ещё работает\n"
    f"• после {config.LECTURE_DURATION_MINUTES} мин — что лекция, возможно, уже закончилась"
)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    if await _wrong_context(message):
        return
    log.info(f"/help от user_id={message.from_user.id}")
    if not await is_admin(message):
        await message.reply("⛔ Эта команда доступна только админам группы.")
        return
    await message.reply(HELP_TEXT, parse_mode="HTML")


@dp.message(Command("status"))
async def cmd_status(message: Message):
    if await _wrong_context(message):
        return
    log.info(f"/status от user_id={message.from_user.id}")
    if not await is_admin(message):
        await message.reply("⛔ Эта команда доступна только админам группы.")
        return
    await message.reply("🟢 Бот подключён к встрече и пишет запись" if state["active"] else "⚪ Бот сейчас не подключён")


async def main():
    log.info("=" * 70)
    log.info("Запуск Telegram-бота для авто-подключения к Яндекс Телемосту")
    log.info(f"RECORD_ENABLED={config.RECORD_ENABLED}  DISPLAY={config.DISPLAY_NUM}  (браузер всегда headed)")
    log.info(
        f"STATUS_REMINDER_MINUTES={config.STATUS_REMINDER_MINUTES}  "
        f"LECTURE_DURATION_MINUTES={config.LECTURE_DURATION_MINUTES}"
    )
    log.info(f"Профиль браузера: {config.PROFILE_DIR}")
    log.info(f"Папка записей:   {config.RECORDINGS_DIR}")
    log.info("=" * 70)

    if not config.BOT_TOKEN:
        log.error("BOT_TOKEN не задан! Проверь .env / переменные окружения.")
        return

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
