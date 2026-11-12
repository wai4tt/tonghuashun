"""Finite numeric reads for external/user facts, preserving their original values."""
from __future__ import annotations
import math
import re
from typing import Any

_NUMERIC_TEXT = re.compile(r"[-+]?(?:(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?")


def finite_number(value: Any, *, allow_text: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not allow_text:
            return None
        text = value.strip()
        if len(text) > 256 or _NUMERIC_TEXT.fullmatch(text) is None:
            return None
        value = text.replace(",", "")
    elif type(value) not in {int, float}:
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None
