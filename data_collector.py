#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Real-time data collector: стримит данные с Pupil Neon в локальные CSV/MP4
во время записи эксперимента.

Потоки:
  - gaze.csv        — сырые координаты взгляда
  - fixations.csv   — события фиксации
  - blinks.csv      — события моргания
  - imu.csv         — акселерометр/гироскоп
  - eye_camera.mp4  — видео камер глаз
  - events.csv      — все отправленные маркеры

Каждый поток запускается как asyncio.Task и работает независимо.
Если поток не поддерживается устройством — он тихо завершится.
"""

from __future__ import annotations

import asyncio
import csv
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pupil_labs.realtime_api.device import Device


class DataCollector:
    """Собирает потоковые данные с Pupil Neon в локальные файлы."""

    def __init__(self, device: "Device", output_dir: str):
        self.device = device
        self.output_dir = output_dir
        self._tasks: list[asyncio.Task] = []
        self._files: list = []
        self._video_writer = None
        self._events_writer = None
        self._events_file = None

    # ────────────────────── public API ──────────────────────

    async def start(self):
        """Запускает все потоки сбора данных. Вызывать ПОСЛЕ recording_start()."""
        os.makedirs(self.output_dir, exist_ok=True)
        self._init_events_csv()

        self._tasks = [
            asyncio.create_task(self._stream_gaze(), name="gaze"),
            asyncio.create_task(self._stream_eye_events(), name="eye_events"),
            asyncio.create_task(self._stream_imu(), name="imu"),
            asyncio.create_task(self._stream_eye_video(), name="eye_video"),
        ]
        print(f"[DataCollector] Запущено {len(self._tasks)} потоков сбора данных")

    async def stop(self):
        """Останавливает все потоки. Вызывать ПЕРЕД recording_stop_and_save()."""
        for t in self._tasks:
            t.cancel()
        results = await asyncio.gather(*self._tasks, return_exceptions=True)

        # Закрываем файлы
        for f in self._files:
            try:
                f.close()
            except Exception:
                pass

        if self._events_file:
            try:
                self._events_file.close()
            except Exception:
                pass

        # Закрываем видео-writer
        if self._video_writer is not None:
            try:
                self._video_writer.close()
            except Exception:
                pass

        # Статистика
        for t, r in zip(self._tasks, results):
            name = t.get_name()
            if isinstance(r, asyncio.CancelledError):
                print(f"  [DataCollector] {name}: остановлен (ОК)")
            elif isinstance(r, Exception):
                print(f"  [DataCollector] {name}: ошибка — {r}")
            else:
                print(f"  [DataCollector] {name}: завершён")

        self._tasks.clear()
        self._files.clear()
        print("[DataCollector] Все потоки остановлены, файлы закрыты.")

    def log_event(self, timestamp_ns: int, tag: str):
        """Записывает маркер-событие в events.csv (вызывается из experiment.py)."""
        if self._events_writer:
            self._events_writer.writerow([timestamp_ns, tag])

    # ────────────────────── events.csv ──────────────────────

    def _init_events_csv(self):
        path = os.path.join(self.output_dir, "events.csv")
        self._events_file = open(path, "w", newline="", encoding="utf-8")
        self._events_writer = csv.writer(self._events_file)
        self._events_writer.writerow(["timestamp_ns", "tag"])

    # ────────────────────── gaze ──────────────────────

    async def _stream_gaze(self):
        path = os.path.join(self.output_dir, "gaze.csv")
        f = open(path, "w", newline="", encoding="utf-8")
        self._files.append(f)
        w = csv.writer(f)
        w.writerow(["timestamp_s", "x_norm", "y_norm", "worn"])

        count = 0
        try:
            async for datum in self.device.receive_gaze_datums():
                w.writerow([
                    f"{datum.timestamp_unix_seconds:.6f}",
                    f"{datum.x:.6f}",
                    f"{datum.y:.6f}",
                    datum.worn,
                ])
                count += 1
                if count % 500 == 0:
                    f.flush()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"  [DataCollector] gaze stream error: {e}")
        finally:
            f.flush()
            print(f"  [DataCollector] gaze: записано {count} точек")

    # ────────────────────── eye events (fixations + blinks) ──────────────────────

    async def _stream_eye_events(self):
        fix_path = os.path.join(self.output_dir, "fixations.csv")
        blink_path = os.path.join(self.output_dir, "blinks.csv")

        fix_f = open(fix_path, "w", newline="", encoding="utf-8")
        blink_f = open(blink_path, "w", newline="", encoding="utf-8")
        self._files.extend([fix_f, blink_f])

        fix_w = csv.writer(fix_f)
        fix_w.writerow(["start_timestamp_s", "end_timestamp_s", "mean_x_norm", "mean_y_norm", "duration_s"])

        blink_w = csv.writer(blink_f)
        blink_w.writerow(["start_timestamp_s", "end_timestamp_s", "duration_s"])

        fix_count = 0
        blink_count = 0

        try:
            async for ev in self.device.receive_eye_events():
                # Определяем тип по наличию атрибутов
                ev_type = type(ev).__name__

                if "Fixation" in ev_type:
                    start_ts = getattr(ev, "start_timestamp_unix_seconds",
                                       getattr(ev, "timestamp_unix_seconds", 0))
                    end_ts = getattr(ev, "end_timestamp_unix_seconds", start_ts)
                    duration = getattr(ev, "duration", end_ts - start_ts)
                    mean_x = getattr(ev, "mean_x", getattr(ev, "x", 0))
                    mean_y = getattr(ev, "mean_y", getattr(ev, "y", 0))
                    fix_w.writerow([
                        f"{start_ts:.6f}", f"{end_ts:.6f}",
                        f"{mean_x:.6f}", f"{mean_y:.6f}",
                        f"{duration:.4f}",
                    ])
                    fix_count += 1
                    if fix_count % 100 == 0:
                        fix_f.flush()

                elif "Blink" in ev_type:
                    start_ts = getattr(ev, "start_timestamp_unix_seconds",
                                       getattr(ev, "timestamp_unix_seconds", 0))
                    end_ts = getattr(ev, "end_timestamp_unix_seconds", start_ts)
                    duration = getattr(ev, "duration", end_ts - start_ts)
                    blink_w.writerow([
                        f"{start_ts:.6f}", f"{end_ts:.6f}",
                        f"{duration:.4f}",
                    ])
                    blink_count += 1

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"  [DataCollector] eye_events stream error: {e}")
        finally:
            fix_f.flush()
            blink_f.flush()
            print(f"  [DataCollector] fixations: {fix_count}, blinks: {blink_count}")

    # ────────────────────── IMU ──────────────────────

    async def _stream_imu(self):
        path = os.path.join(self.output_dir, "imu.csv")
        f = open(path, "w", newline="", encoding="utf-8")
        self._files.append(f)
        w = csv.writer(f)
        w.writerow(["timestamp_s", "accel_x", "accel_y", "accel_z",
                     "gyro_x", "gyro_y", "gyro_z"])

        count = 0
        try:
            async for datum in self.device.receive_imu_datums():
                # Формат зависит от версии API — пробуем разные атрибуты
                ts = getattr(datum, "timestamp_unix_seconds",
                             getattr(datum, "timestamp", 0))
                accel = getattr(datum, "accel", None)
                gyro = getattr(datum, "gyro", None)

                if accel is not None and gyro is not None:
                    # accel и gyro могут быть объектами с .x/.y/.z или кортежами
                    if hasattr(accel, "x"):
                        ax, ay, az = accel.x, accel.y, accel.z
                    else:
                        ax, ay, az = accel[0], accel[1], accel[2]
                    if hasattr(gyro, "x"):
                        gx, gy, gz = gyro.x, gyro.y, gyro.z
                    else:
                        gx, gy, gz = gyro[0], gyro[1], gyro[2]
                else:
                    # Плоская структура
                    ax = getattr(datum, "accel_x", 0)
                    ay = getattr(datum, "accel_y", 0)
                    az = getattr(datum, "accel_z", 0)
                    gx = getattr(datum, "gyro_x", 0)
                    gy = getattr(datum, "gyro_y", 0)
                    gz = getattr(datum, "gyro_z", 0)

                w.writerow([f"{ts:.6f}", ax, ay, az, gx, gy, gz])
                count += 1
                if count % 500 == 0:
                    f.flush()
        except asyncio.CancelledError:
            pass
        except AttributeError:
            print("  [DataCollector] IMU: метод receive_imu_datums() не поддерживается — пропускаем")
        except Exception as e:
            print(f"  [DataCollector] imu stream error: {e}")
        finally:
            f.flush()
            print(f"  [DataCollector] imu: записано {count} точек")

    # ────────────────────── eye video ──────────────────────

    async def _stream_eye_video(self):
        """Записывает видеопоток камер глаз в eye_camera.mp4."""
        try:
            import imageio.v3 as iio
        except ImportError:
            try:
                import imageio as iio
            except ImportError:
                print("  [DataCollector] eye_video: imageio не установлен — видео пропущено")
                return

        path = os.path.join(self.output_dir, "eye_camera.mp4")
        writer = None
        count = 0

        try:
            async for frame_data in self.device.receive_eyes_video_frame():
                frame = getattr(frame_data, "bgr_pixels",
                                getattr(frame_data, "frame", None))
                if frame is None:
                    continue

                # Инициализируем writer при получении первого кадра
                if writer is None:
                    try:
                        h, w_px = frame.shape[:2]
                        writer = iio.imopen(path, "w", plugin="pyav")
                        writer.init_video_stream("libx264", fps=30)
                        self._video_writer = writer
                    except Exception as e:
                        # Fallback: используем imageio legacy API
                        try:
                            import imageio
                            writer = imageio.get_writer(path, fps=30, codec="libx264",
                                                         quality=7)
                            self._video_writer = writer
                        except Exception as e2:
                            print(f"  [DataCollector] eye_video: не удалось создать writer — {e2}")
                            return

                try:
                    # Конвертируем BGR → RGB если нужно
                    import numpy as np
                    if frame.ndim == 3 and frame.shape[2] == 3:
                        rgb = frame[:, :, ::-1]  # BGR→RGB
                    else:
                        rgb = frame
                    writer.write(rgb)
                    count += 1
                except Exception:
                    pass

        except asyncio.CancelledError:
            pass
        except AttributeError:
            print("  [DataCollector] eye_video: receive_eyes_video_frame() не поддерживается")
        except Exception as e:
            print(f"  [DataCollector] eye_video stream error: {e}")
        finally:
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    pass
            print(f"  [DataCollector] eye_video: записано {count} кадров → {path}")
