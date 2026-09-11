"""Общий клиент YandexGPT — вынесен из content/news.py, чтобы content/digest_engine.py
мог переиспользовать его же, не дублируя и не создавая циклический импорт (news.py и
digest_engine.py оба зависят от этого модуля, а не друг от друга)."""
import json
import re

import requests

from config import settings

YANDEX_COMPLETION_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
YANDEX_MODEL = "yandexgpt/latest"  # рерайт/итоговый текст — нюанс важен, остаёмся на Pro
# Дешевле, для чисто структурной оценки без пересказа (digest_engine.py::_classify) — по
# биллингу 2026-09-02 именно классификация (не рерайт и не картинки) съедала основную часть
# расхода: она идёт на КАЖДЫЙ кандидат (70-125/день), а не только на то, что публикуется.
YANDEX_MODEL_LITE = "yandexgpt-lite/latest"


def call_yandexgpt(system_prompt: str, user_content: str, max_tokens: int = 2000, model: str = YANDEX_MODEL) -> str:
    payload = {
        "modelUri": f"gpt://{settings.YANDEX_FOLDER_ID}/{model}",
        "completionOptions": {"stream": False, "temperature": 0.3, "maxTokens": str(max_tokens)},
        "messages": [
            {"role": "system", "text": system_prompt},
            {"role": "user", "text": user_content},
        ],
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {settings.YANDEX_API_KEY}",
        "x-folder-id": settings.YANDEX_FOLDER_ID,
    }
    r = requests.post(YANDEX_COMPLETION_URL, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"YandexGPT вернул ошибку: {data['error']}")
    return data["result"]["alternatives"][0]["message"]["text"]


def extract_json(raw: str) -> dict:
    """YandexGPT не гарантирует чистый JSON (в отличие от structured outputs у Claude) —
    иногда оборачивает в ```json ... ``` или добавляет пояснение. Вырезаем сам объект."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError(f"Не нашёл JSON в ответе модели: {raw[:200]!r}")
    return json.loads(match.group(0))
