"""
JPEG Compressor & Artifact Studio 2.0
Профессиональное приложение для сжатия фото в стиле JPEG:
- Тонкая настройка квантования, поколенческих потерь и артефактов
- Режим подбора качества под целевой размер (Target Size в КБ)
- Анализ метрик качества: PSNR (dB), RMSE и оценка искажений
- Цветовая тепловая карта потерь (Difference Heatmap)
- Интерактивная 4x лупа для изучения 8x8 блоков DCT
- Вертикальный и горизонтальный сплит-слайдеры (До / После)
- Зажатие Пробела для моментального сравнения с оригиналом
- Стилизация: хроматическая аберрация, Deep-Fry насыщенность и контраст
- Многопоточная пакетная обработка на всех ядрах CPU
- Экспорт в JPEG, PNG (сохранение артефактов без повторного сжатия) и WebP
"""

import os
import sys
import time
import math
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, List, Tuple, Dict, Any

import tkinter as tk
from tkinter import filedialog, messagebox
import customtkinter as ctk
from PIL import Image, ImageTk

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False

from compressor import (
    compress_jpeg,
    prepare_image_for_jpeg,
    format_bytes,
    calculate_savings,
    compute_quality_metrics,
    generate_difference_heatmap,
    find_quality_for_target_size,
    render_magnifier_loupe,
    read_exif_info
)

# Windows crisp DPI scaling
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


class BaseAppClass(ctk.CTk, (TkinterDnD.DnDWrapper if HAS_DND else object)):
    def __init__(self):
        super().__init__()
        if HAS_DND:
            self.TkdndVersion = TkinterDnD._require(self)


class JpegCompressorApp(BaseAppClass):
    def __init__(self):
        super().__init__()

        self.title("JPEG Compressor & Artifact Studio 2.0")
        self.geometry("1340x860")
        self.minsize(1020, 680)

        # Image state
        self.current_file_path: Optional[str] = None
        self.all_loaded_files: List[str] = []
        self.original_image: Optional[Image.Image] = None
        self.original_bytes_count: int = 0
        self.compressed_image: Optional[Image.Image] = None
        self.compressed_bytes: bytes = b""
        self.heatmap_image: Optional[Image.Image] = None
        self.metrics_data: Optional[Dict[str, Any]] = None

        # View settings
        self.view_mode = "split_v"  # "split_v", "split_h", "heatmap", "compressed", "original", "side_by_side"
        self.split_ratio_x = 0.5
        self.split_ratio_y = 0.5
        self.zoom_factor = 1.0
        self.pan_offset_x = 0
        self.pan_offset_y = 0
        self.is_auto_fit = True

        # Mouse / Interaction states
        self.is_dragging_split = False
        self.is_panning = False
        self.pan_start_x = 0
        self.pan_start_y = 0
        self.mouse_canvas_x = 0
        self.mouse_canvas_y = 0
        self.loupe_enabled = False
        self.space_key_held = False

        # Target size mode
        self.compression_mode = "quality"  # "quality" or "target_size"

        # Worker thread
        self.work_queue = queue.Queue()
        self.result_queue = queue.Queue()
        self.request_counter = 0
        self.worker_thread = threading.Thread(target=self._compression_worker, daemon=True)
        self.worker_thread.start()

        # Build UI
        self._build_ui()
        self._bind_global_shortcuts()

        # Check worker results periodically
        self.after(30, self._check_worker_results)

        # Drag and drop
        if HAS_DND:
            try:
                self.drop_target_register(DND_FILES)
                self.dnd_bind("<<Drop>>", self._on_file_drop)
            except Exception as e:
                print("DND warning:", e)

        # Load demo file if present
        demo_path = "D:/panelnie.jpg"
        if os.path.exists(demo_path):
            self.after(200, lambda: self.load_image(demo_path))

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # ----------------------------------------------------
        # Left Sidebar (Tabbed navigation)
        # ----------------------------------------------------
        self.sidebar_frame = ctk.CTkFrame(self, width=390, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        self.sidebar_frame.grid_rowconfigure(1, weight=1)
        self.sidebar_frame.grid_columnconfigure(0, weight=1)

        # Top App Title
        header_frame = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        header_frame.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 4))

        title_lbl = ctk.CTkLabel(
            header_frame,
            text="⚡ JPEG Compressor 2.0",
            font=ctk.CTkFont(size=20, weight="bold")
        )
        title_lbl.pack(anchor="w")

        sub_lbl = ctk.CTkLabel(
            header_frame,
            text="Студия сжатия, артефактов и анализа качества",
            font=ctk.CTkFont(size=12),
            text_color="gray70"
        )
        sub_lbl.pack(anchor="w")

        # Tabs: [Сжатие] [Артефакты] [Метрики & EXIF]
        self.tabview = ctk.CTkTabview(self.sidebar_frame, corner_radius=8)
        self.tabview.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 6))

        self.tab_compress = self.tabview.add("⚡ Сжатие")
        self.tab_artifacts = self.tabview.add("🎨 Артефакты")
        self.tab_metrics = self.tabview.add("📊 Метрики")

        self._build_tab_compress()
        self._build_tab_artifacts()
        self._build_tab_metrics()

        # Bottom Action Bar (Saving & Batch)
        action_bar = ctk.CTkFrame(self.sidebar_frame, fg_color=("gray90", "gray17"), corner_radius=8)
        action_bar.grid(row=2, column=0, sticky="ew", padx=12, pady=(4, 12))

        # Format selector row
        fmt_row = ctk.CTkFrame(action_bar, fg_color="transparent")
        fmt_row.pack(fill="x", padx=10, pady=(8, 4))
        ctk.CTkLabel(fmt_row, text="Формат сохранения:", font=ctk.CTkFont(size=11)).pack(side="left")
        self.export_fmt_var = ctk.StringVar(value="JPEG (.jpg)")
        self.export_fmt_menu = ctk.CTkOptionMenu(
            fmt_row,
            variable=self.export_fmt_var,
            values=["JPEG (.jpg)", "PNG (.png - без потерь)", "WEBP (.webp)"],
            width=150,
            height=26,
            font=ctk.CTkFont(size=11),
            command=lambda _: self._trigger_compression()
        )
        self.export_fmt_menu.pack(side="right")

        self.save_btn = ctk.CTkButton(
            action_bar,
            text="💾 Сохранить изображение...",
            font=ctk.CTkFont(size=13, weight="bold"),
            height=36,
            fg_color="#2E7D32",
            hover_color="#1B5E20",
            command=self._save_single_file
        )
        self.save_btn.pack(fill="x", padx=10, pady=3)

        btn_grid = ctk.CTkFrame(action_bar, fg_color="transparent")
        btn_grid.pack(fill="x", padx=10, pady=(2, 8))
        btn_grid.columnconfigure(0, weight=1)
        btn_grid.columnconfigure(1, weight=1)

        self.quick_save_btn = ctk.CTkButton(
            btn_grid,
            text="⚡ Быстрое",
            font=ctk.CTkFont(size=11),
            height=30,
            fg_color="gray30",
            hover_color="gray25",
            command=self._quick_save
        )
        self.quick_save_btn.grid(row=0, column=0, padx=(0, 3), sticky="ew")

        self.batch_btn = ctk.CTkButton(
            btn_grid,
            text="📦 Пакетное (Все)",
            font=ctk.CTkFont(size=11),
            height=30,
            fg_color="#0277BD",
            hover_color="#01579B",
            command=self._batch_compress_all
        )
        self.batch_btn.grid(row=0, column=1, padx=(3, 0), sticky="ew")

        # ----------------------------------------------------
        # Right Main Area (Preview & Toolbar)
        # ----------------------------------------------------
        self.main_area = ctk.CTkFrame(self, fg_color=("gray95", "gray12"), corner_radius=0)
        self.main_area.grid(row=0, column=1, sticky="nsew", padx=0, pady=0)
        self.main_area.grid_rowconfigure(1, weight=1)
        self.main_area.grid_columnconfigure(0, weight=1)

        # Toolbar
        self.toolbar = ctk.CTkFrame(self.main_area, height=48, fg_color=("gray90", "gray15"), corner_radius=0)
        self.toolbar.grid(row=0, column=0, sticky="ew", padx=0, pady=0)

        # View mode selector
        self.view_mode_seg = ctk.CTkSegmentedButton(
            self.toolbar,
            values=["↔ Верт. сплит", "↕ Гориз. сплит", "♨ Тепловая карта", "Сжатое", "Оригинал", "Рядом"],
            command=self._on_view_mode_change
        )
        self.view_mode_seg.set("↔ Верт. сплит")
        self.view_mode_seg.pack(side="left", padx=10, pady=6)

        # Magnifier & Zoom tools
        tools_frame = ctk.CTkFrame(self.toolbar, fg_color="transparent")
        tools_frame.pack(side="right", padx=10, pady=6)

        self.loupe_btn = ctk.CTkButton(
            tools_frame,
            text="🔍 Лупа 4x",
            width=76,
            height=28,
            font=ctk.CTkFont(size=11),
            fg_color="gray30",
            hover_color="gray25",
            command=self._toggle_loupe
        )
        self.loupe_btn.pack(side="left", padx=3)

        fit_btn = ctk.CTkButton(
            tools_frame,
            text="Fit",
            width=40,
            height=28,
            font=ctk.CTkFont(size=11),
            fg_color="gray30",
            hover_color="gray25",
            command=self._reset_to_fit
        )
        fit_btn.pack(side="left", padx=2)

        actual_btn = ctk.CTkButton(
            tools_frame,
            text="100%",
            width=46,
            height=28,
            font=ctk.CTkFont(size=11),
            fg_color="gray30",
            hover_color="gray25",
            command=self._zoom_100
        )
        actual_btn.pack(side="left", padx=2)

        zoom_out_btn = ctk.CTkButton(
            tools_frame,
            text="−",
            width=28,
            height=28,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="gray30",
            hover_color="gray25",
            command=lambda: self._adjust_zoom(0.8)
        )
        zoom_out_btn.pack(side="left", padx=2)

        self.zoom_label = ctk.CTkLabel(tools_frame, text="100%", font=ctk.CTkFont(size=11), width=42)
        self.zoom_label.pack(side="left", padx=2)

        zoom_in_btn = ctk.CTkButton(
            tools_frame,
            text="+",
            width=28,
            height=28,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="gray30",
            hover_color="gray25",
            command=lambda: self._adjust_zoom(1.25)
        )
        zoom_in_btn.pack(side="left", padx=2)

        # Center Canvas
        self.canvas_frame = ctk.CTkFrame(self.main_area, fg_color="#181818", corner_radius=0)
        self.canvas_frame.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        self.canvas_frame.grid_rowconfigure(0, weight=1)
        self.canvas_frame.grid_columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(
            self.canvas_frame,
            bg="#181818",
            highlightthickness=0,
            cursor="crosshair"
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")

        # Canvas event bindings
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<ButtonPress-1>", self._on_canvas_press_left)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag_left)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release_left)
        self.canvas.bind("<Motion>", self._on_canvas_motion)

        self.canvas.bind("<ButtonPress-3>", self._on_canvas_press_right)
        self.canvas.bind("<B3-Motion>", self._on_canvas_drag_right)
        self.canvas.bind("<ButtonRelease-3>", self._on_canvas_release_right)

        self.canvas.bind("<ButtonPress-2>", self._on_canvas_press_right)
        self.canvas.bind("<B2-Motion>", self._on_canvas_drag_right)
        self.canvas.bind("<ButtonRelease-2>", self._on_canvas_release_right)

        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)
        self.canvas.bind("<Double-Button-1>", lambda _: self._reset_to_fit())

        # Status Bar
        self.status_bar = ctk.CTkFrame(self.main_area, height=28, fg_color=("gray90", "gray15"), corner_radius=0)
        self.status_bar.grid(row=2, column=0, sticky="ew", padx=0, pady=0)

        self.status_label = ctk.CTkLabel(
            self.status_bar,
            text="Готово к работе. Откройте изображение или перетащите его в окно.",
            font=ctk.CTkFont(size=11),
            text_color="gray70"
        )
        self.status_label.pack(side="left", padx=12, pady=2)

        self.hint_label = ctk.CTkLabel(
            self.status_bar,
            text="[Пробел]: оригинал | ЛКМ: сплит | Колесико: зум | ПКМ: панорама",
            font=ctk.CTkFont(size=11),
            text_color="gray50"
        )
        self.hint_label.pack(side="right", padx=12, pady=2)

        self.tk_photo_cache = None

    # ----------------------------------------------------
    # TAB 1: Сжатие (Compression & Target Size)
    # ----------------------------------------------------
    def _build_tab_compress(self):
        container = ctk.CTkScrollableFrame(self.tab_compress, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=2, pady=2)

        # File load buttons
        file_box = ctk.CTkFrame(container, fg_color=("gray85", "gray18"))
        file_box.pack(fill="x", pady=4)

        ctk.CTkLabel(file_box, text="📁 Исходные файлы", font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=10, pady=(8, 4))
        btn_row = ctk.CTkFrame(file_box, fg_color="transparent")
        btn_row.pack(fill="x", padx=10, pady=2)
        btn_row.columnconfigure((0, 1), weight=1)

        ctk.CTkButton(
            btn_row, text="Выбрать фото", height=30,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._choose_single_file
        ).grid(row=0, column=0, padx=(0, 3), sticky="ew")

        ctk.CTkButton(
            btn_row, text="Выбрать несколько", height=30,
            fg_color="gray30", hover_color="gray25", font=ctk.CTkFont(size=11),
            command=self._choose_multiple_files
        ).grid(row=0, column=1, padx=(3, 0), sticky="ew")

        self.file_list_combobox = ctk.CTkComboBox(
            file_box,
            values=["(Нет открытых файлов)"],
            command=self._on_combobox_select,
            height=28
        )
        self.file_list_combobox.pack(fill="x", padx=10, pady=(6, 8))

        # Mode switch: Quality vs Target Size
        mode_box = ctk.CTkFrame(container, fg_color=("gray85", "gray18"))
        mode_box.pack(fill="x", pady=6)

        ctk.CTkLabel(mode_box, text="⚙️ Режим сжатия", font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=10, pady=(8, 4))
        self.mode_selector = ctk.CTkSegmentedButton(
            mode_box,
            values=["Ручное качество (%)", "🎯 Целевой размер (КБ)"],
            command=self._on_compression_mode_change
        )
        self.mode_selector.set("Ручное качество (%)")
        self.mode_selector.pack(fill="x", padx=10, pady=(2, 8))

        # Frame for Quality Slider
        self.quality_controls_frame = ctk.CTkFrame(container, fg_color=("gray85", "gray18"))
        self.quality_controls_frame.pack(fill="x", pady=4)

        q_head = ctk.CTkFrame(self.quality_controls_frame, fg_color="transparent")
        q_head.pack(fill="x", padx=10, pady=(8, 2))
        ctk.CTkLabel(q_head, text="Качество JPEG (Quality):", font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
        self.quality_val_label = ctk.CTkLabel(q_head, text="30%", font=ctk.CTkFont(size=13, weight="bold"), text_color="#3B8ED0")
        self.quality_val_label.pack(side="right")

        self.quality_slider = ctk.CTkSlider(
            self.quality_controls_frame,
            from_=1, to=100, number_of_steps=99,
            command=self._on_quality_change
        )
        self.quality_slider.set(30)
        self.quality_slider.pack(fill="x", padx=10, pady=(2, 6))

        # Presets Grid (8 Presets!)
        ctk.CTkLabel(self.quality_controls_frame, text="Быстрые пресеты:", font=ctk.CTkFont(size=11), text_color="gray70").pack(anchor="w", padx=10, pady=(2, 2))
        p_grid = ctk.CTkFrame(self.quality_controls_frame, fg_color="transparent")
        p_grid.pack(fill="x", padx=10, pady=(2, 8))
        p_grid.columnconfigure((0, 1, 2, 3), weight=1)

        ctk.CTkButton(p_grid, text="85% Кач.", height=24, font=ctk.CTkFont(size=10), fg_color="gray30", hover_color="#2E7D32",
                      command=lambda: self._apply_preset(85, 1, 1.0)).grid(row=0, column=0, padx=1, pady=1, sticky="ew")
        ctk.CTkButton(p_grid, text="60% Веб", height=24, font=ctk.CTkFont(size=10), fg_color="gray30", hover_color="#1565C0",
                      command=lambda: self._apply_preset(60, 1, 1.0)).grid(row=0, column=1, padx=1, pady=1, sticky="ew")
        ctk.CTkButton(p_grid, text="25% Низк.", height=24, font=ctk.CTkFont(size=10), fg_color="gray30", hover_color="#E65100",
                      command=lambda: self._apply_preset(25, 1, 1.0)).grid(row=0, column=2, padx=1, pady=1, sticky="ew")
        ctk.CTkButton(p_grid, text="5% Экстрим", height=24, font=ctk.CTkFont(size=10), fg_color="gray30", hover_color="#C62828",
                      command=lambda: self._apply_preset(5, 1, 1.0)).grid(row=0, column=3, padx=1, pady=1, sticky="ew")

        ctk.CTkButton(p_grid, text="👾 Ретро 00-х", height=24, font=ctk.CTkFont(size=10), fg_color="gray30", hover_color="#6A1B9A",
                      command=lambda: self._apply_preset(15, 2, 0.4, pixelate=True)).grid(row=1, column=0, padx=1, pady=1, sticky="ew")
        ctk.CTkButton(p_grid, text="🐺 Шакал", height=24, font=ctk.CTkFont(size=10, weight="bold"), fg_color="#880E4F", hover_color="#AD1457",
                      command=lambda: self._apply_preset(2, 5, 1.0, shift_grid=True)).grid(row=1, column=1, padx=1, pady=1, sticky="ew")
        ctk.CTkButton(p_grid, text="🍟 Deep-Fry", height=24, font=ctk.CTkFont(size=10), fg_color="#B71C1C", hover_color="#D32F2F",
                      command=lambda: self._apply_preset(3, 3, 1.0, sat=2.2, cont=1.8)).grid(row=1, column=2, padx=1, pady=1, sticky="ew")
        ctk.CTkButton(p_grid, text="📼 VHS Glitch", height=24, font=ctk.CTkFont(size=10), fg_color="#00695C", hover_color="#00897B",
                      command=lambda: self._apply_preset(10, 2, 1.0, chrom=5)).grid(row=1, column=3, padx=1, pady=1, sticky="ew")

        # Frame for Target Size controls (hidden initially)
        self.target_size_frame = ctk.CTkFrame(container, fg_color=("gray85", "gray18"))

        ts_head = ctk.CTkFrame(self.target_size_frame, fg_color="transparent")
        ts_head.pack(fill="x", padx=10, pady=(8, 2))
        ctk.CTkLabel(ts_head, text="Желаемый размер файла:", font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")

        ts_input_row = ctk.CTkFrame(self.target_size_frame, fg_color="transparent")
        ts_input_row.pack(fill="x", padx=10, pady=4)
        self.target_size_entry = ctk.CTkEntry(ts_input_row, placeholder_text="200", width=100)
        self.target_size_entry.insert(0, "200")
        self.target_size_entry.pack(side="left", padx=(0, 6))
        ctk.CTkLabel(ts_input_row, text="КБ").pack(side="left")

        ctk.CTkButton(
            ts_input_row, text="🎯 Рассчитать", width=100, height=28,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._apply_target_size
        ).pack(side="right")

        # Quick size presets
        ts_presets = ctk.CTkFrame(self.target_size_frame, fg_color="transparent")
        ts_presets.pack(fill="x", padx=10, pady=(4, 10))
        ts_presets.columnconfigure((0, 1, 2, 3), weight=1)
        ctk.CTkButton(ts_presets, text="50 КБ", height=22, font=ctk.CTkFont(size=10), fg_color="gray30",
                      command=lambda: self._set_target_kb(50)).grid(row=0, column=0, padx=1, sticky="ew")
        ctk.CTkButton(ts_presets, text="200 КБ", height=22, font=ctk.CTkFont(size=10), fg_color="gray30",
                      command=lambda: self._set_target_kb(200)).grid(row=0, column=1, padx=1, sticky="ew")
        ctk.CTkButton(ts_presets, text="256 КБ (Discord)", height=22, font=ctk.CTkFont(size=10), fg_color="gray30",
                      command=lambda: self._set_target_kb(256)).grid(row=0, column=2, padx=1, sticky="ew")
        ctk.CTkButton(ts_presets, text="1 МБ", height=22, font=ctk.CTkFont(size=10), fg_color="gray30",
                      command=lambda: self._set_target_kb(1024)).grid(row=0, column=3, padx=1, sticky="ew")

        # Options
        opt_box = ctk.CTkFrame(container, fg_color=("gray85", "gray18"))
        opt_box.pack(fill="x", pady=6)
        ctk.CTkLabel(opt_box, text="Дополнительные опции:", font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=10, pady=(8, 4))

        self.strip_exif_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            opt_box, text="Удалить EXIF-данные (приватность и размер)",
            variable=self.strip_exif_var, font=ctk.CTkFont(size=11),
            command=self._trigger_compression
        ).pack(anchor="w", padx=10, pady=3)

        self.optimize_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            opt_box, text="Оптимизация таблиц Хаффмана",
            variable=self.optimize_var, font=ctk.CTkFont(size=11),
            command=self._trigger_compression
        ).pack(anchor="w", padx=10, pady=(3, 8))

    # ----------------------------------------------------
    # TAB 2: Артефакты & Глитч (Deep-Fry, Chroma, VHS)
    # ----------------------------------------------------
    def _build_tab_artifacts(self):
        container = ctk.CTkScrollableFrame(self.tab_artifacts, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=2, pady=2)

        # Multi-pass & Grid shift
        pass_box = ctk.CTkFrame(container, fg_color=("gray85", "gray18"))
        pass_box.pack(fill="x", pady=4)
        ctk.CTkLabel(pass_box, text="🔁 Поколенческие потери (Generation Loss)", font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=10, pady=(8, 2))

        pass_row = ctk.CTkFrame(pass_box, fg_color="transparent")
        pass_row.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(pass_row, text="Проходы пережатия:", font=ctk.CTkFont(size=11)).pack(side="left")
        self.passes_val_label = ctk.CTkLabel(pass_row, text="1x", font=ctk.CTkFont(size=12, weight="bold"))
        self.passes_val_label.pack(side="right")

        self.passes_slider = ctk.CTkSlider(pass_box, from_=1, to=10, number_of_steps=9, command=self._on_passes_change)
        self.passes_slider.set(1)
        self.passes_slider.pack(fill="x", padx=10, pady=2)

        self.shift_grid_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            pass_box, text="Сдвиг сетки DCT (эффект повторных сохранений)",
            variable=self.shift_grid_var, font=ctk.CTkFont(size=11),
            command=self._trigger_compression
        ).pack(anchor="w", padx=10, pady=(4, 8))

        # Chroma Subsampling
        chroma_box = ctk.CTkFrame(container, fg_color=("gray85", "gray18"))
        chroma_box.pack(fill="x", pady=4)
        ctk.CTkLabel(chroma_box, text="🎨 Субдискретизация цвета", font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=10, pady=(8, 2))

        self.subsampling_var = ctk.StringVar(value="4:2:0 (Классика JPEG / размытие цвета)")
        self.subsampling_menu = ctk.CTkOptionMenu(
            chroma_box,
            variable=self.subsampling_var,
            values=[
                "4:2:0 (Классика JPEG / размытие цвета)",
                "4:2:2 (Средняя)",
                "4:4:4 (Без искажения цвета)"
            ],
            command=lambda _: self._trigger_compression()
        )
        self.subsampling_menu.pack(fill="x", padx=10, pady=(2, 6))

        # Chromatic Aberration (Channel offset)
        ab_row = ctk.CTkFrame(chroma_box, fg_color="transparent")
        ab_row.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(ab_row, text="Хроматическая аберрация (сдвиг R/B):", font=ctk.CTkFont(size=11)).pack(side="left")
        self.chrom_val_label = ctk.CTkLabel(ab_row, text="0 px", font=ctk.CTkFont(size=11, weight="bold"))
        self.chrom_val_label.pack(side="right")

        self.chrom_slider = ctk.CTkSlider(chroma_box, from_=0, to=10, number_of_steps=10, command=self._on_chrom_change)
        self.chrom_slider.set(0)
        self.chrom_slider.pack(fill="x", padx=10, pady=(2, 8))

        # Deep-Fry (Saturation & Contrast)
        fry_box = ctk.CTkFrame(container, fg_color=("gray85", "gray18"))
        fry_box.pack(fill="x", pady=4)
        ctk.CTkLabel(fry_box, text="🍟 Эффект Deep-Fried (Усиление)", font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=10, pady=(8, 2))

        sat_row = ctk.CTkFrame(fry_box, fg_color="transparent")
        sat_row.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(sat_row, text="Насыщенность цвета:", font=ctk.CTkFont(size=11)).pack(side="left")
        self.sat_val_label = ctk.CTkLabel(sat_row, text="1.0x", font=ctk.CTkFont(size=11, weight="bold"))
        self.sat_val_label.pack(side="right")

        self.sat_slider = ctk.CTkSlider(fry_box, from_=1.0, to=3.0, number_of_steps=20, command=self._on_sat_change)
        self.sat_slider.set(1.0)
        self.sat_slider.pack(fill="x", padx=10, pady=2)

        cont_row = ctk.CTkFrame(fry_box, fg_color="transparent")
        cont_row.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(cont_row, text="Контрастность артефактов:", font=ctk.CTkFont(size=11)).pack(side="left")
        self.cont_val_label = ctk.CTkLabel(cont_row, text="1.0x", font=ctk.CTkFont(size=11, weight="bold"))
        self.cont_val_label.pack(side="right")

        self.cont_slider = ctk.CTkSlider(fry_box, from_=1.0, to=2.5, number_of_steps=15, command=self._on_cont_change)
        self.cont_slider.set(1.0)
        self.cont_slider.pack(fill="x", padx=10, pady=(2, 8))

        # Downscale & Pixelation
        scale_box = ctk.CTkFrame(container, fg_color=("gray85", "gray18"))
        scale_box.pack(fill="x", pady=4)
        ctk.CTkLabel(scale_box, text="📉 Масштаб разрешения и пиксели", font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=10, pady=(8, 2))

        sc_row = ctk.CTkFrame(scale_box, fg_color="transparent")
        sc_row.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(sc_row, text="Разрешение:", font=ctk.CTkFont(size=11)).pack(side="left")
        self.scale_val_label = ctk.CTkLabel(sc_row, text="100%", font=ctk.CTkFont(size=11, weight="bold"))
        self.scale_val_label.pack(side="right")

        self.scale_slider = ctk.CTkSlider(scale_box, from_=10, to=100, number_of_steps=90, command=self._on_scale_change)
        self.scale_slider.set(100)
        self.scale_slider.pack(fill="x", padx=10, pady=2)

        self.pixelate_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            scale_box, text="Пикселизация (растянуть в исходный размер)",
            variable=self.pixelate_var, font=ctk.CTkFont(size=11),
            command=self._trigger_compression
        ).pack(anchor="w", padx=10, pady=(4, 8))

    # ----------------------------------------------------
    # TAB 3: Метрики качества & EXIF
    # ----------------------------------------------------
    def _build_tab_metrics(self):
        container = ctk.CTkScrollableFrame(self.tab_metrics, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=2, pady=2)

        # Quality metrics card
        metric_card = ctk.CTkFrame(container, fg_color=("gray85", "gray18"))
        metric_card.pack(fill="x", pady=4)

        ctk.CTkLabel(metric_card, text="🔬 Научные метрики искажений", font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=10, pady=(8, 4))

        self.metric_psnr_label = ctk.CTkLabel(metric_card, text="PSNR: —", font=ctk.CTkFont(size=16, weight="bold"), text_color="#3B8ED0")
        self.metric_psnr_label.pack(anchor="w", padx=10, pady=1)

        self.metric_verdict_label = ctk.CTkLabel(metric_card, text="Оценка: —", font=ctk.CTkFont(size=11), text_color="gray70")
        self.metric_verdict_label.pack(anchor="w", padx=10, pady=1)

        self.metric_rmse_label = ctk.CTkLabel(metric_card, text="RMSE (Ср. ошибка): —", font=ctk.CTkFont(size=11), text_color="gray70")
        self.metric_rmse_label.pack(anchor="w", padx=10, pady=(1, 8))

        # Size & savings card
        size_card = ctk.CTkFrame(container, fg_color=("gray85", "gray18"))
        size_card.pack(fill="x", pady=4)
        ctk.CTkLabel(size_card, text="💾 Размеры файлов", font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=10, pady=(8, 4))

        self.stat_orig_label = ctk.CTkLabel(size_card, text="Исходный: —", font=ctk.CTkFont(size=11))
        self.stat_orig_label.pack(anchor="w", padx=10, pady=1)

        self.stat_comp_label = ctk.CTkLabel(size_card, text="Сжатый: —", font=ctk.CTkFont(size=12, weight="bold"), text_color="#4CAF50")
        self.stat_comp_label.pack(anchor="w", padx=10, pady=1)

        self.stat_diff_label = ctk.CTkLabel(size_card, text="Экономия: —", font=ctk.CTkFont(size=11))
        self.stat_diff_label.pack(anchor="w", padx=10, pady=(1, 8))

        # EXIF Metadata Card
        self.exif_card = ctk.CTkFrame(container, fg_color=("gray85", "gray18"))
        self.exif_card.pack(fill="x", pady=4)
        ctk.CTkLabel(self.exif_card, text="📷 Данные камеры (EXIF)", font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=10, pady=(8, 4))

        self.exif_info_label = ctk.CTkLabel(self.exif_card, text="Метаданные не найдены", font=ctk.CTkFont(size=11), text_color="gray70", justify="left")
        self.exif_info_label.pack(anchor="w", padx=10, pady=(1, 8))

    # ----------------------------------------------------
    # Shortcuts & Keybindings
    # ----------------------------------------------------
    def _bind_global_shortcuts(self):
        self.bind("<KeyPress-space>", self._on_space_press)
        self.bind("<KeyRelease-space>", self._on_space_release)

    def _on_space_press(self, event):
        if not self.space_key_held:
            self.space_key_held = True
            self._render_canvas()

    def _on_space_release(self, event):
        if self.space_key_held:
            self.space_key_held = False
            self._render_canvas()

    def _toggle_loupe(self):
        self.loupe_enabled = not self.loupe_enabled
        if self.loupe_enabled:
            self.loupe_btn.configure(fg_color="#1F6AA5")
            self.status_label.configure(text="Лупа включена. Наведите курсор на холст для увеличения 4x.")
        else:
            self.loupe_btn.configure(fg_color="gray30")
        self._render_canvas()

    # ----------------------------------------------------
    # File Operations
    # ----------------------------------------------------
    def _choose_single_file(self):
        path = filedialog.askopenfilename(
            title="Выберите фото",
            filetypes=[("Изображения", "*.jpg *.jpeg *.png *.bmp *.webp *.tiff"), ("Все файлы", "*.*")]
        )
        if path:
            self.all_loaded_files = [path]
            self._update_file_dropdown()
            self.load_image(path)

    def _choose_multiple_files(self):
        paths = filedialog.askopenfilenames(
            title="Выберите изображения",
            filetypes=[("Изображения", "*.jpg *.jpeg *.png *.bmp *.webp *.tiff"), ("Все файлы", "*.*")]
        )
        if paths:
            self.all_loaded_files = list(paths)
            self._update_file_dropdown()
            self.load_image(self.all_loaded_files[0])

    def _on_file_drop(self, event):
        raw = event.data
        if not raw:
            return
        files = []
        if "{" in raw:
            import re
            files = re.findall(r"\{([^}]+)\}", raw)
        else:
            files = raw.split()
        valid = [f for f in files if os.path.isfile(f)]
        if valid:
            self.all_loaded_files = valid
            self._update_file_dropdown()
            self.load_image(self.all_loaded_files[0])

    def _update_file_dropdown(self):
        if not self.all_loaded_files:
            self.file_list_combobox.configure(values=["(Нет открытых файлов)"])
            self.file_list_combobox.set("(Нет открытых файлов)")
            return
        names = [f"({i+1}/{len(self.all_loaded_files)}) {os.path.basename(f)}" for i, f in enumerate(self.all_loaded_files)]
        self.file_list_combobox.configure(values=names)
        self.file_list_combobox.set(names[0])

    def _on_combobox_select(self, choice):
        if not self.all_loaded_files or "(" not in choice:
            return
        try:
            idx = int(choice.split("/")[0].replace("(", "")) - 1
            if 0 <= idx < len(self.all_loaded_files):
                self.load_image(self.all_loaded_files[idx])
        except Exception:
            pass

    def load_image(self, path: str):
        try:
            self.status_label.configure(text=f"Загрузка {os.path.basename(path)}...")
            self.update_idletasks()

            raw_im = Image.open(path)
            self.original_image = prepare_image_for_jpeg(raw_im)
            self.current_file_path = path
            self.original_bytes_count = os.path.getsize(path)

            w, h = self.original_image.size
            self.stat_orig_label.configure(text=f"Исходный: {format_bytes(self.original_bytes_count)} ({w}×{h} px)")

            # Read EXIF
            exif_dict = read_exif_info(path)
            if exif_dict:
                lines = [f"{k}: {v}" for k, v in exif_dict.items()]
                self.exif_info_label.configure(text="\n".join(lines))
            else:
                self.exif_info_label.configure(text="Метаданные отсутствуют или очищены")

            self.is_auto_fit = True
            self.pan_offset_x = 0
            self.pan_offset_y = 0
            self._trigger_compression()

        except Exception as e:
            messagebox.showerror("Ошибка загрузки", f"Не удалось открыть файл:\n{e}")

    # ----------------------------------------------------
    # Compression Logic & Worker
    # ----------------------------------------------------
    def _on_compression_mode_change(self, mode_str):
        if "Целевой" in mode_str:
            self.compression_mode = "target_size"
            self.quality_controls_frame.pack_forget()
            self.target_size_frame.pack(fill="x", pady=4, after=self.mode_selector.master)
        else:
            self.compression_mode = "quality"
            self.target_size_frame.pack_forget()
            self.quality_controls_frame.pack(fill="x", pady=4, after=self.mode_selector.master)
        self._trigger_compression()

    def _set_target_kb(self, kb: float):
        self.target_size_entry.delete(0, "end")
        self.target_size_entry.insert(0, str(int(kb)))
        self._apply_target_size()

    def _apply_target_size(self):
        if not self.original_image:
            return
        try:
            target_kb = float(self.target_size_entry.get().strip())
        except ValueError:
            messagebox.showwarning("Ошибка ввода", "Введите число (КБ)!")
            return

        self.status_label.configure(text=f"Подбор качества под {target_kb:.0f} КБ...")
        self.update_idletasks()

        q, comp_img, raw_bytes = find_quality_for_target_size(
            self.original_image,
            target_kb=target_kb,
            passes=int(self.passes_slider.get()),
            subsampling="4:2:0"
        )
        self.quality_slider.set(q)
        self.quality_val_label.configure(text=f"{q}%")
        self.mode_selector.set("Ручное качество (%)")
        self._on_compression_mode_change("Ручное качество (%)")

    def _get_current_params(self) -> dict:
        q = int(self.quality_slider.get())
        passes = int(self.passes_slider.get())
        sub_str = self.subsampling_var.get()
        sub = "4:2:0" if "4:2:0" in sub_str else ("4:2:2" if "4:2:2" in sub_str else "4:4:4")
        scale = float(self.scale_slider.get()) / 100.0
        pixelate = self.pixelate_var.get()
        shift_grid = self.shift_grid_var.get()
        chrom = int(self.chrom_slider.get())
        sat = float(self.sat_slider.get())
        cont = float(self.cont_slider.get())
        strip_ex = self.strip_exif_var.get()
        opt = self.optimize_var.get()
        fmt_str = self.export_fmt_var.get()
        exp_fmt = "PNG" if "PNG" in fmt_str else ("WEBP" if "WEBP" in fmt_str else "JPEG")

        return {
            "quality": q,
            "passes": passes,
            "subsampling": sub,
            "scale": scale,
            "pixelate": pixelate,
            "shift_grid": shift_grid,
            "chromatic_aberration": chrom,
            "saturation": sat,
            "contrast": cont,
            "strip_exif": strip_ex,
            "optimize": opt,
            "export_format": exp_fmt
        }

    def _trigger_compression(self):
        if self.original_image is None:
            return
        self.request_counter += 1
        params = self._get_current_params()
        self.status_label.configure(text="Сжатие...")
        self.work_queue.put((self.request_counter, self.original_image.copy(), params))

    def _compression_worker(self):
        while True:
            item = self.work_queue.get()
            if item is None:
                break
            req_id, img_copy, params = item
            while not self.work_queue.empty():
                req_id, img_copy, params = self.work_queue.get()

            t0 = time.time()
            try:
                comp_img, raw_bytes = compress_jpeg(img_copy, **params)
                metrics = compute_quality_metrics(img_copy, comp_img)
                heatmap = generate_difference_heatmap(img_copy, comp_img)
                dt_ms = (time.time() - t0) * 1000.0
                self.result_queue.put((req_id, comp_img, raw_bytes, metrics, heatmap, dt_ms))
            except Exception as e:
                self.result_queue.put((req_id, None, str(e), None, None, 0.0))

    def _check_worker_results(self):
        latest = None
        while not self.result_queue.empty():
            latest = self.result_queue.get_nowait()

        if latest:
            req_id, comp_img, raw_data, metrics, heatmap, dt_ms = latest
            if comp_img is not None:
                self.compressed_image = comp_img
                self.compressed_bytes = raw_data
                self.metrics_data = metrics
                self.heatmap_image = heatmap

                comp_sz = len(raw_data)
                w, h = comp_img.size
                self.stat_comp_label.configure(text=f"Сжатый: {format_bytes(comp_sz)} ({w}×{h} px)")

                _, diff_str = calculate_savings(self.original_bytes_count, comp_sz)
                self.stat_diff_label.configure(
                    text=f"Экономия: {diff_str}",
                    text_color="#4CAF50" if "-" in diff_str else "#E53935"
                )

                if metrics:
                    psnr = metrics["psnr"]
                    self.metric_psnr_label.configure(text=f"PSNR: {psnr:.2f} dB")
                    self.metric_verdict_label.configure(text=f"Оценка: {metrics['verdict']}")
                    self.metric_rmse_label.configure(text=f"RMSE (ошибка): {metrics['rmse']:.2f}")

                self.status_label.configure(
                    text=f"Готово ({dt_ms:.1f} мс) | Размер: {format_bytes(comp_sz)} ({diff_str}) | PSNR: {metrics['psnr']:.1f} dB"
                )
                self._render_canvas()

        self.after(30, self._check_worker_results)

    # ----------------------------------------------------
    # Control Callbacks & Presets
    # ----------------------------------------------------
    def _on_quality_change(self, val):
        q = int(val)
        self.quality_val_label.configure(text=f"{q}%")
        self._trigger_compression()

    def _on_passes_change(self, val):
        p = int(val)
        self.passes_val_label.configure(text=f"{p}x")
        self._trigger_compression()

    def _on_scale_change(self, val):
        s = int(val)
        self.scale_val_label.configure(text=f"{s}%")
        self._trigger_compression()

    def _on_chrom_change(self, val):
        c = int(val)
        self.chrom_val_label.configure(text=f"{c} px")
        self._trigger_compression()

    def _on_sat_change(self, val):
        s = float(val)
        self.sat_val_label.configure(text=f"{s:.1f}x")
        self._trigger_compression()

    def _on_cont_change(self, val):
        c = float(val)
        self.cont_val_label.configure(text=f"{c:.1f}x")
        self._trigger_compression()

    def _apply_preset(self, q: int, passes: int = 1, scale: float = 1.0,
                      pixelate: bool = False, shift_grid: bool = True,
                      sat: float = 1.0, cont: float = 1.0, chrom: int = 0):
        self.quality_slider.set(q)
        self.quality_val_label.configure(text=f"{q}%")
        self.passes_slider.set(passes)
        self.passes_val_label.configure(text=f"{passes}x")
        self.scale_slider.set(int(scale * 100))
        self.scale_val_label.configure(text=f"{int(scale * 100)}%")
        self.pixelate_var.set(pixelate)
        self.shift_grid_var.set(shift_grid)
        self.sat_slider.set(sat)
        self.sat_val_label.configure(text=f"{sat:.1f}x")
        self.cont_slider.set(cont)
        self.cont_val_label.configure(text=f"{cont:.1f}x")
        self.chrom_slider.set(chrom)
        self.chrom_val_label.configure(text=f"{chrom} px")
        self._trigger_compression()

    def _on_view_mode_change(self, val):
        if "Верт. сплит" in val:
            self.view_mode = "split_v"
        elif "Гориз. сплит" in val:
            self.view_mode = "split_h"
        elif "Тепловая" in val:
            self.view_mode = "heatmap"
        elif "Сжатое" in val:
            self.view_mode = "compressed"
        elif "Оригинал" in val:
            self.view_mode = "original"
        elif "Рядом" in val:
            self.view_mode = "side_by_side"
        self._render_canvas()

    # ----------------------------------------------------
    # Canvas Rendering, Zoom, Pan, Split & Loupe
    # ----------------------------------------------------
    def _on_canvas_resize(self, event):
        if self.is_auto_fit:
            self._reset_to_fit()
        else:
            self._render_canvas()

    def _reset_to_fit(self):
        if not self.original_image:
            self._render_empty_canvas()
            return
        cw = max(50, self.canvas.winfo_width())
        ch = max(50, self.canvas.winfo_height())
        iw, ih = self.original_image.size
        margin = 20
        scale = min(max(10, cw - margin * 2) / iw, max(10, ch - margin * 2) / ih)
        self.zoom_factor = scale
        self.pan_offset_x = 0
        self.pan_offset_y = 0
        self.is_auto_fit = True
        self.zoom_label.configure(text=f"{int(scale * 100)}%")
        self._render_canvas()

    def _zoom_100(self):
        self.zoom_factor = 1.0
        self.is_auto_fit = False
        self.zoom_label.configure(text="100%")
        self._render_canvas()

    def _adjust_zoom(self, mult: float):
        self.is_auto_fit = False
        self.zoom_factor = max(0.05, min(12.0, self.zoom_factor * mult))
        self.zoom_label.configure(text=f"{int(self.zoom_factor * 100)}%")
        self._render_canvas()

    def _on_mouse_wheel(self, event):
        mult = 1.15 if event.delta > 0 else 0.85
        self._adjust_zoom(mult)

    def _on_canvas_press_left(self, event):
        self.mouse_canvas_x = event.x
        self.mouse_canvas_y = event.y
        if self.view_mode in ("split_v", "split_h"):
            self.is_dragging_split = True
            self._update_split_from_event(event)

    def _on_canvas_drag_left(self, event):
        self.mouse_canvas_x = event.x
        self.mouse_canvas_y = event.y
        if self.is_dragging_split and self.view_mode in ("split_v", "split_h"):
            self._update_split_from_event(event)
        elif self.loupe_enabled:
            self._render_canvas()

    def _on_canvas_release_left(self, event):
        self.is_dragging_split = False

    def _on_canvas_motion(self, event):
        self.mouse_canvas_x = event.x
        self.mouse_canvas_y = event.y
        if self.loupe_enabled:
            self._render_canvas()

    def _on_canvas_press_right(self, event):
        self.is_panning = True
        self.pan_start_x = event.x
        self.pan_start_y = event.y

    def _on_canvas_drag_right(self, event):
        if self.is_panning:
            self.is_auto_fit = False
            dx = event.x - self.pan_start_x
            dy = event.y - self.pan_start_y
            self.pan_offset_x += dx
            self.pan_offset_y += dy
            self.pan_start_x = event.x
            self.pan_start_y = event.y
            self._render_canvas()

    def _on_canvas_release_right(self, event):
        self.is_panning = False

    def _update_split_from_event(self, event):
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        if self.view_mode == "split_v":
            self.split_ratio_x = max(0.02, min(0.98, event.x / cw))
        elif self.view_mode == "split_h":
            self.split_ratio_y = max(0.02, min(0.98, event.y / ch))
        self._render_canvas()

    def _render_empty_canvas(self):
        self.canvas.delete("all")
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        self.canvas.create_text(
            cw // 2, ch // 2 - 20,
            text="🖼️ Перетащите изображение сюда",
            fill="gray50", font=("Arial", 16, "bold")
        )
        self.canvas.create_text(
            cw // 2, ch // 2 + 15,
            text="или нажмите кнопку «Выбрать фото» слева",
            fill="gray40", font=("Arial", 12)
        )

    def _render_canvas(self):
        if self.original_image is None or self.compressed_image is None:
            self._render_empty_canvas()
            return

        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw < 20 or ch < 20:
            return

        orig_w, orig_h = self.original_image.size
        display_w = max(8, int(orig_w * self.zoom_factor))
        display_h = max(8, int(orig_h * self.zoom_factor))

        center_x = (cw // 2) + self.pan_offset_x
        center_y = (ch // 2) + self.pan_offset_y

        img_left = center_x - (display_w // 2)
        img_top = center_y - (display_h // 2)

        # Space key temporary override to Original
        active_mode = "original" if self.space_key_held else self.view_mode

        if active_mode == "original":
            disp = self.original_image.resize((display_w, display_h), Image.Resampling.BILINEAR)
        elif active_mode == "compressed":
            disp = self.compressed_image.resize((display_w, display_h), Image.Resampling.BILINEAR)
        elif active_mode == "heatmap" and self.heatmap_image:
            disp = self.heatmap_image.resize((display_w, display_h), Image.Resampling.BILINEAR)
        elif active_mode == "side_by_side":
            half_w = max(4, display_w // 2)
            left_orig = self.original_image.resize((half_w, display_h), Image.Resampling.BILINEAR)
            right_comp = self.compressed_image.resize((half_w, display_h), Image.Resampling.BILINEAR)
            disp = Image.new("RGB", (half_w * 2, display_h))
            disp.paste(left_orig, (0, 0))
            disp.paste(right_comp, (half_w, 0))
            display_w = half_w * 2
            img_left = center_x - (display_w // 2)
        elif active_mode == "split_h":
            # Horizontal split (Top = Original, Bottom = Compressed)
            orig_resized = self.original_image.resize((display_w, display_h), Image.Resampling.BILINEAR)
            comp_resized = self.compressed_image.resize((display_w, display_h), Image.Resampling.BILINEAR)

            canvas_split_y = int(ch * self.split_ratio_y)
            img_split_y = max(0, min(display_h, canvas_split_y - img_top))

            disp = Image.new("RGB", (display_w, display_h))
            if img_split_y > 0:
                disp.paste(orig_resized.crop((0, 0, display_w, img_split_y)), (0, 0))
            if img_split_y < display_h:
                disp.paste(comp_resized.crop((0, img_split_y, display_w, display_h)), (0, img_split_y))
        else:
            # Vertical split (Left = Original, Right = Compressed)
            orig_resized = self.original_image.resize((display_w, display_h), Image.Resampling.BILINEAR)
            comp_resized = self.compressed_image.resize((display_w, display_h), Image.Resampling.BILINEAR)

            canvas_split_x = int(cw * self.split_ratio_x)
            img_split_x = max(0, min(display_w, canvas_split_x - img_left))

            disp = Image.new("RGB", (display_w, display_h))
            if img_split_x > 0:
                disp.paste(orig_resized.crop((0, 0, img_split_x, display_h)), (0, 0))
            if img_split_x < display_w:
                disp.paste(comp_resized.crop((img_split_x, 0, display_w, display_h)), (img_split_x, 0))

        # Render Magnifier Loupe if enabled
        loupe_img = None
        loupe_x = self.mouse_canvas_x
        loupe_y = self.mouse_canvas_y

        if self.loupe_enabled and not self.space_key_held:
            # Calculate coordinates relative to image
            rel_x = int((loupe_x - img_left) / self.zoom_factor)
            rel_y = int((loupe_y - img_top) / self.zoom_factor)
            if 0 <= rel_x < orig_w and 0 <= rel_y < orig_h:
                source_for_loupe = self.heatmap_image if active_mode == "heatmap" else self.compressed_image
                loupe_pil = render_magnifier_loupe(source_for_loupe, rel_x, rel_y, radius=75, zoom=4)
                loupe_img = ImageTk.PhotoImage(loupe_pil)

        # Put on canvas
        self.tk_photo_cache = ImageTk.PhotoImage(disp)
        self.canvas.delete("all")
        self.canvas.create_image(img_left, img_top, anchor="nw", image=self.tk_photo_cache)

        # Overlays & Indicators
        if active_mode == "split_v":
            split_x = int(cw * self.split_ratio_x)
            self.canvas.create_line(split_x, 0, split_x, ch, fill="#3B8ED0", width=2)
            handle_y = ch // 2
            self.canvas.create_oval(split_x - 14, handle_y - 14, split_x + 14, handle_y + 14, fill="#1F6AA5", outline="#ffffff", width=2)
            self.canvas.create_text(split_x, handle_y, text="↔", fill="white", font=("Arial", 11, "bold"))
            self._draw_badge(max(10, split_x - 110), 16, "ДО (Оригинал)", "#2b2b2b")
            self._draw_badge(min(cw - 110, split_x + 10), 16, "ПОСЛЕ (JPEG)", "#1F6AA5")

        elif active_mode == "split_h":
            split_y = int(ch * self.split_ratio_y)
            self.canvas.create_line(0, split_y, cw, split_y, fill="#3B8ED0", width=2)
            handle_x = cw // 2
            self.canvas.create_oval(handle_x - 14, split_y - 14, handle_x + 14, split_y + 14, fill="#1F6AA5", outline="#ffffff", width=2)
            self.canvas.create_text(handle_x, split_y, text="↕", fill="white", font=("Arial", 11, "bold"))
            self._draw_badge(16, max(10, split_y - 36), "ВВЕРХ: Оригинал", "#2b2b2b")
            self._draw_badge(16, min(ch - 36, split_y + 10), "ВНИЗ: JPEG", "#1F6AA5")

        elif active_mode == "heatmap":
            self._draw_badge(16, 16, "♨ ТЕПЛОВАЯ КАРТА ПОТЕРЬ", "#B71C1C")
            # Legend at bottom right
            self._draw_badge(cw - 220, ch - 36, "Синий: норм | Красный: шум", "#2b2b2b")

        elif active_mode == "side_by_side":
            half_x = img_left + (display_w // 2)
            self.canvas.create_line(half_x, img_top, half_x, img_top + display_h, fill="#3B8ED0", width=2)
            self._draw_badge(img_left + 10, img_top + 10, "Оригинал", "#2b2b2b")
            self._draw_badge(half_x + 10, img_top + 10, "Сжатое", "#1F6AA5")

        if self.space_key_held:
            self._draw_badge(cw // 2 - 60, 20, "РЕЖИМ: ОРИГИНАЛ [ПРОБЕЛ]", "#E65100")

        # Draw Loupe on top if active
        if loupe_img is not None:
            self.tk_loupe_cache = loupe_img
            self.canvas.create_image(loupe_x - 75, loupe_y - 75, anchor="nw", image=self.tk_loupe_cache)

    def _draw_badge(self, x: int, y: int, text: str, bg_color: str):
        pad_x = 10
        fnt = ("Arial", 10, "bold")
        width = len(text) * 7 + pad_x * 2
        height = 22
        self.canvas.create_rectangle(x, y, x + width, y + height, fill=bg_color, outline="#ffffff", width=1)
        self.canvas.create_text(x + width // 2, y + height // 2, text=text, fill="white", font=fnt)

    # ----------------------------------------------------
    # Saving & Multi-Core Batch Processing
    # ----------------------------------------------------
    def _save_single_file(self):
        if not self.compressed_bytes:
            messagebox.showwarning("Внимание", "Нет сжатого изображения для сохранения.")
            return

        fmt_str = self.export_fmt_var.get()
        ext = ".png" if "PNG" in fmt_str else (".webp" if "WEBP" in fmt_str else ".jpg")

        default_name = f"compressed{ext}"
        if self.current_file_path:
            base, _ = os.path.splitext(os.path.basename(self.current_file_path))
            q = int(self.quality_slider.get())
            default_name = f"{base}_q{q}{ext}"

        save_path = filedialog.asksaveasfilename(
            title="Сохранить изображение",
            initialfile=default_name,
            defaultextension=ext,
            filetypes=[("Файл изображения", f"*{ext}")]
        )
        if save_path:
            try:
                with open(save_path, "wb") as f:
                    f.write(self.compressed_bytes)
                messagebox.showinfo("Успешно", f"Файл сохранен:\n{save_path}\nРазмер: {format_bytes(len(self.compressed_bytes))}")
            except Exception as e:
                messagebox.showerror("Ошибка сохранения", f"Не удалось сохранить:\n{e}")

    def _quick_save(self):
        if not self.compressed_bytes or not self.current_file_path:
            messagebox.showwarning("Внимание", "Сначала откройте изображение.")
            return

        dir_name = os.path.dirname(self.current_file_path)
        base, _ = os.path.splitext(os.path.basename(self.current_file_path))
        q = int(self.quality_slider.get())
        fmt_str = self.export_fmt_var.get()
        ext = ".png" if "PNG" in fmt_str else (".webp" if "WEBP" in fmt_str else ".jpg")
        out_path = os.path.join(dir_name, f"{base}_compressed_q{q}{ext}")

        try:
            with open(out_path, "wb") as f:
                f.write(self.compressed_bytes)
            self.status_label.configure(text=f"Быстрое сохранение: {os.path.basename(out_path)} ({format_bytes(len(self.compressed_bytes))})")
            messagebox.showinfo("Быстрое сохранение", f"Файл успешно сохранен рядом:\n{out_path}")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось сохранить:\n{e}")

    def _batch_compress_all(self):
        if not self.all_loaded_files:
            messagebox.showwarning("Пакетное сжатие", "Сначала выберите файлы через «Выбрать несколько».")
            return

        out_dir = filedialog.askdirectory(title="Выберите папку для сохранения сжатых файлов")
        if not out_dir:
            return

        params = self._get_current_params()
        q = params["quality"]
        fmt_str = self.export_fmt_var.get()
        ext = ".png" if "PNG" in fmt_str else (".webp" if "WEBP" in fmt_str else ".jpg")

        progress_window = ctk.CTkToplevel(self)
        progress_window.title("Многопоточное пакетное сжатие")
        progress_window.geometry("480x200")
        progress_window.transient(self)
        progress_window.grab_set()

        ctk.CTkLabel(
            progress_window,
            text=f"Параллельное сжатие {len(self.all_loaded_files)} файлов...",
            font=ctk.CTkFont(size=13, weight="bold")
        ).pack(padx=20, pady=(16, 6))

        p_bar = ctk.CTkProgressBar(progress_window, width=420)
        p_bar.set(0.0)
        p_bar.pack(padx=20, pady=8)

        p_status = ctk.CTkLabel(progress_window, text="Запуск рабочих потоков CPU...")
        p_status.pack(padx=20, pady=4)

        def worker_job(fpath: str) -> Tuple[bool, int, int]:
            try:
                raw_sz = os.path.getsize(fpath)
                raw_im = Image.open(fpath)
                prep_im = prepare_image_for_jpeg(raw_im)
                _, c_bytes = compress_jpeg(prep_im, **params)

                base, _ = os.path.splitext(os.path.basename(fpath))
                out_name = f"{base}_q{q}{ext}"
                dest_path = os.path.join(out_dir, out_name)

                with open(dest_path, "wb") as f_out:
                    f_out.write(c_bytes)
                return True, raw_sz, len(c_bytes)
            except Exception as ex:
                print(f"Error on {fpath}: {ex}")
                return False, 0, 0

        def run_batch_parallel():
            total = len(self.all_loaded_files)
            orig_total = 0
            comp_total = 0
            success_count = 0
            completed = 0

            max_workers = min(8, max(1, os.cpu_count() or 4))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(worker_job, f) for f in self.all_loaded_files]
                for fut in futures:
                    ok, o_sz, c_sz = fut.result()
                    completed += 1
                    if ok:
                        success_count += 1
                        orig_total += o_sz
                        comp_total += c_sz

                    progress_window.after(0, lambda c=completed: p_bar.set(c / total))
                    progress_window.after(0, lambda c=completed: p_status.configure(text=f"Обработано {c} из {total} файлов..."))

            _, diff_str = calculate_savings(orig_total, comp_total)

            def on_complete():
                progress_window.destroy()
                msg = (
                    f"Пакетное сжатие завершено!\n\n"
                    f"Успешно обработано: {success_count} из {total}\n"
                    f"Исходный размер: {format_bytes(orig_total)}\n"
                    f"Итоговый размер: {format_bytes(comp_total)}\n"
                    f"Экономия памяти: {diff_str}\n\n"
                    f"Папка с результатами:\n{out_dir}"
                )
                if messagebox.askyesno("Готово", msg + "\n\nОткрыть папку в Проводнике?"):
                    try:
                        os.startfile(out_dir)
                    except Exception:
                        pass

            progress_window.after(200, on_complete)

        threading.Thread(target=run_batch_parallel, daemon=True).start()


def main():
    app = JpegCompressorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
