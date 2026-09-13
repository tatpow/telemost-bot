#!/bin/bash
set -e

echo "[entrypoint] Запуск Xvfb на дисплее ${DISPLAY:-:99}"
Xvfb ${DISPLAY:-:99} -screen 0 1920x1080x24 &
sleep 1

echo "[entrypoint] Запуск PulseAudio (виртуальный звук, без физической звуковухи)"
pulseaudio -D --exit-idle-time=-1 --disallow-exit --log-target=stderr
sleep 1

echo "[entrypoint] Создаю виртуальный аудио-синк MeetingSink"
pactl load-module module-null-sink sink_name=MeetingSink sink_properties=device.description=MeetingSink
pactl set-default-sink MeetingSink
pactl set-default-source MeetingSink.monitor

echo "[entrypoint] Доступные аудио-устройства:"
pactl list short sinks
pactl list short sources

echo "[entrypoint] Запуск Telegram-бота"
exec python bot.py
