"""Приведение всех публикуемых фото к единой ширине.

Владелец (2026-08-17): все фото в ленте должны быть одной ширины. Обрезка горизонтальных
фото под портрет для этого не годится — вырезает большую часть кадра, теряя контент.
Вместо этого просто масштабируем по ширине (высота меняется свободно, пропорции не
трогаем) — портретное фото остаётся высоким, горизонтальное невысоким, но ширина у
всех одинаковая, и именно ширину показывает Telegram в чате/группе.
"""
from PIL import Image, ImageOps

TARGET_WIDTH = 1080


def normalize_photo(path: str) -> None:
    """Масштабирует файл по указанному пути на месте (в тот же файл) под TARGET_WIDTH."""
    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)  # учитываем поворот из EXIF, если есть
            img = img.convert("RGB")
            if img.width != TARGET_WIDTH:
                target_height = round(img.height * TARGET_WIDTH / img.width)
                img = img.resize((TARGET_WIDTH, target_height), Image.LANCZOS)
            img.save(path, quality=90)
    except Exception as e:
        print(f"[images] не смог нормализовать {path}: {e}")
