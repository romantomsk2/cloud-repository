import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("telethon")
from telethon.tl.types import User  # noqa: E402

from matcher import Matcher  # noqa: E402
from monitor import DEFAULTS, Monitor, Store  # noqa: E402


class FakeClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, target, text, **kwargs):
        self.sent.append((target, text))


def make_message(msg_id, text, sender, chat_id=-100500, out=False):
    chat = SimpleNamespace(title="Барахолка", username="baraholka")

    async def get_chat():
        return chat

    async def get_sender():
        return sender

    return SimpleNamespace(id=msg_id, chat_id=chat_id, raw_text=text, out=out,
                           get_chat=get_chat, get_sender=get_sender)


def make_monitor(tmp_path, dry_run=False, **reply):
    cfg = {**DEFAULTS, "dry_run": dry_run,
           "reply": {**DEFAULTS["reply"], "to_buyer": "Привет, {name}! Продаю.",
                     "to_seller": "Привет, {name}! Куплю.", "delay_seconds": [0, 0], **reply}}
    client = FakeClient()
    return Monitor(client, cfg, Store(tmp_path / "state.db"), Matcher()), client


async def drain(monitor):
    while not monitor.queue.empty():
        message, sender, text = monitor.queue.get_nowait()
        await monitor.send_reply(message, sender, text)


def run(coro):
    return asyncio.run(coro)


def test_buyer_gets_notification_and_dm(tmp_path):
    monitor, client = make_monitor(tmp_path)
    ivan = User(id=1, first_name="Иван", username="ivan")

    async def scenario():
        await monitor.handle(make_message(10, "Куплю iPhone Duo", ivan))
        await drain(monitor)

    run(scenario())
    assert client.sent[0][0] == "me"
    assert "ХОЧЕТ КУПИТЬ" in client.sent[0][1]
    assert "https://t.me/baraholka/10" in client.sent[0][1]
    assert client.sent[1] == (ivan, "Привет, Иван! Продаю.")


def test_seller_gets_seller_template(tmp_path):
    monitor, client = make_monitor(tmp_path)
    olga = User(id=2, first_name="Ольга")

    async def scenario():
        await monitor.handle(make_message(11, "Продам айфон дуо, торг", olga))
        await drain(monitor)

    run(scenario())
    assert client.sent[1] == (olga, "Привет, Ольга! Куплю.")


def test_same_person_is_contacted_once_across_chats(tmp_path):
    monitor, client = make_monitor(tmp_path)
    ivan = User(id=1, first_name="Иван")

    async def scenario():
        await monitor.handle(make_message(10, "Куплю iPhone Duo", ivan, chat_id=-1))
        await monitor.handle(make_message(77, "Куплю iPhone Duo", ivan, chat_id=-2))
        await monitor.handle(make_message(10, "Куплю iPhone Duo", ivan, chat_id=-1))  # повтор
        await drain(monitor)

    run(scenario())
    notifications = [m for m in client.sent if m[0] == "me"]
    replies = [m for m in client.sent if m[0] is ivan]
    assert len(notifications) == 2
    assert len(replies) == 1


def test_dry_run_sends_nothing_to_people(tmp_path):
    monitor, client = make_monitor(tmp_path, dry_run=True)
    ivan = User(id=1, first_name="Иван")

    async def scenario():
        await monitor.handle(make_message(10, "Куплю iPhone Duo", ivan))
        await drain(monitor)

    run(scenario())
    assert [m[0] for m in client.sent] == ["me"]
    assert "ПРОБНЫЙ РЕЖИМ" in client.sent[0][1]
    assert not monitor.store.contacted_since(1, 0)


def test_irrelevant_own_and_unclear_messages(tmp_path):
    monitor, client = make_monitor(tmp_path)
    ivan = User(id=1, first_name="Иван")

    async def scenario():
        await monitor.handle(make_message(1, "Продам iPhone 15 Pro", ivan))
        await monitor.handle(make_message(2, "Куплю iPhone Duo", ivan, out=True))
        await monitor.handle(make_message(3, "iPhone Duo 256 чёрный", ivan))
        await drain(monitor)

    run(scenario())
    assert len(client.sent) == 1                      # только уведомление о третьем
    assert "не отвечаю" in client.sent[0][1]
