"""Telegram-бот: управление, уведомления и подтверждения пользователя (раздел 23).

Запуск: TELEGRAM_BOT_TOKEN=… TELEGRAM_ALLOWED_CHAT_ID=… python -m arbitrage telegram
Бот отвечает только разрешённому чату. Переговоры начинаются только после
дословной команды пользователя; покупок и оплат бот не выполняет.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import requests

from .analyzer import fmt_rub
from .config import Settings
from .models import UserStatus
from .negotiation import Negotiator, NotAuthorized, is_authorization_phrase
from .pipeline import export_workbook
from .storage import Storage

log = logging.getLogger(__name__)

HELP = """Команды:
/top [N] — лучшие возможности
/show ID — подробности по объявлению
/approve ID… · /watch ID… · /reject ID… · /review ID… — ваше решение
/prepare — подготовить черновики первых сообщений (после разрешения)
/sent ID — подтвердить, что вы отправили сообщение продавцу
/reply ID текст — ответ продавца → следующий шаг
/export — пересобрать таблицу
Чтобы разрешить переговоры по одобренным объявлениям, отправьте дословно:
«Давай, начинай переговоры по одобренным объявлениям»"""

STATUS_COMMANDS = {"/approve": UserStatus.APPROVED, "/watch": UserStatus.WATCHLIST,
                   "/reject": UserStatus.REJECTED, "/review": UserStatus.NEEDS_REVIEW}


class BotLogic:
    """Обработка команд без сетевого кода — тестируется отдельно."""

    def __init__(self, storage: Storage, settings: Settings, workbook_path: str | Path):
        self.storage = storage
        self.settings = settings
        self.workbook_path = workbook_path
        self.negotiator = Negotiator(storage, settings)

    def handle(self, text: str) -> str:
        text = (text or "").strip()
        if is_authorization_phrase(text):
            ids = self.negotiator.authorize(text)
            return (f"Разрешение принято для {len(ids)} одобренных объявлений. "
                    f"Отправьте /prepare, чтобы получить черновики сообщений.") if ids else \
                "Нет новых одобренных объявлений (статус APPROVED)."
        cmd, _, rest = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        args = rest.split()
        try:
            if cmd in ("/start", "/help"):
                return HELP
            if cmd == "/top":
                return self._top(int(args[0]) if args else 10)
            if cmd == "/show" and args:
                return self._show(args[0])
            if cmd in STATUS_COMMANDS and args:
                for lid in args:
                    self.storage.set_user_status(lid, STATUS_COMMANDS[cmd], "telegram")
                return f"Статус {STATUS_COMMANDS[cmd].value}: {', '.join(args)}"
            if cmd == "/prepare":
                return self._prepare()
            if cmd == "/sent" and args:
                return "\n".join(f"{lid}: {'отправка подтверждена' if self.negotiator.mark_sent(lid) else 'нет черновика'}"
                                 for lid in args)
            if cmd == "/reply" and len(args) >= 2:
                lid, reply = args[0], rest.split(maxsplit=1)[1]
                out = self.negotiator.process_reply(lid, reply)
                msg = f"{lid}: {out.status.value}\nСледующий шаг: {out.next_step}"
                if out.new_facts:
                    msg += "\nНовые сведения: " + "; ".join(out.new_facts)
                if out.draft:
                    msg += f"\n\nЧерновик ответа (отправьте сами):\n{out.draft}"
                return msg
            if cmd == "/export":
                return f"Таблица обновлена: {export_workbook(self.storage, self.settings, self.workbook_path)}"
        except (NotAuthorized, KeyError, ValueError) as exc:
            return f"Отказано: {exc}"
        return "Не понял команду.\n\n" + HELP

    def _top(self, n: int) -> str:
        analyses = [a for a in self.storage.latest_analyses().values()
                    if a.best and not a.critical and not a.insufficient_data]
        analyses.sort(key=lambda a: a.rating, reverse=True)
        if not analyses:
            return "Пока нет проанализированных объявлений."
        return "\n".join(f"[{a.rating}] {a.listing_id} {a.brand} {a.model}: {fmt_rub(a.asking_price)} → "
                         f"конс. прибыль {fmt_rub(a.best.scenarios['conservative'].profit)}, риск {a.risk}. "
                         f"{a.recommendation}" for a in analyses[:n])

    def _show(self, lid: str) -> str:
        a = self.storage.latest_analysis(lid)
        listing = self.storage.get_listing(lid)
        if not a or not listing:
            return "Не найдено."
        lines = [f"{listing.title} — {fmt_rub(listing.price)}", listing.url,
                 f"Рейтинг {a.rating}, риск {a.risk}, ликвидность {a.liquidity}",
                 f"Рынок (медиана исправных): {fmt_rub(a.market.working_median)}, аналогов {a.market.comps_count}",
                 f"Рекомендация: {a.recommendation}"]
        if a.best:
            lines.append(f"Макс. цена покупки: {fmt_rub(a.best.max_purchase)}; старт/цель: "
                         f"{fmt_rub(a.start_price)} / {fmt_rub(a.target_price)}")
        lines += [f"• {r}" for r in a.risks[:4]]
        return "\n".join(lines)

    def _prepare(self) -> str:
        out = []
        for lid, state in self.storage.negotiations().items():
            if not state.get("authorized_at"):
                continue
            try:
                _, draft, note = self.negotiator.prepare(lid)
            except NotAuthorized as exc:
                out.append(f"{lid}: {exc}")
                continue
            out.append(f"— {lid}: {note}" + (f"\n{draft}" if draft else ""))
        return "\n\n".join(out) or "Нет разрешённых переговоров. Сначала одобрите объявления и дайте команду."


def run_bot(storage: Storage, settings: Settings, workbook_path: str | Path) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    allowed = os.environ.get("TELEGRAM_ALLOWED_CHAT_ID")
    if not token or not allowed:
        raise SystemExit("Нужны TELEGRAM_BOT_TOKEN и TELEGRAM_ALLOWED_CHAT_ID")
    api = f"https://api.telegram.org/bot{token}"
    logic = BotLogic(storage, settings, workbook_path)
    offset = 0
    while True:
        try:
            resp = requests.get(f"{api}/getUpdates", params={"timeout": 50, "offset": offset}, timeout=60)
            resp.raise_for_status()
            for upd in resp.json().get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                if chat_id != str(allowed):
                    log.warning("Сообщение из неразрешённого чата %s проигнорировано", chat_id)
                    continue
                reply = logic.handle(msg.get("text", ""))
                for chunk in [reply[i:i + 4000] for i in range(0, len(reply), 4000)] or ["—"]:
                    requests.post(f"{api}/sendMessage", json={"chat_id": chat_id, "text": chunk}, timeout=30)
        except requests.RequestException as exc:
            log.warning("Ошибка Telegram API: %s; повтор через 10 с", exc)
            time.sleep(10)
