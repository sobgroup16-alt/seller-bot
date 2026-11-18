"""Загрузка texts.yaml и безопасная подстановка {переменных}."""
from __future__ import annotations

from pathlib import Path

import yaml

TEXTS_PATH = Path(__file__).resolve().parent / "texts.yaml"


class _Safe(dict):
    def __missing__(self, key):  # неизвестная {переменная} останется как есть
        return "{" + key + "}"


def load_texts(path: Path = TEXTS_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "sources" not in data:
        raise SystemExit("Файл texts.yaml повреждён: не найден раздел sources. Проверьте отступы.")
    return data


def fmt(template: str, **kwargs) -> str:
    return str(template or "").strip().format_map(_Safe(**kwargs))
