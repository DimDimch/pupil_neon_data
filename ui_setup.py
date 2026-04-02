#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Tkinter-форма настройки эксперимента.
Открывается ДО pygame, возвращает dict с параметрами или None (если закрыли окно).
"""

from __future__ import annotations

import json
import os
import tkinter as tk
from tkinter import ttk
from typing import Optional

_LAST_SESSION_FILE = "last_session.json"
_IP_CONFIG_FILE = "pupil_ip_config.json"

# Значения по умолчанию
_DEFAULTS = {
    "subject_id": "",
    "group_id": "Пациент",
    "age": "",
    "gender": "М",
    "session": "1",
    "view_dist_cm": "62.5",
    "device_ip": "192.168.1.101",
    "monitor_width_cm": "52.7",
}


def _load_saved() -> dict:
    """Загружаем сохранённые значения из last_session.json и pupil_ip_config.json."""
    saved = dict(_DEFAULTS)
    if os.path.exists(_LAST_SESSION_FILE):
        try:
            with open(_LAST_SESSION_FILE, "r", encoding="utf-8") as f:
                saved.update(json.load(f))
        except Exception:
            pass
    if os.path.exists(_IP_CONFIG_FILE):
        try:
            with open(_IP_CONFIG_FILE, "r", encoding="utf-8") as f:
                ip = json.load(f).get("ip")
                if ip:
                    saved["device_ip"] = ip
        except Exception:
            pass
    return saved


def _save_session(data: dict):
    """Сохраняем текущие значения для следующего запуска."""
    try:
        with open(_LAST_SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    try:
        with open(_IP_CONFIG_FILE, "w") as f:
            json.dump({"ip": data.get("device_ip", "")}, f)
    except Exception:
        pass


def show_setup_form() -> Optional[dict]:
    """
    Показывает форму настройки. Возвращает dict с параметрами или None.
    После возврата tkinter полностью уничтожен — можно запускать pygame.
    """
    saved = _load_saved()
    result: list = []  # list-обёртка чтобы замыкание могло менять значение

    root = tk.Tk()
    root.title("Настройка эксперимента — Pupil Neon")
    root.resizable(False, False)

    # Центрируем окно
    w, h = 520, 500
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    x = (sw - w) // 2
    y = (sh - h) // 2
    root.geometry(f"{w}x{h}+{x}+{y}")

    # Стили
    style = ttk.Style()
    style.configure("Title.TLabel", font=("Helvetica", 16, "bold"))
    style.configure("Field.TLabel", font=("Helvetica", 12))
    style.configure("Start.TButton", font=("Helvetica", 13, "bold"))

    # Заголовок
    ttk.Label(root, text="Настройка эксперимента", style="Title.TLabel").pack(
        pady=(18, 12)
    )

    # Основной фрейм
    frame = ttk.Frame(root, padding=10)
    frame.pack(fill="both", expand=True)

    entries: dict[str, tk.Widget] = {}

    def add_field(parent, row, label_text, key, widget_type="entry", options=None):
        ttk.Label(parent, text=label_text, style="Field.TLabel").grid(
            row=row, column=0, sticky="w", padx=(8, 4), pady=6
        )
        if widget_type == "entry":
            var = tk.StringVar(value=saved.get(key, ""))
            ent = ttk.Entry(parent, textvariable=var, width=28)
            ent.grid(row=row, column=1, padx=(4, 8), pady=6, sticky="ew")
            entries[key] = var
        elif widget_type == "combo":
            var = tk.StringVar(value=saved.get(key, ""))
            cb = ttk.Combobox(
                parent, textvariable=var, values=options, width=26, state="readonly"
            )
            cb.grid(row=row, column=1, padx=(4, 8), pady=6, sticky="ew")
            entries[key] = var

    add_field(frame, 0, "ID испытуемого:", "subject_id")
    add_field(
        frame,
        1,
        "Группа:",
        "group_id",
        "combo",
        ["Пациент", "Контроль", "Другое"],
    )
    add_field(frame, 2, "Возраст:", "age")
    add_field(frame, 3, "Пол:", "gender", "combo", ["М", "Ж"])
    add_field(frame, 4, "Сессия №:", "session")
    add_field(frame, 5, "Расстояние до экрана (см):", "view_dist_cm")
    add_field(frame, 6, "Ширина монитора (см):", "monitor_width_cm")
    add_field(frame, 7, "IP устройства:", "device_ip")

    frame.columnconfigure(1, weight=1)

    # Статус-строка
    status_var = tk.StringVar(value="")
    status_label = ttk.Label(root, textvariable=status_var, foreground="red")
    status_label.pack(pady=(0, 4))

    def on_start():
        # Валидация
        sid = entries["subject_id"].get().strip()
        if not sid:
            status_var.set("Ошибка: введите ID испытуемого")
            return

        age_str = entries["age"].get().strip()
        if age_str and not age_str.isdigit():
            status_var.set("Ошибка: возраст должен быть числом")
            return

        try:
            vd = float(entries["view_dist_cm"].get())
        except ValueError:
            status_var.set("Ошибка: расстояние до экрана — число")
            return

        try:
            mw = float(entries["monitor_width_cm"].get())
        except ValueError:
            status_var.set("Ошибка: ширина монитора — число")
            return

        data = {
            "subject_id": sid,
            "group_id": entries["group_id"].get(),
            "age": age_str,
            "gender": entries["gender"].get(),
            "session": entries["session"].get().strip() or "1",
            "view_dist_cm": vd,
            "monitor_width_cm": mw,
            "device_ip": entries["device_ip"].get().strip(),
        }
        _save_session({k: str(v) for k, v in data.items()})
        result.append(data)
        root.destroy()

    def on_close():
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    btn_frame = ttk.Frame(root)
    btn_frame.pack(pady=(4, 16))
    ttk.Button(btn_frame, text="Начать эксперимент", style="Start.TButton", command=on_start).pack()

    # Фокус на первое поле
    root.after(100, lambda: None)  # даём окну отрисоваться

    root.mainloop()

    if result:
        return result[0]
    return None


if __name__ == "__main__":
    cfg = show_setup_form()
    if cfg:
        print("Конфигурация:", json.dumps(cfg, ensure_ascii=False, indent=2))
    else:
        print("Отменено пользователем.")
