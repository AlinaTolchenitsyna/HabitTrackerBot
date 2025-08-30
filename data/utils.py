# utils.py
import random
from typing import List

MOTIVATIONAL_PHRASES: List[str] = [
    "Отлично! Так держать!",
    "Молодец — маленький шаг к большой цели!",
    "Прекрасно! Продолжай в том же духе!",
    "Круто! Ты на пути к привычке!",
    "Вот это продуктивность — горжусь тобой!",
    "Ещё один день — ещё один прогресс!",
    "Удивительно! Ты справился(ась)!",
]

def get_motivation() -> str:
    """Вернуть случайную мотивационную фразу."""
    return random.choice(MOTIVATIONAL_PHRASES)
