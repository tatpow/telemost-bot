import subprocess
import time
from pathlib import Path
from typing import Optional

import config
from utils import get_logger

log = get_logger("recorder")


class Recorder:
    """
    Обёртка над ffmpeg: захватывает виртуальный X11-дисплей (видео) и
    monitor виртуального PulseAudio-синка (звук браузера), пишет в mp4.

    Работает только внутри окружения с Xvfb+PulseAudio (см. entrypoint.sh /
    Docker). Если config.RECORD_ENABLED=false — просто ничего не делает,
    это нужно для теста логики подключения без записи (например, на Windows
    без Docker).
    """

    def __init__(self, meeting_tag: str):
        self.meeting_tag = meeting_tag
        self.process: Optional[subprocess.Popen] = None
        self.output_path: Optional[Path] = None

    def start(self) -> None:
        if not config.RECORD_ENABLED:
            log.warning("RECORD_ENABLED=false — запись пропущена (тестовый режим без записи)")
            return

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"{self.meeting_tag}_{timestamp}.mp4"
        self.output_path = config.RECORDINGS_DIR / filename

        cmd = [
            "ffmpeg", "-y",
            "-thread_queue_size", "512",
            "-f", "x11grab",
            "-video_size", config.VIDEO_SIZE,
            "-framerate", config.FRAMERATE,
            "-i", config.DISPLAY_NUM,
            "-thread_queue_size", "512",
            "-f", "pulse",
            "-i", f"{config.PULSE_SINK}.monitor",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "160k",
            str(self.output_path),
        ]
        log.debug("Команда ffmpeg: " + " ".join(cmd))

        try:
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            log.error(
                "ffmpeg не найден в PATH! Убедись, что он установлен "
                "(в Docker-образе уже есть, локально — установи вручную)."
            )
            return
        except Exception as e:
            log.error(f"Не удалось запустить ffmpeg: {e}")
            return

        log.info(f"ffmpeg запущен, PID={self.process.pid}")
        log.info(f"Файл записи: {self.output_path}")

        # Даём ffmpeg пару секунд и проверяем, что он не упал сразу
        time.sleep(2)
        if self.process.poll() is not None:
            log.error(f"ffmpeg завершился сразу же с кодом {self.process.returncode}! Вывод ffmpeg:")
            self._drain_output()

    def _drain_output(self) -> None:
        if not self.process or not self.process.stdout:
            return
        try:
            for line in self.process.stdout:
                log.debug(f"[ffmpeg] {line.rstrip()}")
        except Exception:
            pass

    def stop(self) -> Optional[Path]:
        if not self.process:
            log.warning("stop() вызван, но запись не запускалась (RECORD_ENABLED=false или ошибка старта)")
            return None

        log.info("Останавливаю ffmpeg мягко (сигнал 'q' на stdin, чтобы файл корректно закрылся)")
        try:
            self.process.communicate(input="q\n", timeout=15)
        except subprocess.TimeoutExpired:
            log.warning("ffmpeg не ответил на 'q' за 15с — убиваю процесс принудительно")
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        except Exception as e:
            log.error(f"Ошибка при остановке ffmpeg: {e}")

        log.info(f"ffmpeg остановлен, код возврата: {self.process.returncode}")

        if self.output_path and self.output_path.exists():
            size_mb = self.output_path.stat().st_size / (1024 * 1024)
            log.info(f"Файл записи готов: {self.output_path} ({size_mb:.1f} МБ)")
        else:
            log.error(
                "Файл записи не найден на диске! Проверь путь recordings/, "
                "права на запись и лог ffmpeg выше."
            )
        return self.output_path

    def is_output_valid(self, min_duration: float = 1.0) -> bool:
        """Проверяет через ffprobe, что записанный файл не битый.

        Возвращает True, если ffprobe видит корректную длительность >= min_duration секунд.
        Используется перед отправкой в Telegram и перед удалением локальной копии, чтобы
        не удалить единственную (пусть и битую) запись. Если ffprobe недоступен (например,
        локально на Windows без ffmpeg) — не блокируем отправку, считаем файл валидным,
        но пишем предупреждение.
        """
        if not self.output_path or not self.output_path.exists():
            return False
        if self.output_path.stat().st_size < 10 * 1024:  # < 10 КБ — почти наверняка мусор
            log.error("Файл записи подозрительно мал (<10 КБ) — считаю его битым")
            return False
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(self.output_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
            duration = float((result.stdout or "0").strip() or 0)
            ok = result.returncode == 0 and duration >= min_duration
            log.info(f"Проверка целостности (ffprobe): длительность={duration:.1f}с, валиден={ok}")
            if not ok and result.stderr.strip():
                log.error(f"ffprobe сообщил об ошибке: {result.stderr.strip()}")
            return ok
        except FileNotFoundError:
            log.warning("ffprobe не найден — пропускаю проверку целостности (считаю файл валидным)")
            return True
        except Exception as e:
            log.error(f"Ошибка ffprobe при проверке файла: {e}")
            return False
