"""Разбор объявлений: есть ли нужный товар и что хочет автор — купить или продать.

Модуль не зависит от Telegram, поэтому его легко проверять тестами.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

BUY = "buy"
SELL = "sell"
UNKNOWN = "unknown"

# По умолчанию ищем именно iPhone Duo (в любом написании), а не любой iPhone.
DEFAULT_PRODUCT_PATTERNS = [
    r"(?:i\s?phone|iphon|айфон\w*|афон\w*|ифон\w*|айф)\s*[-–—_.,:]?\s*(?:duo|дуо|дуа)\b",
]

BUY_PATTERNS = [
    r"\bкуплю\b", r"\bкупим\b", r"\bкуплю-?\w*", r"\bпокупаю\b", r"\bпокупка\b",
    r"\bищу\b", r"\bищем\b", r"\bразыскива\w+",
    r"\bнуж(?:ен|на|но|ны)\b", r"\bвозьму\b", r"\bзаберу\b", r"\bприобрету\b",
    r"\bвыкуп\w*", r"\bскуп\w*",
    r"\bхочу\s+(?:купить|приобрести|взять)\b",
    r"\bкто\s+(?:прода[её]т|продаст)\b", r"\bу\s+кого\s+есть\b",
    r"\bwtb\b", r"\bbuy(?:ing)?\b", r"\blooking\s+for\b",
]

SELL_PATTERNS = [
    r"\bпродам\b", r"\bпродаю\b", r"\bпрода[её]м\b", r"\bпрода[её]тся\b",
    r"\bпродажа\b", r"\bпродажу\b", r"\bна\s+продаж\w+", r"\bв\s+продаже\b",
    r"\bотдам\b", r"\bуступлю\b", r"\bреализую\b",
    r"\bв\s+наличии\b", r"\bторг\b", r"\bбез\s+торга\b",
    r"\bwts\b", r"\bsell(?:ing)?\b", r"\bfor\s+sale\b",
]


@dataclass(frozen=True)
class Match:
    intent: str          # BUY / SELL / UNKNOWN
    product: str         # какой фрагмент текста совпал с товаром


def normalize(text: str) -> str:
    text = text.lower().replace("ё", "е")
    # неразрывные пробелы и прочие невидимые символы превращаем в обычный пробел
    return re.sub(r"[ ​-‍⁠﻿]", " ", text)


def _compile(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(normalize(p), re.IGNORECASE) for p in patterns]


class Matcher:
    def __init__(
        self,
        product_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ) -> None:
        self._products = _compile(product_patterns or DEFAULT_PRODUCT_PATTERNS)
        self._exclude = _compile(exclude_patterns or [])
        self._buy = _compile(BUY_PATTERNS)
        self._sell = _compile(SELL_PATTERNS)

    def match(self, text: str | None) -> Match | None:
        """Вернёт Match, если в тексте упомянут товар, иначе None."""
        if not text:
            return None
        norm = normalize(text)

        found = None
        for pattern in self._products:
            found = pattern.search(norm)
            if found:
                break
        if not found:
            return None
        if any(p.search(norm) for p in self._exclude):
            return None

        return Match(intent=self._intent(norm, found.start()), product=found.group(0).strip())

    def _intent(self, norm: str, product_pos: int) -> str:
        buy = self._nearest(self._buy, norm, product_pos)
        sell = self._nearest(self._sell, norm, product_pos)
        if buy is None and sell is None:
            return UNKNOWN
        if sell is None:
            return BUY
        if buy is None:
            return SELL
        # Есть оба признака («куплю или обменяю, продам чехол») —
        # берём тот, что стоит ближе к названию товара.
        return BUY if buy <= sell else SELL

    @staticmethod
    def _nearest(patterns: list[re.Pattern[str]], norm: str, pos: int) -> int | None:
        best = None
        for pattern in patterns:
            for m in pattern.finditer(norm):
                # слово перед товаром («куплю iphone duo») весит больше, чем после
                dist = pos - m.end() if m.end() <= pos else (m.start() - pos) * 2
                if best is None or dist < best:
                    best = dist
        return best
