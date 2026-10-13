"""Анализ неисправностей по тексту объявления (раздел 8).

Извлекает симптомы (факты — дословные фразы из объявления), строит гипотезы
о причинах и оценивает ремонт по справочнику repair_kb.yaml. Диагноз по
тексту — всегда гипотеза, а не подтверждённый факт.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .categories import get_category, normalize_text
from .models import DefectAnalysis, RedFlag, RepairItem

KB_PATH = Path(__file__).parent / "data" / "repair_kb.yaml"
CLASS_ORDER = {"A": 1, "B": 2, "C": 3}
SKILL_ORDER = {"basic": 1, "soldering": 2, "expert": 3}
_BATTERY_HEALTH_RE = re.compile(
    r"(?:акб|аккумулятор\w*|батаре\w*|емкость|состояние акб|здоровье акб)\s*[:\-–]?\s*(\d{2,3})\s*%")
_WORD_RE = re.compile(r"[a-zа-я0-9]+")


@dataclass
class _Rule:
    code: str
    label: str
    regex: re.Pattern
    negation_exempt: bool = False
    exclude_before: list[str] = field(default_factory=list)
    exclude_after: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Match:
    code: str
    evidence: str
    meta: dict[str, Any]


class RepairKnowledgeBase:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.negations = set(data.get("negation_words", []))
        self.symptoms = self._rules(data.get("symptoms", {}))
        self.red_flags = self._rules(data.get("red_flags", {}))
        self.signals = self._rules(data.get("signals", {}))
        self.profiles: dict[str, dict] = data.get("profiles", {})
        self.no_diy_groups: dict[str, str] = data.get("no_diy_groups", {})
        self.battery_threshold = int(data.get("battery_health_threshold", 80))

    @staticmethod
    def _rules(section: dict[str, Any]) -> list[_Rule]:
        rules = []
        for code, spec in section.items():
            alternatives = "|".join(re.escape(normalize_text(p)) for p in spec["patterns"])
            rules.append(_Rule(
                code=code,
                label=spec["label"],
                regex=re.compile(r"(?<![a-zа-я0-9])(?:" + alternatives + r")"),
                negation_exempt=bool(spec.get("negation_exempt")),
                exclude_before=spec.get("exclude_before", []),
                exclude_after=spec.get("exclude_after", []),
                meta={k: v for k, v in spec.items() if k not in ("patterns", "label")},
            ))
        return rules

    def _negated(self, text: str, start: int) -> bool:
        before = _WORD_RE.findall(text[max(0, start - 25):start])[-2:]
        return any(w in self.negations for w in before)

    def match(self, rules: list[_Rule], text: str) -> list[_Match]:
        found = []
        for rule in rules:
            for m in rule.regex.finditer(text):
                before = text[max(0, m.start() - 20):m.start()]
                after = text[m.end():m.end() + 20]
                if not rule.negation_exempt and self._negated(text, m.start()):
                    continue
                if any(w in before for w in rule.exclude_before):
                    continue
                if any(w in after for w in rule.exclude_after):
                    continue
                found.append(_Match(rule.code, _snippet(text, m.start(), m.end()), rule.meta))
                break
        return found

    def profile(self, group: str, symptom: str) -> dict[str, Any]:
        base = copy.deepcopy(self.profiles.get("default", {}).get(symptom, {}))
        override = self.profiles.get(group, {}).get(symptom, {})
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                if "parts" in value or "parts_frac" in value:
                    base[key].pop("parts", None)
                    base[key].pop("parts_frac", None)
                base[key].update(value)
            else:
                base[key] = copy.deepcopy(value)
        return base


@lru_cache(maxsize=4)
def _load_kb(path: str) -> RepairKnowledgeBase:
    with open(path, encoding="utf-8") as fh:
        return RepairKnowledgeBase(yaml.safe_load(fh))


def load_kb(path: str | Path | None = None) -> RepairKnowledgeBase:
    return _load_kb(str(path or KB_PATH))


def _snippet(text: str, start: int, end: int, pad: int = 25) -> str:
    left = text.rfind(" ", 0, max(0, start - pad))
    right = text.find(" ", min(len(text), end + pad))
    return text[left + 1 if left >= 0 else 0: right if right >= 0 else len(text)].strip()


def _cost_range(spec: dict[str, Any], market_price: int | None) -> tuple[int, int]:
    if "parts_frac" in spec:
        floor = int(spec.get("parts_min", 0))
        if not market_price:
            return floor, max(floor, floor * 2)
        lo_f, hi_f = spec["parts_frac"]
        lo = max(floor, int(lo_f * market_price))
        return lo, max(lo, int(hi_f * market_price))
    lo, hi = spec.get("parts", (0, 0))
    return int(lo), int(hi)


def _pair(value: Any) -> tuple[int, int]:
    if not value:
        return 0, 0
    return int(value[0]), int(value[1])


def analyze_defects(title: str, description: str, category: str, condition: str = "",
                    market_price: int | None = None, diy_skills: list[str] | None = None,
                    kb: RepairKnowledgeBase | None = None,
                    extra_symptoms: list[tuple[str, str]] = (),
                    extra_flags: list[tuple[str, str]] = ()) -> DefectAnalysis:
    """extra_symptoms/extra_flags — пары (код, обоснование), например от LLM-анализа."""
    kb = kb or load_kb()
    cat = get_category(category)
    group = cat.repair_group
    text = normalize_text(f"{title}. {description}. {condition}")
    skills = diy_skills or ["basic"]
    max_skill = max(SKILL_ORDER.get(s, 1) for s in skills)

    result = DefectAnalysis()
    symptom_matches = kb.match(kb.symptoms, text)
    known = {m.code for m in symptom_matches}
    symptom_meta = {r.code: r.meta for r in kb.symptoms}
    symptom_matches += [_Match(code, evidence, symptom_meta[code]) for code, evidence in extra_symptoms
                        if code in symptom_meta and code not in known]

    health = _BATTERY_HEALTH_RE.search(text)
    if health and int(health.group(1)) < kb.battery_threshold and not any(
            m.code == "battery" for m in symptom_matches):
        symptom_matches.append(_Match("battery", health.group(0), {}))

    cond = normalize_text(condition)
    if any(w in cond for w in ("запчаст", "неисправ", "требует ремонта")) and not any(
            m.code == "parts_listing" for m in symptom_matches):
        symptom_matches.append(_Match("parts_listing", f"состояние: {condition}", {}))

    codes = {m.code for m in symptom_matches}
    result.is_untested = "untested" in codes
    result.is_parts_listing = "parts_listing" in codes
    specific = [m for m in symptom_matches
                if m.code not in ("untested", "parts_listing", "unknown_fault")]
    if specific:
        symptom_matches = specific
    else:
        symptom_matches = [m for m in symptom_matches if m.code == "unknown_fault"]
        if result.is_parts_listing and not symptom_matches:
            symptom_matches = [_Match("unknown_fault", "продаётся на запчасти без описания поломки", {})]

    labels = {r.code: r.label for r in kb.symptoms}
    causes = {m.code for m in symptom_matches} & set(kb.data.get("cause_symptoms", []))
    if causes:
        effects = set(kb.data.get("effect_symptoms", []))
        absorbed = [m for m in symptom_matches if m.code in effects]
        symptom_matches = [m for m in symptom_matches if m.code not in effects]
        for m in symptom_matches:
            if m.code in causes and absorbed:
                m.evidence += " → следствия: " + ", ".join(labels[a.code].lower() for a in absorbed)
                break
    for m in symptom_matches:
        prof = kb.profile(group, m.code)
        if not prof:
            continue
        diy = prof.get("diy", {})
        svc = prof.get("service", {})
        diy_allowed = bool(diy.get("allowed", False))
        diy_note = diy.get("note", "")
        skill = diy.get("skill", "basic")
        if group in kb.no_diy_groups:
            diy_allowed, diy_note = False, kb.no_diy_groups[group]
        elif diy_allowed and SKILL_ORDER.get(skill, 1) > max_skill:
            diy_allowed, diy_note = False, f"Требуется навык: {skill}"
        result.items.append(RepairItem(
            symptom=m.code,
            label=labels.get(m.code, m.code),
            defect_class=prof.get("class", "C"),
            evidence=m.evidence,
            causes=list(prof.get("causes", [])),
            alt_causes=list(prof.get("alt_causes", [])),
            diy_allowed=diy_allowed,
            diy_note=diy_note,
            diy_skill=skill,
            diy_parts=_cost_range(diy, market_price),
            diy_tools=int(diy.get("tools", 0)),
            diy_hours=float(diy.get("hours", 0)),
            diy_success=float(diy.get("success", 0)),
            service_diag=_pair(svc.get("diag")),
            service_parts=_cost_range(svc, market_price),
            service_labor=_pair(svc.get("labor")),
            service_success=float(svc.get("success", 0)),
            service_days=int(svc.get("days", 0)),
            as_is_factor=prof.get("as_is_factor"),
            accessory=bool(kb.data["symptoms"].get(m.code, {}).get("accessory")),
            checks=list(prof.get("checks", [])),
        ))

    flag_matches = kb.match(kb.red_flags, text)
    flag_meta = {r.code: r.meta for r in kb.red_flags}
    flag_matches += [_Match(code, evidence, flag_meta[code]) for code, evidence in extra_flags
                     if code in flag_meta and code not in {m.code for m in flag_matches}]
    for m in flag_matches:
        result.red_flags.append(RedFlag(code=m.code, label=_label(kb.red_flags, m.code),
                                        critical=bool(m.meta.get("critical")),
                                        risk=int(m.meta.get("risk", 0)), evidence=m.evidence))
        if "resale_factor" in m.meta:
            result.resale_factor *= float(m.meta["resale_factor"])
        result.extra_transport += int(m.meta.get("extra_transport", 0))
    result.signals = [m.code for m in kb.match(kb.signals, text)]

    repairs = [i for i in result.items if not i.accessory]
    if repairs:
        result.defect_class = max((i.defect_class for i in repairs), key=CLASS_ORDER.__getitem__)
        if any(w in text for w in ("диагностика показала", "в сервисе сказали", "сервис сказал",
                                   "по диагностике", "мастер сказал")):
            result.diagnosis_confidence = "средняя (со слов продавца о диагностике)"
        elif result.defect_class == "C" or any(i.symptom == "unknown_fault" for i in repairs):
            result.diagnosis_confidence = "низкая (гипотеза по тексту)"
        else:
            result.diagnosis_confidence = "средняя (гипотеза по тексту)"
    elif result.is_untested:
        result.diagnosis_confidence = "нет данных (не проверялся)"

    checks: list[str] = []
    for item in result.items:
        checks.extend(item.checks)
    if result.is_untested:
        checks.append("Проверить работоспособность на месте до оплаты")
    if cat.repair_group == "phone":
        checks.append("Проверить отсутствие блокировки аккаунта (iCloud/Google) и IMEI до оплаты")
    checks.extend(kb.data.get("group_checks", {}).get(cat.repair_group, []))
    result.checks = list(dict.fromkeys(checks))
    return result


def _label(rules: list[_Rule], code: str) -> str:
    return next((r.label for r in rules if r.code == code), code)


def repair_complexity(analysis: DefectAnalysis) -> int:
    """Сложность ремонта 1–10."""
    repairs = [i for i in analysis.items if not i.accessory]
    if not repairs:
        return 1
    base = {"A": 3, "B": 5, "C": 8}[analysis.defect_class or "A"]
    if not any(i.diy_allowed for i in repairs):
        base += 1
    base += min(2, len(repairs) - 1)
    return min(10, base)
