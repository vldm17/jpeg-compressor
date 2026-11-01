"""
JPEG Compressor Core Engine 2.0
Расширенное ядро сжатия JPEG с поддержкой:
- Квантования и поколенческих потерь (Multi-pass generation loss)
- Расчета метрик качества (PSNR, RMSE, экономия)
- Тепловой карты потерь (Difference Heatmap)
- Хроматической аберрации и Deep-Fried эффектов
- Подбора качества под целевой размер файла (Target Size)
- Очистки / чтения EXIF метаданных
- Экспорта в JPG, PNG и WEBP
"""

import io
import math
import os
from typing import Tuple, Optional, Dict, Any, List
import numpy as np
from PIL import Image, ImageOps, ImageChops, ImageEnhance, ImageDraw

# Precomputed Turbo/Jet color palette for 256 error values
def _build_heatmap_palette() -> List[int]:
    palette = []
    for i in range(256):
        norm = i / 255.0
        # Turbo-like colormap approximation: black -> blue -> cyan -> green -> yellow -> red -> white
        if norm < 0.15:
            t = norm / 0.15
            r, g, b = 0, int(t * 80), int(t * 220)
        elif norm < 0.4:
            t = (norm - 0.15) / 0.25
            r, g, b = 0, int(80 + t * 140), int(220 - t * 40)
        elif norm < 0.7:
            t = (norm - 0.4) / 0.3
            r, g, b = int(t * 240), int(220 + t * 35), int(180 - t * 180)
        elif norm < 0.9:
            t = (norm - 0.7) / 0.2
            r, g, b = 255, int(255 - t * 180), 0
        else:
            t = (norm - 0.9) / 0.1
            r, g, b = 255, int(75 + t * 180), int(t * 255)
        palette.extend([r, g, b])
    return palette

HEATMAP_PALETTE = _build_heatmap_palette()


def prepare_image_for_jpeg(
    image: Image.Image,
    bg_color: Tuple[int, int, int] = (255, 255, 255)
) -> Image.Image:
    """Нормализует изображение: исправляет поворот по EXIF и композитит прозрачность."""
    try:
        image = ImageOps.exif_transpose(image)
    except Exception:
        pass

    if image.mode in ('RGBA', 'LA') or (image.mode == 'P' and 'transparency' in image.info):
        rgba = image.convert('RGBA')
        background = Image.new('RGB', rgba.size, bg_color)
        background.paste(rgba, mask=rgba.split()[3])
        return background
    elif image.mode != 'RGB':
        return image.convert('RGB')

    return image


def roll_image(im: Image.Image, delta_x: int, delta_y: int) -> Image.Image:
    """Циклический сдвиг изображения для симуляции смещения 8x8 DCT сетки при пересохранении."""
    w, h = im.size
    delta_x = delta_x % w
    delta_y = delta_y % h
    if delta_x == 0 and delta_y == 0:
        return im.copy()

    rolled = Image.new(im.mode, (w, h))
    part_tl = im.crop((0, 0, w - delta_x, h - delta_y))
    part_tr = im.crop((w - delta_x, 0, w, h - delta_y))
    part_bl = im.crop((0, h - delta_y, w - delta_x, h))
    part_br = im.crop((w - delta_x, h - delta_y, w, h))

    rolled.paste(part_br, (0, 0))
    rolled.paste(part_bl, (delta_x, 0))
    rolled.paste(part_tr, (0, delta_y))
    rolled.paste(part_tl, (delta_x, delta_y))
    return rolled


def apply_chromatic_aberration(im: Image.Image, offset: int = 0) -> Image.Image:
    """Сдвигает красный и синий каналы для создания винтажного/глитч цветового расхождения."""
    if offset <= 0:
        return im
    r, g, b = im.split()
    r_shifted = ImageChops.offset(r, -offset, 0)
    b_shifted = ImageChops.offset(b, offset, 0)
    return Image.merge('RGB', (r_shifted, g, b_shifted))


def apply_deep_fry(
    im: Image.Image,
    saturation: float = 1.0,
    contrast: float = 1.0,
    sharpness: float = 1.0
) -> Image.Image:
    """Усиливает насыщенность, контраст и резкость для аутентичного "deep-fried" эффекта."""
    cur = im
    if abs(saturation - 1.0) > 0.01:
        cur = ImageEnhance.Color(cur).enhance(saturation)
    if abs(contrast - 1.0) > 0.01:
        cur = ImageEnhance.Contrast(cur).enhance(contrast)
    if abs(sharpness - 1.0) > 0.01:
        cur = ImageEnhance.Sharpness(cur).enhance(sharpness)
    return cur


def compress_jpeg(
    image: Image.Image,
    quality: int = 50,
    passes: int = 1,
    subsampling: str = "4:2:0",
    scale: float = 1.0,
    pixelate: bool = False,
    shift_grid: bool = True,
    chromatic_aberration: int = 0,
    saturation: float = 1.0,
    contrast: float = 1.0,
    strip_exif: bool = True,
    optimize: bool = True,
    export_format: str = "JPEG"
) -> Tuple[Image.Image, bytes]:
    """
    Основная процедура сжатия и стилизации JPEG.
    Возвращает (compressed_pil_image, raw_file_bytes).
    """
    quality = max(1, min(100, int(quality)))
    passes = max(1, min(15, int(passes)))
    scale = max(0.05, min(1.0, float(scale)))

    valid_subsampling = {"4:2:0": 2, "4:2:2": 1, "4:4:4": 0}
    sub_code = valid_subsampling.get(subsampling, 2)

    cur = prepare_image_for_jpeg(image)
    orig_w, orig_h = cur.size

    # 1. Применение хроматической аберрации перед компрессией
    if chromatic_aberration > 0:
        cur = apply_chromatic_aberration(cur, chromatic_aberration)

    # 2. Предварительное усиление для эффекта Deep-fry
    if saturation > 1.01 or contrast > 1.01:
        cur = apply_deep_fry(cur, saturation=saturation, contrast=contrast, sharpness=1.2)

    # 3. Масштабирование
    if scale < 0.999:
        scaled_w = max(8, int(orig_w * scale))
        scaled_h = max(8, int(orig_h * scale))
        cur = cur.resize((scaled_w, scaled_h), Image.Resampling.BILINEAR)
        if pixelate:
            cur = cur.resize((orig_w, orig_h), Image.Resampling.NEAREST)

    total_shift_x = 0
    total_shift_y = 0
    shift_step = 3 if shift_grid and passes > 1 else 0

    last_bytes = b""

    for p in range(passes):
        buf = io.BytesIO()
        cur.save(
            buf,
            format="JPEG",
            quality=quality,
            subsampling=sub_code,
            optimize=optimize
        )
        last_bytes = buf.getvalue()

        buf.seek(0)
        cur = Image.open(buf)
        cur.load()

        if p < passes - 1 and shift_step > 0:
            cur = roll_image(cur, shift_step, shift_step)
            total_shift_x += shift_step
            total_shift_y += shift_step

    # Восстановление выравнивания после сдвига сетки
    if total_shift_x > 0 or total_shift_y > 0:
        cur = roll_image(cur, -total_shift_x, -total_shift_y)
        final_buf = io.BytesIO()
        cur.save(
            final_buf,
            format="JPEG",
            quality=quality,
            subsampling=sub_code,
            optimize=optimize
        )
        last_bytes = final_buf.getvalue()

    # Если запрошен экспорт в PNG или WebP
    if export_format.upper() == "PNG":
        png_buf = io.BytesIO()
        cur.save(png_buf, format="PNG", optimize=True)
        last_bytes = png_buf.getvalue()
    elif export_format.upper() == "WEBP":
        webp_buf = io.BytesIO()
        cur.save(webp_buf, format="WEBP", quality=quality)
        last_bytes = webp_buf.getvalue()

    return cur, last_bytes


def compute_quality_metrics(orig_im: Image.Image, comp_im: Image.Image) -> Dict[str, Any]:
    """Вычисляет PSNR (dB) и RMSE между оригиналом и сжатым изображением."""
    w, h = orig_im.size
    comp_matched = comp_im
    if comp_im.size != (w, h):
        comp_matched = comp_im.resize((w, h), Image.Resampling.BILINEAR)

    arr_orig = np.asarray(orig_im, dtype=np.float32)
    arr_comp = np.asarray(comp_matched, dtype=np.float32)

    diff = arr_orig - arr_comp
    mse = float(np.mean(diff ** 2))
    rmse = math.sqrt(mse)

    if mse <= 1e-6:
        psnr = 99.9
        verdict = "Идеальное совпадение"
    else:
        psnr = 20.0 * math.log10(255.0 / rmse)
        if psnr >= 38:
            verdict = "Отличное качество (почти без потерь)"
        elif psnr >= 30:
            verdict = "Хорошее качество (веб-стандарт)"
        elif psnr >= 24:
            verdict = "Заметные артефакты"
        elif psnr >= 18:
            verdict = "Сильное сжатие (выраженные блоки)"
        else:
            verdict = "Экстремальное разрушение (шакал)"

    return {
        "psnr": psnr,
        "rmse": rmse,
        "verdict": verdict,
        "mse": mse
    }


def generate_difference_heatmap(
    orig_im: Image.Image,
    comp_im: Image.Image,
    amplification: float = 6.0
) -> Image.Image:
    """
    Генерирует цветовую тепловую карту (Turbo colormap) распределения ошибок и потерь качества.
    Черный/синий = без изменений, зеленый/желтый = умеренная потеря, красный/белый = максимальные искажения.
    """
    w, h = orig_im.size
    comp_matched = comp_im
    if comp_im.size != (w, h):
        comp_matched = comp_im.resize((w, h), Image.Resampling.BILINEAR)

    arr_orig = np.asarray(orig_im, dtype=np.float32)
    arr_comp = np.asarray(comp_matched, dtype=np.float32)

    # Средняя ошибка по каналам
    diff = np.mean(np.abs(arr_orig - arr_comp), axis=2)
    boosted = np.clip(diff * amplification, 0, 255).astype(np.uint8)

    heat_im = Image.fromarray(boosted, mode='P')
    heat_im.putpalette(HEATMAP_PALETTE)
    return heat_im.convert('RGB')


def find_quality_for_target_size(
    image: Image.Image,
    target_kb: float,
    passes: int = 1,
    subsampling: str = "4:2:0",
    tolerance: float = 0.05,
    max_steps: int = 8
) -> Tuple[int, Image.Image, bytes]:
    """Бинарный поиск значения качества для достижения желаемого размера файла (в КБ)."""
    target_bytes = int(target_kb * 1024)
    low, high = 1, 95
    best_q = 50
    best_img = None
    best_bytes = b""

    for _ in range(max_steps):
        mid = (low + high) // 2
        comp_img, raw_bytes = compress_jpeg(
            image, quality=mid, passes=passes, subsampling=subsampling
        )
        sz = len(raw_bytes)
        best_q = mid
        best_img = comp_img
        best_bytes = raw_bytes

        if abs(sz - target_bytes) / max(1, target_bytes) <= tolerance:
            break

        if sz < target_bytes:
            low = mid + 1
        else:
            high = mid - 1

        if low > high:
            break

    return best_q, best_img, best_bytes


def render_magnifier_loupe(
    img: Image.Image,
    center_x: int,
    center_y: int,
    radius: int = 75,
    zoom: int = 4
) -> Image.Image:
    """Создает круглую интерактивную экранную лупу с увеличением пикселей."""
    w, h = img.size
    crop_size = radius // zoom
    left = max(0, min(w - crop_size * 2, center_x - crop_size))
    top = max(0, min(h - crop_size * 2, center_y - crop_size))

    cropped = img.crop((left, top, left + crop_size * 2, top + crop_size * 2))
    zoomed = cropped.resize((radius * 2, radius * 2), Image.Resampling.NEAREST)

    mask = Image.new('L', (radius * 2, radius * 2), 0)
    draw_mask = ImageDraw.Draw(mask)
    draw_mask.ellipse((0, 0, radius * 2 - 1, radius * 2 - 1), fill=255)

    res = Image.new('RGBA', (radius * 2, radius * 2), (0, 0, 0, 0))
    res.paste(zoomed.convert('RGBA'), (0, 0), mask=mask)

    draw_res = ImageDraw.Draw(res)
    # Линзовый ободок
    draw_res.ellipse((1, 1, radius * 2 - 2, radius * 2 - 2), outline=(59, 130, 246, 255), width=3)
    # Перекрестие в центре
    cx, cy = radius, radius
    draw_res.line((cx - 6, cy, cx + 6, cy), fill=(255, 255, 255, 200), width=1)
    draw_res.line((cx, cy - 6, cx, cy + 6), fill=(255, 255, 255, 200), width=1)
    return res


def format_bytes(size_in_bytes: int) -> str:
    """Форматирует байты в читаемую строку (Б, КБ, МБ)."""
    if size_in_bytes < 1024:
        return f"{size_in_bytes} Б"
    elif size_in_bytes < 1024 * 1024:
        return f"{size_in_bytes / 1024:.1f} КБ"
    else:
        return f"{size_in_bytes / (1024 * 1024):.2f} МБ"


def calculate_savings(original_bytes: int, compressed_bytes: int) -> Tuple[float, str]:
    """Вычисляет процент экономии места."""
    if original_bytes <= 0:
        return 0.0, "0%"
    diff = ((compressed_bytes - original_bytes) / original_bytes) * 100.0
    if diff <= 0:
        return diff, f"{diff:.1f}%"
    else:
        return diff, f"+{diff:.1f}%"


def read_exif_info(file_path: str) -> Dict[str, str]:
    """Считывает сводку основных метаданных EXIF (если присутствуют)."""
    info = {}
    try:
        im = Image.open(file_path)
        exif = im.getexif()
        if exif:
            tag_map = {
                271: "Камера",
                272: "Модель",
                306: "Дата съемки",
                33434: "Выдержка",
                33437: "Диафрагма (F)",
                34855: "ISO"
            }
            for tag_id, name in tag_map.items():
                val = exif.get(tag_id)
                if val:
                    info[name] = str(val)
    except Exception:
        pass
    return info
