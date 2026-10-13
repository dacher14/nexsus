"""Семантический анализ описаний через Claude API (раздел 23).

Вызывается только для неоднозначных объявлений (модель не распознана или
неисправность не уточнена), с лимитом вызовов на кампанию и кэшем по
содержимому — неизменившиеся объявления повторно не анализируются.
Результат — гипотезы, которые дополняют, а не заменяют эвристику.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .categories import CATEGORIES

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты помогаешь оценивать объявления вторичного рынка (Avito, Россия) для перепродажи.
По заголовку и описанию определи категорию, бренд и точную модель товара, а также
заявленные продавцом неисправности и признаки риска. Опирайся только на текст
объявления: не придумывай дефекты, которых нет в тексте, и отмечай неопределённость.
Коды симптомов и рисков выбирай только из переданных списков."""


@dataclass
class LLMFindings:
    category: str = ""
    brand: str = ""
    model: str = ""
    variant: str = ""
    model_confident: bool = False
    symptoms: list[tuple[str, str]] = field(default_factory=list)
    red_flags: list[tuple[str, str]] = field(default_factory=list)
    summary: str = ""


def _schema(symptom_codes: list[str], flag_codes: list[str]) -> dict[str, Any]:
    item = lambda codes: {  # noqa: E731
        "type": "object",
        "properties": {"code": {"type": "string", "enum": codes}, "evidence": {"type": "string"}},
        "required": ["code", "evidence"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": sorted(CATEGORIES)},
            "brand": {"type": "string"},
            "model": {"type": "string"},
            "variant": {"type": "string"},
            "model_confident": {"type": "boolean"},
            "symptoms": {"type": "array", "items": item(symptom_codes)},
            "red_flags": {"type": "array", "items": item(flag_codes)},
            "summary": {"type": "string"},
        },
        "required": ["category", "brand", "model", "variant", "model_confident",
                     "symptoms", "red_flags", "summary"],
        "additionalProperties": False,
    }


def content_key(title: str, description: str) -> str:
    return hashlib.sha256(f"{title}\n{description}".encode()).hexdigest()


class LLMAnalyzer:
    def __init__(self, symptom_codes: list[str], flag_codes: list[str], model: str = "claude-opus-5-5",
                 effort: str = "low", max_calls: int = 50, client: Any = None,
                 cache_get: Callable[[str], dict | None] | None = None,
                 cache_put: Callable[[str, dict], None] | None = None):
        self.symptom_codes = symptom_codes
        self.flag_codes = flag_codes
        self.model = model
        self.effort = effort
        self.max_calls = max_calls
        self.calls = 0
        self._client = client
        self._cache_get = cache_get or (lambda key: None)
        self._cache_put = cache_put or (lambda key, value: None)

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic  # опциональная зависимость: pip install anthropic
            self._client = anthropic.Anthropic()
        return self._client

    def analyze(self, title: str, description: str, price: int) -> LLMFindings | None:
        key = content_key(title, description)
        cached = self._cache_get(key)
        if cached is not None:
            return _findings(cached)
        if self.calls >= self.max_calls:
            return None
        self.calls += 1
        prompt = (f"Заголовок: {title}\nЦена: {price} ₽\nОписание:\n{description or '(нет описания)'}\n\n"
                  f"Допустимые коды симптомов: {', '.join(self.symptom_codes)}\n"
                  f"Допустимые коды рисков: {', '.join(self.flag_codes)}")
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=8000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=SYSTEM_PROMPT,
                output_config={"effort": self.effort,
                               "format": {"type": "json_schema",
                                          "schema": _schema(self.symptom_codes, self.flag_codes)}},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # сеть, лимиты, авторизация — анализ продолжается без LLM
            log.warning("LLM-анализ недоступен: %s", exc)
            return None
        if response.stop_reason in ("refusal", "max_tokens"):
            log.warning("LLM не вернул результат (stop_reason=%s)", response.stop_reason)
            return None
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("LLM вернул некорректный JSON")
            return None
        self._cache_put(key, data)
        return _findings(data)


def _findings(data: dict[str, Any]) -> LLMFindings:
    return LLMFindings(
        category=data.get("category", ""),
        brand=data.get("brand", ""),
        model=data.get("model", ""),
        variant=data.get("variant", ""),
        model_confident=bool(data.get("model_confident")),
        symptoms=[(s["code"], "LLM: " + s.get("evidence", "")) for s in data.get("symptoms", [])],
        red_flags=[(f["code"], "LLM: " + f.get("evidence", "")) for f in data.get("red_flags", [])],
        summary=data.get("summary", ""),
    )
