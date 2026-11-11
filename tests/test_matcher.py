import pytest

from matcher import BUY, SELL, UNKNOWN, Matcher

m = Matcher()


@pytest.mark.parametrize("text,intent", [
    ("Куплю iPhone Duo, рассмотрю варианты", BUY),
    ("куплю айфон дуо срочно", BUY),
    ("Ищу Айфон-Дуо 256гб в хорошем состоянии", BUY),
    ("Кто продаёт iphone duo? напишите в лс", BUY),
    ("#куплю\nIPHONE DUO", BUY),
    ("Выкуп техники: iPhone Duo, дорого", BUY),
    ("Продам iPhone Duo 512, идеал, торг", SELL),
    ("Продаю айфон дуо, полный комплект", SELL),
    ("iPhone Duo. Продаётся, цена 150к", SELL),
    ("В наличии iPhoneDuo, новые, запечатанные", SELL),
    ("Продам iPhone Duo", SELL),
    ("iPhone Duo 256 чёрный 140 000", UNKNOWN),
    ("Продам чехол. Куплю iPhone Duo", BUY),
    ("Куплю чехол. Продам iPhone Duo", SELL),
])
def test_intent(text, intent):
    result = m.match(text)
    assert result is not None
    assert result.intent == intent


@pytest.mark.parametrize("text", [
    "Продам iPhone 15 Pro Max",
    "Куплю айфон 13, недорого",
    "Продам Surface Duo",
    "Продам iPhone 14 и колонку Duolingo",
    "Сдам квартиру",
    "",
    None,
])
def test_other_goods_are_ignored(text):
    assert m.match(text) is None


def test_exclude_patterns():
    custom = Matcher(exclude_patterns=[r"чехол", r"стекло"])
    assert custom.match("Продам чехол на iPhone Duo") is None
    assert custom.match("Продам iPhone Duo") is not None


def test_custom_product_patterns():
    custom = Matcher(product_patterns=[r"pixel\s*fold"])
    assert custom.match("Куплю Pixel Fold").intent == BUY
    assert custom.match("Куплю iPhone Duo") is None
