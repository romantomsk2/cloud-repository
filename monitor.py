#!/usr/bin/env python3
"""Мониторинг барахолок в Telegram: ищет объявления о покупке/продаже нужного
товара (по умолчанию iPhone Duo), уведомляет в «Избранное» и отвечает авторам.

Запуск:
    python monitor.py               # мониторинг
    python monitor.py --list-chats  # показать чаты и какие из них будут отслеживаться
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, PeerFloodError, RPCError
from telethon.tl.types import Channel, User

from matcher import BUY, SELL, Match, Matcher

log = logging.getLogger("monitor")

BASE_DIR = Path(__file__).resolve().parent

DEFAULTS: dict[str, Any] = {
    "session": "monitor",
    "chats": [],
    "auto_chat_keywords": [
        "барахолк", "куплю", "продам", "купи", "прода", "объявлен", "авито", "б/у", "бу ",
    ],
    "include_channels": False,
    "product_patterns": [],
    "exclude_patterns": [],
    "backfill_minutes": 60,
    "backfill_limit": 300,
    "notify_chat": "me",
    "dry_run": True,
    "reply": {
        "mode": "dm",                 # dm — в личку, reply — ответом в чате, off — не отвечать
        "to_buyer": "",
        "to_seller": "",
        "delay_seconds": [20, 90],
        "max_per_hour": 10,
        "cooldown_days": 7,
    },
}

INTENT_LABEL = {BUY: "🛒 ХОЧЕТ КУПИТЬ", SELL: "💰 ПРОДАЁТ"}


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        sys.exit(f"Не найден {path}. Скопируйте config.example.yaml в config.yaml и заполните его.")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = {**DEFAULTS, **raw}
    cfg["reply"] = {**DEFAULTS["reply"], **(raw.get("reply") or {})}
    if not cfg.get("api_id") or not cfg.get("api_hash"):
        sys.exit("В config.yaml нужно указать api_id и api_hash (берутся на https://my.telegram.org).")
    if cfg["reply"]["mode"] not in ("dm", "reply", "off"):
        sys.exit("reply.mode должен быть dm, reply или off.")
    return cfg


class Store:
    """Память между запусками: какие сообщения уже разобраны и кому уже писали."""

    def __init__(self, path: Path | str) -> None:
        self.db = sqlite3.connect(str(path))
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen (
                chat_id INTEGER, msg_id INTEGER, PRIMARY KEY (chat_id, msg_id));
            CREATE TABLE IF NOT EXISTS sent (
                user_id INTEGER, ts REAL);
            CREATE INDEX IF NOT EXISTS sent_user ON sent (user_id, ts);
            """
        )

    def mark_seen(self, chat_id: int, msg_id: int) -> bool:
        """True, если сообщение встретилось впервые."""
        cur = self.db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (chat_id, msg_id))
        self.db.commit()
        return cur.rowcount == 1

    def contacted_since(self, user_id: int, since: float) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM sent WHERE user_id = ? AND ts >= ? LIMIT 1", (user_id, since)
        ).fetchone()
        return row is not None

    def sent_count_since(self, since: float) -> int:
        return self.db.execute("SELECT COUNT(*) FROM sent WHERE ts >= ?", (since,)).fetchone()[0]

    def record_sent(self, user_id: int) -> None:
        self.db.execute("INSERT INTO sent VALUES (?, ?)", (user_id, time.time()))
        self.db.commit()


def message_link(chat: Any, msg_id: int) -> str:
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{msg_id}"
    if isinstance(chat, Channel):
        return f"https://t.me/c/{chat.id}/{msg_id}"
    return ""


def display_name(sender: Any) -> str:
    if isinstance(sender, User):
        name = " ".join(p for p in (sender.first_name, sender.last_name) if p) or "без имени"
        return f"{name} (@{sender.username})" if sender.username else f"{name} (id {sender.id})"
    return getattr(sender, "title", None) or "неизвестно"


class Monitor:
    def __init__(self, client: Any, cfg: dict[str, Any], store: Store, matcher: Matcher) -> None:
        self.client = client
        self.cfg = cfg
        self.reply_cfg = cfg["reply"]
        self.store = store
        self.matcher = matcher
        self.queue: asyncio.Queue = asyncio.Queue()
        self.replies_enabled = self.reply_cfg["mode"] != "off"
        self.dry_run_shown: set[int] = set()

    # ---------- разбор входящих ----------

    async def on_new_message(self, event: Any) -> None:
        try:
            await self.handle(event.message)
        except Exception:
            log.exception("Ошибка при обработке сообщения")

    async def handle(self, message: Any) -> None:
        if message.out:
            return
        match = self.matcher.match(message.raw_text)
        if not match:
            return
        if not self.store.mark_seen(message.chat_id, message.id):
            return

        chat = await message.get_chat()
        sender = await message.get_sender()
        plan = self.plan_reply(match, sender)
        log.info(
            "Найдено [%s] в «%s» от %s: %s",
            match.intent, getattr(chat, "title", "?"), display_name(sender),
            message.raw_text[:120].replace("\n", " "),
        )
        await self.notify(message, chat, sender, match, plan["note"])
        if plan["text"]:
            await self.queue.put((message, sender, plan["text"]))

    def plan_reply(self, match: Match, sender: Any) -> dict[str, str]:
        """Решает, отвечать ли автору, и объясняет решение для уведомления."""
        if not self.replies_enabled:
            return {"text": "", "note": "автоответ выключен"}
        template = {BUY: self.reply_cfg["to_buyer"], SELL: self.reply_cfg["to_seller"]}.get(match.intent, "")
        if not template or not template.strip():
            reason = "непонятно, покупка это или продажа" if match.intent not in (BUY, SELL) else "нет шаблона ответа"
            return {"text": "", "note": f"не отвечаю: {reason}"}
        if not isinstance(sender, User) or sender.bot:
            return {"text": "", "note": "не отвечаю: автор — не обычный пользователь"}
        cooldown = time.time() - self.reply_cfg["cooldown_days"] * 86400
        if self.store.contacted_since(sender.id, cooldown):
            return {"text": "", "note": "не отвечаю: этому человеку уже писали"}
        text = template.strip().replace("{name}", sender.first_name or "")
        prefix = "ПРОБНЫЙ РЕЖИМ, отправил бы" if self.cfg["dry_run"] else "отвечу"
        where = "в личку" if self.reply_cfg["mode"] == "dm" else "в чате"
        return {"text": text, "note": f"{prefix} {where}"}

    async def notify(self, message: Any, chat: Any, sender: Any, match: Match, note: str) -> None:
        target = self.cfg["notify_chat"]
        if not target:
            return
        lines = [
            f"{INTENT_LABEL.get(match.intent, '❓ УПОМИНАНИЕ')}: {match.product}",
            f"Чат: {getattr(chat, 'title', '?')}",
            f"Автор: {display_name(sender)}",
        ]
        link = message_link(chat, message.id)
        if link:
            lines.append(link)
        lines += ["", message.raw_text[:1500], "", f"→ {note}"]
        try:
            await self.client.send_message(target, "\n".join(lines), parse_mode=None, link_preview=False)
        except RPCError as e:
            log.warning("Не удалось отправить уведомление: %s", e)

    # ---------- отправка ответов ----------

    async def reply_worker(self) -> None:
        while True:
            message, sender, text = await self.queue.get()
            try:
                await self.send_reply(message, sender, text)
            except Exception:
                log.exception("Ошибка при отправке ответа")
            finally:
                self.queue.task_done()

    async def send_reply(self, message: Any, sender: Any, text: str) -> None:
        if not self.replies_enabled:
            return
        # Один и тот же человек часто постит объявление сразу в несколько чатов.
        cooldown = time.time() - self.reply_cfg["cooldown_days"] * 86400
        if self.store.contacted_since(sender.id, cooldown):
            return
        if self.cfg["dry_run"]:
            # В базу не пишем: после выключения пробного режима этим людям ещё надо ответить.
            if sender.id not in self.dry_run_shown:
                self.dry_run_shown.add(sender.id)
                log.info("ПРОБНЫЙ РЕЖИМ: не отправляю %s: %s", display_name(sender), text)
            return

        while self.store.sent_count_since(time.time() - 3600) >= self.reply_cfg["max_per_hour"]:
            log.info("Достигнут лимит ответов в час, жду")
            await asyncio.sleep(300)
        low, high = self.reply_cfg["delay_seconds"]
        await asyncio.sleep(random.uniform(low, high))

        for attempt in (1, 2):
            try:
                if self.reply_cfg["mode"] == "dm":
                    await self.client.send_message(sender, text, parse_mode=None)
                else:
                    await message.reply(text, parse_mode=None)
                self.store.record_sent(sender.id)
                log.info("Ответ отправлен: %s", display_name(sender))
                return
            except FloodWaitError as e:
                log.warning("Telegram просит подождать %s с", e.seconds)
                if attempt == 2:
                    return
                await asyncio.sleep(e.seconds + 5)
            except PeerFloodError:
                self.replies_enabled = False
                warning = ("⚠️ Telegram ограничил отправку сообщений незнакомым людям. "
                           "Автоответы остановлены, мониторинг продолжается.")
                log.error(warning)
                if self.cfg["notify_chat"]:
                    await self.client.send_message(self.cfg["notify_chat"], warning)
                return
            except RPCError as e:
                log.warning("Не удалось ответить %s: %s", display_name(sender), e)
                return

    # ---------- выбор чатов и запуск ----------

    async def resolve_chats(self) -> dict[int, str]:
        """Возвращает {id чата: название} для отслеживания."""
        result: dict[int, str] = {}
        keywords = [k.lower() for k in self.cfg["auto_chat_keywords"]]
        async for dialog in self.client.iter_dialogs():
            is_broadcast = dialog.is_channel and not dialog.is_group
            if not dialog.is_group and not (is_broadcast and self.cfg["include_channels"]):
                continue
            title = (dialog.name or "").lower() + " "
            if any(k in title for k in keywords):
                result[dialog.id] = dialog.name
        for item in self.cfg["chats"]:
            try:
                entity = await self.client.get_entity(item)
                peer_id = await self.client.get_peer_id(entity)
                result[peer_id] = getattr(entity, "title", str(item))
            except (ValueError, RPCError) as e:
                log.warning("Чат %r не найден (вы в нём состоите?): %s", item, e)
        return result

    async def backfill(self, chat_ids: list[int]) -> None:
        minutes = self.cfg["backfill_minutes"]
        if not minutes:
            return
        since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        for chat_id in chat_ids:
            try:
                async for message in self.client.iter_messages(
                    chat_id, offset_date=since, reverse=True, limit=self.cfg["backfill_limit"]
                ):
                    await self.handle(message)
            except RPCError as e:
                log.warning("Не удалось прочитать историю чата %s: %s", chat_id, e)

    async def run(self) -> None:
        chats = await self.resolve_chats()
        if not chats:
            sys.exit("Не нашлось ни одного чата для мониторинга. Запустите с --list-chats "
                     "и впишите нужные чаты в config.yaml → chats.")
        log.info("Отслеживаю чатов: %d", len(chats))
        for title in chats.values():
            log.info("  • %s", title)
        if self.cfg["dry_run"]:
            log.info("ПРОБНЫЙ РЕЖИМ: ответы авторам не отправляются (dry_run: true)")

        self.client.add_event_handler(self.on_new_message, events.NewMessage(chats=list(chats)))
        worker = asyncio.create_task(self.reply_worker())
        await self.backfill(list(chats))
        log.info("Жду новые объявления. Остановить — Ctrl+C")
        try:
            await self.client.run_until_disconnected()
        finally:
            worker.cancel()


async def list_chats(monitor: Monitor) -> None:
    selected = await monitor.resolve_chats()
    print("\n[x] — чат будет отслеживаться\n")
    async for dialog in monitor.client.iter_dialogs():
        if not (dialog.is_group or dialog.is_channel):
            continue
        kind = "группа" if dialog.is_group else "канал "
        mark = "x" if dialog.id in selected else " "
        print(f"[{mark}] {kind}  {dialog.id:>15}  {dialog.name}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(BASE_DIR / "config.yaml"), help="путь к config.yaml")
    parser.add_argument("--list-chats", action="store_true", help="показать чаты и выйти")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("telethon").setLevel(logging.WARNING)

    cfg = load_config(Path(args.config))
    session = BASE_DIR / cfg["session"]
    client = TelegramClient(str(session), int(cfg["api_id"]), str(cfg["api_hash"]))
    await client.start()  # при первом запуске спросит номер телефона и код из Telegram
    monitor = Monitor(
        client, cfg, Store(BASE_DIR / "state.db"),
        Matcher(cfg["product_patterns"] or None, cfg["exclude_patterns"] or None),
    )
    try:
        if args.list_chats:
            await list_chats(monitor)
        else:
            await monitor.run()
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлено.")
