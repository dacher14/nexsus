"""Итоговый отчёт кампании и переговоров (раздел 22).

Потенциальная прибыль, согласованные цены и фактическая прибыль не смешиваются.
"""
from __future__ import annotations

from collections import Counter

from .analyzer import fmt_rub
from .models import Analysis, NegotiationStatus as NS
from .storage import Storage


def campaign_report(analyses: list[Analysis], stats: dict, workbook: str | None, settings,
                    has_synthetic: bool, top_n: int = 10, statuses: dict[str, str] | None = None) -> str:
    rejected = {lid for lid, st in (statuses or {}).items() if st == "REJECTED"}
    n = len(analyses)
    profitable = [a for a in analyses if a.best and not a.critical and a.best.max_purchase
                  and a.asking_price <= a.best.max_purchase]
    lines = []
    if has_synthetic:
        lines.append("⚠ ТЕСТОВЫЙ ПРОГОН: объявления синтетические, результаты не являются данными рынка.\n")
    lines += [
        "ИТОГИ ИССЛЕДОВАТЕЛЬСКОЙ КАМПАНИИ",
        f"Обнаружено объявлений: {stats.get('found', n)} (отклонено при чтении: {stats.get('invalid', 0)})",
        f"Уникальных после удаления дубликатов: {stats.get('unique', n)} "
        f"(точных дубликатов: {stats.get('exact_duplicates', 0)}, повторных публикаций: {stats.get('reposts', 0)})",
        f"В анализе (все активные в базе): {n}",
        f"Прошли первичную фильтрацию: {sum(1 for a in analyses if not a.critical and not a.insufficient_data)}",
        f"Потенциально выгодных при текущей цене: {len(profitable)}; выгодны после торга: "
        f"{sum(1 for a in analyses if a.action == 'Торговаться')}",
        f"Требуют диагностики: {sum(1 for a in analyses if a.defects.defect_class in ('B', 'C') or a.defects.is_untested)}",
        f"Лучшая стратегия — DIY: {sum(1 for a in analyses if a.best_strategy == 'diy')}, "
        f"сервис: {sum(1 for a in analyses if a.best_strategy == 'service')}, "
        f"на запчасти/как есть: {sum(1 for a in analyses if a.best_strategy == 'as_is')}",
        f"Высокий риск (≥70): {sum(1 for a in analyses if a.risk >= 70)}, "
        f"критические риски: {sum(1 for a in analyses if a.critical)}",
        f"Недостаточно данных: {sum(1 for a in analyses if a.insufficient_data)} "
        f"({round(100 * sum(1 for a in analyses if a.insufficient_data) / max(n, 1))}%)",
    ]
    if n < settings.campaign_target_min:
        lines.append(f"Объём ниже целевого ({settings.campaign_target_min}–{settings.campaign_target_max}): "
                     f"собрано {n}. Объявления не дополнялись выдуманными.")
    by_cat = Counter(a.category_name for a in profitable)
    if by_cat:
        lines.append("Категории с лучшими результатами: " + ", ".join(f"{c} ({k})" for c, k in by_cat.most_common(5)))
    top = sorted((a for a in analyses if a.best and not a.critical and not a.insufficient_data
                  and a.listing_id not in rejected),
                 key=lambda a: a.rating, reverse=True)[:top_n]
    if top:
        lines.append("\nЛУЧШИЕ ВОЗМОЖНОСТИ (прогноз, не факт):")
        for a in top:
            cons = a.best.scenarios["conservative"]
            lines.append(f"  [{a.rating:>3}] {a.listing_id} {a.brand} {a.model} — цена {fmt_rub(a.asking_price)}, "
                         f"макс. {fmt_rub(a.best.max_purchase)}, конс. прибыль {fmt_rub(cons.profit)}, "
                         f"риск {a.risk}: {a.recommendation}")
    review = [a for a in analyses if a.action.startswith("Проверить продавца") or a.action == "Дособрать данные"]
    if review:
        lines.append(f"\nТребуют дополнительной проверки: {len(review)} (подозрительная цена или нет аналогов)")
    if workbook:
        lines.append(f"\nТаблица: {workbook}")
    lines += [
        "\nДАЛЬШЕ:",
        "  1. Проверьте таблицу и проставьте статусы (APPROVED / WATCHLIST / REJECTED / NEEDS REVIEW)",
        "     в колонке «Статус пользователя», затем: python -m arbitrage import-statuses <файл.xlsx>",
        "  2. Переговоры начнутся только после команды: "
        "python -m arbitrage negotiate authorize \"Давай, начинай переговоры по одобренным объявлениям\"",
    ]
    return "\n".join(lines)


def negotiation_report(storage: Storage) -> str:
    states = storage.negotiations()
    if not states:
        return "Переговоры не велись."
    msgs = {lid: storage.messages(lid) for lid in states}
    drafted = [lid for lid, m in msgs.items() if any(x["direction"] == "out" for x in m)]
    contacted = [lid for lid, m in msgs.items() if any(x["direction"] == "out" and x["sent"] for x in m)]
    replied = [lid for lid, m in msgs.items() if any(x["direction"] == "in" for x in m)]
    status = Counter(s["status"] for s in states.values())
    within = [lid for lid, s in states.items()
              if s.get("seller_price") and s.get("target_price") and s["seller_price"] <= s["target_price"]]
    agreed = [(lid, s) for lid, s in states.items() if s["status"] == NS.AGREED.value]
    lines = [
        "ИТОГИ ПЕРЕГОВОРОВ",
        f"Разрешено переговоров: {len(states)}; с подготовленными сообщениями: {len(drafted)}; "
        f"отправлено пользователем: {len(contacted)}",
        f"Ответили: {len(replied)}",
        f"Встречная цена: {status.get(NS.COUNTER_OFFER.value, 0)}; отложено: {status.get(NS.DEFERRED.value, 0)}; "
        f"закрыто: {status.get(NS.CLOSED.value, 0)}",
        f"В пределах целевой цены: {len(within)}",
        f"Цена согласована: {len(agreed)} — требуют проверки товара и отдельного подтверждения покупки:",
    ]
    lines += [f"  {lid}: {fmt_rub(s.get('agreed_price'))}" for lid, s in agreed]
    deals = storage.deals()
    sold = [d for d in deals if d["status"] == "sold"]
    if deals:
        lines.append(f"\nСделки: открытых {sum(1 for d in deals if d['status'] in ('bought', 'repairing', 'listed'))}, "
                     f"завершённых {len(sold)}; фактическая прибыль: "
                     f"{fmt_rub(sum(d['data'].get('net_profit', 0) for d in sold))}")
    return "\n".join(lines)
