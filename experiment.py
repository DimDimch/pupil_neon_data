#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Experiment controller — Pupil Neon eye-tracking cognitive experiment.

Запуск:
    python experiment.py

Изменения относительно 3.py:
  - Ввод данных через tkinter-форму (ui_setup.py) вместо input()
  - Все данные айтрекера стримятся на компьютер в реальном времени (data_collector.py)
  - Данные сохраняются в папку data/{Subject}_{Group}_S{N}_{datetime}/
  - Все task-функции (GAP, OVERLAP, PREDICTION, DECISION, ANTISACCADE) без изменений
"""

from __future__ import annotations

import asyncio
import csv
import math
import os
import random
import sys
import time
import json
from datetime import datetime
from typing import Any

import pygame
import simpleobsws

# ─── FIX SSL: отключаем SSL по умолчанию для aiohttp ───
# Pupil Neon API работает по HTTP, но aiohttp может пытаться SSL.
try:
    import aiohttp
    _orig_tcp_connector_init = aiohttp.TCPConnector.__init__

    def _patched_tcp_connector_init(self, *args, **kwargs):
        if "ssl" not in kwargs:
            kwargs["ssl"] = False
        _orig_tcp_connector_init(self, *args, **kwargs)

    aiohttp.TCPConnector.__init__ = _patched_tcp_connector_init
except Exception:
    pass
# ─── конец FIX SSL ───

from pupil_labs.realtime_api.device import Device
from pupil_labs.realtime_api.time_echo import TimeOffsetEstimator

from ui_setup import show_setup_form
from data_collector import DataCollector


# ═══════════════════════════════════════════════════════════════════
#  SETUP: Tkinter form (до инициализации pygame!)
# ═══════════════════════════════════════════════════════════════════

config = show_setup_form()
if config is None:
    print("Эксперимент отменён пользователем.")
    sys.exit(0)

subject_id = config["subject_id"]
group_id = config["group_id"]
age = config["age"]
gender = config["gender"]
session = config["session"]
VIEW_DIST_CM = float(config["view_dist_cm"])
MONITOR_WIDTH_CM_VAL = float(config["monitor_width_cm"])
current_ip = config["device_ip"]

# Выходная папка
ts_str = datetime.now().strftime("%Y%m%d_%H%M")
folder_name = f"{subject_id}_{group_id}_S{session}_{ts_str}"
output_dir = os.path.join("data", folder_name)
os.makedirs(output_dir, exist_ok=True)

print(f"\n📁 Данные будут сохранены в: {output_dir}")


# ═══════════════════════════════════════════════════════════════════
#  Helpers (без изменений)
# ═══════════════════════════════════════════════════════════════════

async def get_time_offset(dev_handle: Device) -> int:
    """Оцениваем смещение часов айтрекера относительно хоста (нс)."""
    print("[DEBUG] Запрашиваем Time Offset...")
    try:
        async with asyncio.timeout(5):
            status = await dev_handle.get_status()
            if not status.phone.time_echo_port:
                print("⚠️ Time‑Echo недоступен, offset=0")
                return 0
            est = TimeOffsetEstimator(status.phone.ip, status.phone.time_echo_port)
            res = await est.estimate()
            if not res:
                print("⚠️ Смещение не получено, offset=0")
                return 0
            print(f"🕒 Offset: {res.time_offset_ms.mean:.2f} ms")
            return int(res.time_offset_ms.mean)
    except TimeoutError:
        print("⚠️ Ошибка: Таймаут при получении Time Offset. Используем 0.")
        return 0
    except Exception as e:
        print(f"⚠️ Ошибка Time Offset: {e}")
        return 0


async def check_device_status(dev_handle: Device):
    """Безопасная проверка статуса."""
    print("[DEBUG] Запрашиваем статус устройства (батарея/память)...")
    try:
        async with asyncio.timeout(5):
            status = await dev_handle.get_status()
            battery = status.phone.battery_level
            print(f"🔋 Заряд батареи: {battery}%")
            if battery < 20:
                print("\n" + "!" * 40)
                print("⚠️ ВНИМАНИЕ! НИЗКИЙ ЗАРЯД БАТАРЕИ!")
                print("Подключите питание, иначе запись прервется.")
                print("!" * 40 + "\n")
                await asyncio.sleep(3)

            free_gb = status.phone.storage_free / (1024 ** 3)
            print(f"💾 Свободно места: {free_gb:.2f} GB")
            if free_gb < 2.0:
                print("\n" + "!" * 40)
                print("⚠️ ВНИМАНИЕ! МАЛО МЕСТА (< 2 GB)!")
                print("!" * 40 + "\n")
                await asyncio.sleep(3)

            print("✅ Статус устройства проверен.")
    except TimeoutError:
        print("⚠️ Не удалось получить статус устройства (таймаут). Пропускаем проверку.")
    except Exception as e:
        print(f"⚠️ Ошибка при проверке статуса: {e}")


async def async_wait_for_recording_begin(dev_handle: Device, timeout_seconds=10) -> bool:
    """Ждём события recording.begin."""
    print("⏳ Ожидаем подтверждения старта записи от очков...")
    try:
        async with asyncio.timeout(timeout_seconds):
            async for comp in dev_handle.status_updates():
                if getattr(comp, "name", None) == "recording.begin":
                    await dev_handle.send_event("sync_start")
                    return True
    except (asyncio.TimeoutError, Exception):
        pass
    print("⚠️ Внимание: Очки не прислали подтверждение 'recording.begin'.")
    return False


async def start_obs_recording():
    print("[DEBUG] Подключение к OBS...")
    try:
        ws = simpleobsws.WebSocketClient(
            url=f"ws://{OBS_HOST}:{OBS_PORT}", password=OBS_PASSWORD
        )
        await ws.connect()
        await ws.wait_until_identified()
        result = await ws.call(simpleobsws.Request("StartRecord"))
        if result.ok:
            print("🎥 OBS запись началась!")
        else:
            print(f"⚠️ Ошибка запуска записи OBS: {result.status}")
        await ws.disconnect()
    except Exception as e:
        print(f"⚠️ Ошибка подключения к OBS: {e}")


async def stop_obs_recording():
    print("[DEBUG] Остановка OBS...")
    try:
        ws = simpleobsws.WebSocketClient(
            url=f"ws://{OBS_HOST}:{OBS_PORT}", password=OBS_PASSWORD
        )
        await ws.connect()
        await ws.wait_until_identified()
        result = await ws.call(simpleobsws.Request("StopRecord"))
        if result.ok:
            print("⏹️ OBS запись остановлена!")
        else:
            print(f"⚠️ Ошибка остановки записи OBS: {result.status}")
        await ws.disconnect()
    except Exception as e:
        print(f"⚠️ Ошибка OBS: {e}")


# ═══════════════════════════════════════════════════════════════════
#  Глобальные переменные
# ═══════════════════════════════════════════════════════════════════

device: Device | None = None
recording_running = False
time_offset_ns = 0
collector: DataCollector | None = None  # <-- NEW

# Пользовательские константы
OBS_HOST, OBS_PORT, OBS_PASSWORD = "localhost", 4455, "XHSKSGDHSFYSLBSt"

# Тайминги блоков
FIXATION_DURATION = 2.8
GAP_TIME = 0.2
TARGET_DURATION = 1.5
ITI = 0.5
PRED_CS_DUR = 1.0
PRED_TARGET_DUR = 1.0
DECISION_FIX = 2.8
DECISION_TG = 1.5
DECISION_FB = 1.5
ANTI_FIX = 2.8
ANTI_RED = 1.5
ANTI_GREEN = 1.5

# Цвета
COLOR_GREEN, COLOR_RED, BG_COLOR = (0, 255, 0), (255, 0, 0), (0, 0, 0)

# ═══════════════════════════════════════════════════════════════════
#  Инициализация pygame (ПОСЛЕ tkinter)
# ═══════════════════════════════════════════════════════════════════

pygame.init()
display_info = pygame.display.Info()
WIN_WIDTH, WIN_HEIGHT = display_info.current_w, display_info.current_h
screen = pygame.display.set_mode((WIN_WIDTH, WIN_HEIGHT), pygame.FULLSCREEN)
pygame.display.set_caption("Experiment")
FPS, clock = 60, pygame.time.Clock()

MONITOR_WIDTH_CM = MONITOR_WIDTH_CM_VAL
PX_PER_CM = WIN_WIDTH / MONITOR_WIDTH_CM


def deg2px(angle_deg: float) -> int:
    size_cm = 2 * VIEW_DIST_CM * math.tan(math.radians(angle_deg / 2))
    return int(round(size_cm * PX_PER_CM))


SIDE_16 = deg2px(16)
CS_RADIUS = deg2px(0.5)
TARGET_RADIUS = CS_RADIUS


# ═══════════════════════════════════════════════════════════════════
#  Структуры данных и утилиты (без изменений)
# ═══════════════════════════════════════════════════════════════════

active_events: dict = {}


def adjusted_time_ns() -> int:
    return time.time_ns() - time_offset_ns


def draw_circle(surf, color, x, y, r):
    pygame.draw.circle(surf, color, (int(x), int(y)), r)


def load_scaled_marker(path: str, scale: float = 1.0) -> pygame.Surface:
    if not os.path.exists(path):
        surf = pygame.Surface((100, 100))
        surf.fill((255, 0, 255))
        return surf
    surf = pygame.image.load(path).convert()
    if scale != 1.0:
        w, h = surf.get_size()
        new_size = (int(w / scale), int(h / scale))
        surf = pygame.transform.smoothscale(surf, new_size)
    return surf


# Маркеры углов и периметра
MARKER_FOLDER = os.path.expanduser("~/Desktop/МАРКЕРЫ")
corner_surfaces = {}

for i in range(24, 34):
    f_path = os.path.join(MARKER_FOLDER, f"MAP_{i}.png")
    corner_surfaces[f"MAP_{i}"] = load_scaled_marker(f_path, scale=3.0)


def draw_corner_markers():
    """Рисуем 10 маркеров (24-33)."""
    cx = WIN_WIDTH // 2
    cy = WIN_HEIGHT // 2
    m_h = corner_surfaces["MAP_24"].get_height()

    top_center_y = m_h // 2
    bottom_center_y = WIN_HEIGHT - (m_h // 2)
    mid_top_y = (top_center_y + cy) // 2
    mid_bottom_y = (bottom_center_y + cy) // 2

    s = corner_surfaces["MAP_24"];
    screen.blit(s, (0, 0))
    s = corner_surfaces["MAP_25"];
    screen.blit(s, (cx - s.get_width() // 2, 0))
    s = corner_surfaces["MAP_26"];
    screen.blit(s, (WIN_WIDTH - s.get_width(), 0))

    s = corner_surfaces["MAP_27"];
    screen.blit(s, (WIN_WIDTH - s.get_width(), mid_top_y - s.get_height() // 2))
    s = corner_surfaces["MAP_28"];
    screen.blit(s, (WIN_WIDTH - s.get_width(), mid_bottom_y - s.get_height() // 2))
    s = corner_surfaces["MAP_29"];
    screen.blit(s, (WIN_WIDTH - s.get_width(), WIN_HEIGHT - s.get_height()))

    s = corner_surfaces["MAP_30"];
    screen.blit(s, (cx - s.get_width() // 2, WIN_HEIGHT - s.get_height()))
    s = corner_surfaces["MAP_31"];
    screen.blit(s, (0, WIN_HEIGHT - s.get_height()))

    s = corner_surfaces["MAP_32"];
    screen.blit(s, (0, mid_bottom_y - s.get_height() // 2))
    s = corner_surfaces["MAP_33"];
    screen.blit(s, (0, mid_top_y - s.get_height() // 2))


_orig_flip = pygame.display.flip


def _flip_and_markers():
    draw_corner_markers()
    _orig_flip()


pygame.display.flip = _flip_and_markers


def draw_and_flip(x=None, y=None, color=BG_COLOR, radius=None):
    screen.fill(BG_COLOR)
    if x is not None and y is not None:
        pygame.draw.circle(screen, color, (int(x), int(y)), radius)
    pygame.display.flip()
    clock.tick(FPS)


def safe_exit():
    pygame.quit()
    sys.exit()


def process_pygame_events():
    for ev in pygame.event.get():
        if ev.type == pygame.QUIT or (ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE):
            safe_exit()


# ═══════════════════════════════════════════════════════════════════
#  Логирование событий (без изменений)
# ═══════════════════════════════════════════════════════════════════

def start_event(task, dot_type, dot_color, x_px, y_px):
    ts = adjusted_time_ns()
    key = (task, dot_type, ts)
    active_events[key] = {
        "task_name": task, "dot_type": dot_type,
        "dot_color": dot_color, "time_start": ts,
        "x_px": x_px, "y_px": y_px
    }
    return key


def end_event(csv_writer, key, dot_is_correct=None):
    te = adjusted_time_ns()
    if key in active_events:
        data = active_events.pop(key)
        csv_writer.writerow([
            data["task_name"], data["dot_type"], data["dot_color"],
            dot_is_correct, data["time_start"], te,
            data["x_px"], data["y_px"]
        ])


# ═══════════════════════════════════════════════════════════════════
#  GUI
# ═══════════════════════════════════════════════════════════════════

def show_instruction(text):
    font = pygame.font.SysFont("Arial", 36)
    surf = pygame.Surface((WIN_WIDTH, WIN_HEIGHT))
    surf.fill(BG_COLOR)
    y = 100
    for line in text.split("\n"):
        r = font.render(line, True, (255, 255, 255))
        surf.blit(r, r.get_rect(center=(WIN_WIDTH // 2, y)))
        y += 60
    screen.blit(surf, (0, 0))
    pygame.display.flip()
    while True:
        process_pygame_events()
        if pygame.key.get_pressed()[pygame.K_SPACE]:
            break


def wait_for_screenshot_setup(marker_folder, scale=3.0):
    print("\n" + "=" * 50)
    print(" 📸 РЕЖИМ СКРИНШОТА ")
    print("1. На экране: Сетка (0-8) + Рамка (24-33).")
    print("2. Сделайте скриншот: Shift + Command + 3.")
    print("3. Нажмите ПРОБЕЛ (SPACE), чтобы начать соединение.")
    print("=" * 50 + "\n")

    cx, cy = WIN_WIDTH // 2, WIN_HEIGHT // 2
    degs = [0, 16, 12, 8, 4]

    positions = []
    marker_id = 0
    for d in degs:
        if d == 0:
            positions.append((marker_id, cx, cy))
            marker_id += 1
            continue
        dx = deg2px(d)
        positions.append((marker_id, cx - dx, cy))
        marker_id += 1
        positions.append((marker_id, cx + dx, cy))
        marker_id += 1

    images = []
    for mid, x, y in positions:
        path = os.path.join(marker_folder, f"attachment {mid}.jpeg")
        if os.path.exists(path):
            img = load_scaled_marker(path, scale=scale)
            rect = img.get_rect(center=(x, y))
            images.append((img, rect))
        else:
            print(f"⚠️ Marker {mid} not found at {path}")

    waiting = True
    while waiting:
        process_pygame_events()
        screen.fill(BG_COLOR)
        for img, rect in images:
            screen.blit(img, rect)
        pygame.display.flip()
        if pygame.key.get_pressed()[pygame.K_SPACE]:
            waiting = False
        clock.tick(30)

    screen.fill(BG_COLOR)
    pygame.display.flip()
    time.sleep(0.5)


async def show_marker(csv_writer, marker, dur=2.0):
    for suffix in ("START", "END"):
        tag = f"{marker}_{suffix}"
        if device:
            try:
                await device.send_event(tag, adjusted_time_ns())
            except Exception:
                pass
        csv_writer.writerow(["MARKER", tag, None, None, adjusted_time_ns(), None, None, None])
        if suffix == "START":
            start = time.time()
            font = pygame.font.SysFont("Arial", 50)
            while time.time() - start < dur:
                process_pygame_events()
                screen.fill((255, 255, 255))
                r = font.render(marker, True, (0, 0, 0))
                screen.blit(r, r.get_rect(center=(WIN_WIDTH // 2, WIN_HEIGHT // 2)))
                pygame.display.flip()
                clock.tick(FPS)


async def show_apriltag_sequence(writer, dev, marker_folder, show_ms=1000, scale=3.0):
    font = pygame.font.SysFont("Arial", 30)
    cx, cy = WIN_WIDTH // 2, WIN_HEIGHT // 2
    degs = [0, 16, 12, 8, 4]
    positions, marker_id = [], 0
    for d in degs:
        if d == 0:
            positions.append((marker_id, cx, cy))
            marker_id += 1
            continue
        dx = deg2px(d)
        positions.append((marker_id, cx - dx, cy))
        marker_id += 1
        positions.append((marker_id, cx + dx, cy))
        marker_id += 1

    for mid, x, y in positions:
        path = os.path.join(marker_folder, f"attachment {mid}.jpeg")
        if not os.path.exists(path):
            print(f"⚠️ Файл не найден: {path}")
            continue
        img = load_scaled_marker(path, scale=scale)
        rect = img.get_rect(center=(x, y))

        tag = f"APRILTAG_{mid:02d}"
        ts_start = adjusted_time_ns()
        if dev: await dev.send_event(f"{tag}_START", ts_start)
        writer.writerow(["CALIB", tag, "START", None, ts_start, None, x, y])

        while True:
            process_pygame_events()
            screen.fill(BG_COLOR)
            screen.blit(img, rect)
            txt = font.render(f"Маркер ID {mid} — SPACE для показа", True, (0, 255, 0))
            screen.blit(txt, txt.get_rect(center=(WIN_WIDTH // 2, 70)))
            pygame.display.flip()
            clock.tick(FPS)
            if pygame.key.get_pressed()[pygame.K_SPACE]:
                break
        while any(pygame.key.get_pressed()):
            pygame.event.pump()

        t0 = pygame.time.get_ticks()
        while pygame.time.get_ticks() - t0 < show_ms:
            process_pygame_events()
            screen.fill(BG_COLOR)
            screen.blit(img, rect)
            pygame.display.flip()
            clock.tick(FPS)

        ts_end = adjusted_time_ns()
        if dev: await dev.send_event(f"{tag}_END", ts_end)
        writer.writerow(["CALIB", tag, "END", None, ts_end, None, x, y])


# ═══════════════════════════════════════════════════════════════════
#  Tasks (БЕЗ ИЗМЕНЕНИЙ)
# ═══════════════════════════════════════════════════════════════════

async def run_gap_task(csv_writer, n_trials=20):
    show_instruction(f"[GAP TASK]\nНажмите ПРОБЕЛ\n(всего {n_trials} триалов)")
    await show_marker(csv_writer, "START_GAP_BLOCK")
    cx, cy = WIN_WIDTH // 2, WIN_HEIGHT // 2

    steps = [1.7, 2.0, 2.3, 2.6, 2.9]
    durations = steps * (math.ceil(n_trials / len(steps)))
    durations = durations[:n_trials]
    random.shuffle(durations)

    for i in range(1, n_trials + 1):
        if device: await device.send_event(f"{i}.1_CENTER_start", adjusted_time_ns())
        ev_c = start_event("GAP", "CENTER", "GREEN", cx, cy)

        cs_duration = durations.pop()
        print(f"Trial {i}: Fixation duration = {cs_duration:.2f} sec")

        till = time.time() + cs_duration
        while time.time() < till:
            process_pygame_events();
            draw_and_flip(cx, cy, COLOR_GREEN, CS_RADIUS)
        end_event(csv_writer, ev_c)
        if device: await device.send_event(f"{i}.1_CENTER_end", adjusted_time_ns())

        gap_end = time.time() + GAP_TIME
        while time.time() < gap_end:
            process_pygame_events();
            draw_and_flip()

        side = random.choice(["LEFT", "RIGHT"])
        x = cx + SIDE_16 if side == "RIGHT" else cx - SIDE_16
        if device: await device.send_event(f"{i}.2_{side}_start", adjusted_time_ns())
        ev_t = start_event("GAP", side, "GREEN", x, cy)
        till = time.time() + TARGET_DURATION
        while time.time() < till:
            process_pygame_events();
            draw_and_flip(x, cy, COLOR_GREEN, TARGET_RADIUS)
        end_event(csv_writer, ev_t)
        if device: await device.send_event(f"{i}.2_{side}_end", adjusted_time_ns())

        iti_end = time.time() + ITI
        while time.time() < iti_end:
            process_pygame_events();
            draw_and_flip()
    await show_marker(csv_writer, "END_GAP_BLOCK")


async def run_overlap_task(csv_writer, n_trials=20):
    show_instruction(f"[OVERLAP TASK]\nНажмите ПРОБЕЛ\n(всего {n_trials} триалов)")
    await show_marker(csv_writer, "START_OVERLAP_BLOCK")
    cx, cy = WIN_WIDTH // 2, WIN_HEIGHT // 2

    steps = [1.7, 2.0, 2.3, 2.6, 2.9]
    durations = steps * (math.ceil(n_trials / len(steps)))
    durations = durations[:n_trials]
    random.shuffle(durations)

    for i in range(1, n_trials + 1):
        side = random.choice(["LEFT", "RIGHT"])
        x = cx + SIDE_16 if side == "RIGHT" else cx - SIDE_16

        if device: await device.send_event(f"{i}.1_CENTER_start", adjusted_time_ns())
        ev_c = start_event("OVERLAP", "CENTER", "GREEN", cx, cy)

        foreperiod = durations.pop()
        print(f"Trial {i} (Overlap): Foreperiod = {foreperiod:.2f} sec")

        t_start = time.time()
        target_onset = t_start + foreperiod
        total_end = target_onset + TARGET_DURATION

        target_shown = False
        ev_t = None

        while time.time() < total_end:
            process_pygame_events()
            now = time.time()

            if not target_shown and now >= target_onset:
                target_shown = True
                if device: await device.send_event(f"{i}.2_{side}_start", adjusted_time_ns())
                ev_t = start_event("OVERLAP", side, "GREEN", x, cy)

            screen.fill(BG_COLOR)
            draw_circle(screen, COLOR_GREEN, cx, cy, CS_RADIUS)
            if target_shown:
                draw_circle(screen, COLOR_GREEN, x, cy, TARGET_RADIUS)

            pygame.display.flip()
            clock.tick(FPS)

        end_event(csv_writer, ev_c)
        if device: await device.send_event(f"{i}.1_CENTER_end", adjusted_time_ns())

        if target_shown:
            end_event(csv_writer, ev_t)
            if device: await device.send_event(f"{i}.2_{side}_end", adjusted_time_ns())

        iti_end = time.time() + ITI
        while time.time() < iti_end:
            process_pygame_events();
            draw_and_flip()
    await show_marker(csv_writer, "END_OVERLAP_BLOCK")


async def run_prediction_task(csv_writer, n_trials=20):
    show_instruction(f"[PREDICTION TASK]\nНажмите ПРОБЕЛ\n(всего {n_trials} триалов)")
    await show_marker(csv_writer, "START_PREDICTION_BLOCK")
    cx, cy = WIN_WIDTH // 2, WIN_HEIGHT // 2
    current = random.choice(["LEFT", "RIGHT"])
    for i in range(1, n_trials + 1):
        if device: await device.send_event(f"{i}.1_CENTER_start", adjusted_time_ns())
        ev_c = start_event("PREDICTION", "CENTER", "GREEN", cx, cy)
        until = time.time() + PRED_CS_DUR
        while time.time() < until:
            process_pygame_events();
            draw_and_flip(cx, cy, COLOR_GREEN, CS_RADIUS)
        end_event(csv_writer, ev_c)
        if device: await device.send_event(f"{i}.1_CENTER_end", adjusted_time_ns())

        x = cx + SIDE_16 if current == "RIGHT" else cx - SIDE_16
        if device: await device.send_event(f"{i}.2_{current}_start", adjusted_time_ns())
        ev_t = start_event("PREDICTION", current, "GREEN", x, cy)
        until = time.time() + PRED_TARGET_DUR
        while time.time() < until:
            process_pygame_events();
            draw_and_flip(x, cy, COLOR_GREEN, TARGET_RADIUS)
        end_event(csv_writer, ev_t)
        if device: await device.send_event(f"{i}.2_{current}_end", adjusted_time_ns())
        current = "LEFT" if current == "RIGHT" else "RIGHT"
        iti_end = time.time() + ITI
        while time.time() < iti_end:
            process_pygame_events();
            draw_and_flip()
    await show_marker(csv_writer, "END_PREDICTION_BLOCK")


async def run_decision_task(csv_writer, n_trials=20):
    show_instruction(f"[DECISION TASK]\nНажмите ПРОБЕЛ\n(всего {n_trials} триалов)")
    await show_marker(csv_writer, "START_DECISION_BLOCK")
    cx, cy = WIN_WIDTH // 2, WIN_HEIGHT // 2
    angles = [(16, 12), (16, 8), (12, 8), (8, 4)]
    for i in range(1, n_trials + 1):
        far, near = random.choice(angles)
        if far < near: far, near = near, far
        on_left = random.choice([True, False])
        left_deg = near if on_left else far
        right_deg = far if on_left else near
        if device: await device.send_event(f"{i}.1_CENTER_start", adjusted_time_ns())
        ev_c = start_event("DECISION", "CENTER", "GREEN", cx, cy)
        till = time.time() + random.uniform(1.7, 2.9)
        while time.time() < till:
            process_pygame_events();
            draw_and_flip(cx, cy, COLOR_GREEN, CS_RADIUS)
        end_event(csv_writer, ev_c)
        if device: await device.send_event(f"{i}.1_CENTER_end", adjusted_time_ns())

        near_px = near * SIDE_16 / 16;
        far_px = far * SIDE_16 / 16
        lx = cx - (near_px if on_left else far_px)
        rx = cx + (far_px if on_left else near_px)
        if device:
            await device.send_event(f"{i}.2_LEFT_{left_deg}_start", adjusted_time_ns())
            await device.send_event(f"{i}.2_RIGHT_{right_deg}_start", adjusted_time_ns())
        ev_left = start_event("DECISION", "LEFT", "GREEN", int(lx), cy)
        ev_right = start_event("DECISION", "RIGHT", "GREEN", int(rx), cy)
        till = time.time() + DECISION_TG
        while time.time() < till:
            process_pygame_events();
            screen.fill(BG_COLOR)
            draw_circle(screen, COLOR_GREEN, lx, cy, TARGET_RADIUS)
            draw_circle(screen, COLOR_GREEN, rx, cy, TARGET_RADIUS)
            pygame.display.flip();
            clock.tick(FPS)
        end_event(csv_writer, ev_left, dot_is_correct=on_left)
        end_event(csv_writer, ev_right, dot_is_correct=not on_left)
        if device:
            await device.send_event(f"{i}.2_LEFT_{left_deg}_end", adjusted_time_ns())
            await device.send_event(f"{i}.2_RIGHT_{right_deg}_end", adjusted_time_ns())

        cL = "CORRECT" if on_left else "INCORRECT"
        cR = "INCORRECT" if on_left else "CORRECT"
        if device:
            await device.send_event(f"{i}.3_LEFT_{left_deg}_{cL}_start", adjusted_time_ns())
            await device.send_event(f"{i}.3_RIGHT_{right_deg}_{cR}_start", adjusted_time_ns())
        fb1 = start_event("DECISION", "FEEDBACK", cL, int(lx), cy)
        fb2 = start_event("DECISION", "FEEDBACK", cR, int(rx), cy)
        till = time.time() + DECISION_FB
        while time.time() < till:
            process_pygame_events();
            screen.fill(BG_COLOR)
            draw_circle(screen, COLOR_GREEN if on_left else COLOR_RED, lx, cy, TARGET_RADIUS)
            draw_circle(screen, COLOR_RED if on_left else COLOR_GREEN, rx, cy, TARGET_RADIUS)
            pygame.display.flip();
            clock.tick(FPS)
        end_event(csv_writer, fb1);
        end_event(csv_writer, fb2)
        if device:
            await device.send_event(f"{i}.3_LEFT_{left_deg}_{cL}_end", adjusted_time_ns())
            await device.send_event(f"{i}.3_RIGHT_{right_deg}_{cR}_end", adjusted_time_ns())
        iti_end = time.time() + ITI
        while time.time() < iti_end: process_pygame_events(); draw_and_flip()
    await show_marker(csv_writer, "END_DECISION_BLOCK")


async def run_antisaccade_task(csv_writer, n_trials=20):
    show_instruction(f"[ANTISACCADE TASK]\nНажмите ПРОБЕЛ\n(всего {n_trials} триалов)")
    await show_marker(csv_writer, "START_ANTISACCADE_BLOCK")
    cx, cy = WIN_WIDTH // 2, WIN_HEIGHT // 2
    for i in range(1, n_trials + 1):
        side = random.choice(["LEFT", "RIGHT"])
        red_x = cx + SIDE_16 if side == "RIGHT" else cx - SIDE_16
        green_x = cx - SIDE_16 if side == "RIGHT" else cx + SIDE_16
        if device: await device.send_event(f"{i}.1_CENTER_start", adjusted_time_ns())
        ev_c = start_event("ANTISACCADE", "CENTER", "GREEN", cx, cy)
        till = time.time() + random.uniform(1.7, 2.9)
        while time.time() < till:
            process_pygame_events();
            draw_and_flip(cx, cy, COLOR_GREEN, CS_RADIUS)
        end_event(csv_writer, ev_c)
        if device: await device.send_event(f"{i}.1_CENTER_end", adjusted_time_ns())

        if device: await device.send_event(f"{i}.2_{side}_RED_start", adjusted_time_ns())
        ev_r = start_event("ANTISACCADE", side, "RED", red_x, cy)
        till = time.time() + ANTI_RED
        while time.time() < till:
            process_pygame_events();
            draw_and_flip(red_x, cy, COLOR_RED, TARGET_RADIUS)
        end_event(csv_writer, ev_r, dot_is_correct=False)
        if device: await device.send_event(f"{i}.2_{side}_RED_end", adjusted_time_ns())

        gr_side = "LEFT" if side == "RIGHT" else "RIGHT"
        if device: await device.send_event(f"{i}.3_{gr_side}_GREEN_start", adjusted_time_ns())
        ev_g = start_event("ANTISACCADE", gr_side, "GREEN", green_x, cy)
        till = time.time() + ANTI_GREEN
        while time.time() < till:
            process_pygame_events();
            draw_and_flip(green_x, cy, COLOR_GREEN, TARGET_RADIUS)
        end_event(csv_writer, ev_g, dot_is_correct=True)
        if device: await device.send_event(f"{i}.3_{gr_side}_GREEN_end", adjusted_time_ns())
        iti_end = time.time() + ITI
        while time.time() < iti_end: process_pygame_events(); draw_and_flip()
    await show_marker(csv_writer, "END_ANTISACCADE_BLOCK")


# ═══════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════

async def main():
    global device, recording_running, time_offset_ns, collector

    # CSV-лог стимулов (формат совместим с оригинальным EXP_*.csv)
    csv_path = os.path.join(output_dir, "stimulus_log.csv")
    logfile = open(csv_path, "w", newline="", encoding="utf-8")
    writer = csv.writer(logfile)

    writer.writerow(["# METADATA"])
    writer.writerow(["Subject_ID", subject_id])
    writer.writerow(["Group", group_id])
    writer.writerow(["Age", age])
    writer.writerow(["Gender", gender])
    writer.writerow(["Session", session])
    writer.writerow(["View_Dist", VIEW_DIST_CM])
    writer.writerow([])
    writer.writerow(["task_name", "dot_type", "dot_color", "dot_is_correct",
                      "t_start", "t_end", "x_px", "y_px"])

    markers_path = "/Users/iliachapliev/Desktop/маркеры"

    # ШАГ 1: Показываем маркеры
    wait_for_screenshot_setup(markers_path, scale=3.0)

    # ШАГ 2: Подключение к устройству
    print(f"\n🔌 Прямое подключение к {current_ip}:8080...")

    try:
        device = Device(address=current_ip, port=8080)

        # ─── FIX: отключаем SSL для aiohttp (API работает по HTTP) ───
        # Некоторые версии aiohttp по умолчанию пытаются SSL,
        # что вызывает "ssl:default [No route to host]".
        try:
            import aiohttp
            import ssl as _ssl

            # Вариант 1: подменяем внутреннюю сессию Device
            if hasattr(device, "_session") and device._session is not None:
                await device._session.close()
                connector = aiohttp.TCPConnector(ssl=False)
                device._session = aiohttp.ClientSession(connector=connector)
            elif hasattr(device, "_api"):
                # Вариант 2: у Device есть _api объект с сессией
                api = device._api
                if hasattr(api, "_session") and api._session is not None:
                    await api._session.close()
                    connector = aiohttp.TCPConnector(ssl=False)
                    api._session = aiohttp.ClientSession(connector=connector)

            # Вариант 3: патчим base_url чтобы точно был http://
            for obj in (device, getattr(device, "_api", None)):
                if obj is None:
                    continue
                for attr in ("_base_url", "base_url", "_api_url"):
                    url = getattr(obj, attr, None)
                    if url and isinstance(url, str) and url.startswith("https://"):
                        setattr(obj, attr, url.replace("https://", "http://", 1))
                        print(f"  [FIX] {attr}: https → http")

        except Exception as ssl_fix_err:
            print(f"  [FIX] SSL-патч не применён (не критично): {ssl_fix_err}")
        # ─── конец FIX ───

        async with asyncio.timeout(5.0):
            await device.get_status()
        print(f"✅ Устройство успешно подключено: {current_ip}")
    except (asyncio.TimeoutError, Exception) as e:
        print(f"❌ ОШИБКА: Не удалось подключиться к {current_ip}:8080.")
        print(f"Детали ошибки: {e}")
        print("Проверьте, что телефон и компьютер в одной Wi-Fi сети.")
        print("\n💡 Попробуйте также:")
        print("   1. Откройте в браузере http://{current_ip}:8080/status")
        print("   2. Если открывается — проблема в SSL, обновите aiohttp:")
        print("      pip install aiohttp==3.9.5")
        logfile.close()
        return

    time_offset_ns = await get_time_offset(device) * 1_000_000
    await check_device_status(device)

    # ШАГ 3: Инициализация DataCollector
    collector = DataCollector(device, output_dir)

    # Обёртка send_event для логирования
    orig_send = device.send_event

    async def send_event_and_log(tag: str, ts: int | None = None):
        if ts is None: ts = adjusted_time_ns()
        try:
            await orig_send(tag, ts)
        except:
            pass

        # Логируем в events.csv через collector
        if collector:
            collector.log_event(ts, tag)

        lower = tag.lower()
        is_start, is_end = lower.endswith("_start"), lower.endswith("_end")
        core = tag[:-6] if is_start else (tag[:-4] if is_end else tag)
        task_name = "UNK"
        for blk in ("GAP", "OVERLAP", "PREDICTION", "DECISION", "ANTISACCADE", "CALIB"):
            if blk in core: task_name = blk; break
        dot_color = "RED" if "RED" in core else ("GREEN" if "GREEN" in core else None)
        correct = False if "INCORRECT" in core else (True if "CORRECT" in core else None)
        writer.writerow(
            [task_name, core, dot_color, correct,
             ts if is_start else None, ts if is_end else None, None, None])

    device.send_event = send_event_and_log

    print("[DEBUG] Отправка метаданных в Cloud...")
    ts = adjusted_time_ns()
    await device.send_event(f"Meta.Subject.{subject_id}", ts)
    await device.send_event(f"Meta.Group.{group_id}", ts)

    try:
        print("[DEBUG] Запрос на старт записи...")
        rec_id = await device.recording_start()
        recording_running = True
        print(f"⏺️ ЗАПИСЬ ИДЕТ! ID: {rec_id}")

        if await async_wait_for_recording_begin(device):
            await start_obs_recording()
        else:
            print("⚠️ OBS не запущен (нет подтверждения от очков).")

    except Exception as e:
        print(f"❌ Критическая ошибка старта: {e}")
        logfile.close()
        return

    # ШАГ 4: Запускаем стриминг данных ПОСЛЕ начала записи
    await collector.start()

    # Калибровка
    await device.send_event("CALIB_BLOCK_START", adjusted_time_ns())
    await show_apriltag_sequence(writer, device, markers_path, scale=3.0)
    await device.send_event("CALIB_BLOCK_END", adjusted_time_ns())

    # Блоки экспериментов (рандомный порядок)
    blocks = [run_gap_task, run_overlap_task, run_prediction_task,
              run_decision_task, run_antisaccade_task]
    random.shuffle(blocks)
    for blk in blocks:
        await blk(writer, n_trials=20)

    # ШАГ 5: Останавливаем стриминг ПЕРЕД остановкой записи
    if collector:
        await collector.stop()

    if recording_running:
        print("⏹️ Финализация записи...")
        await device.recording_stop_and_save()
        await device.close()

    await stop_obs_recording()

    logfile.close()

    print("\n" + "=" * 50)
    print(f"✅ Эксперимент завершен успешно!")
    print(f"📁 Все данные сохранены в: {output_dir}")
    print("   - stimulus_log.csv  (лог стимулов)")
    print("   - events.csv        (все маркеры)")
    print("   - gaze.csv          (координаты взгляда)")
    print("   - fixations.csv     (фиксации)")
    print("   - blinks.csv        (моргания)")
    print("   - imu.csv           (акселерометр/гироскоп)")
    print("   - eye_camera.mp4    (видео камер глаз)")
    print("=" * 50)

    pygame.quit()
    sys.exit()


if __name__ == "__main__":
    asyncio.run(main())
