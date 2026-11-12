"""Пакет «Проверка графа».

Два слоя. Первый — код в этом файле: мгновенно и бесплатно находит то, что видно без модели,
и оставляет в графе карточки «замечание». Второй — скилл для агента: противоречия, дубли по
смыслу, расплывчатые формулировки и пробелы; по каждой находке агент задаёт вопрос.

После первой полной проверки проверяются только карточки, изменённые с прошлого раза, и их соседи.
"""

from __future__ import annotations

from pathlib import Path

from ideagraph_core import DETAIL, PROPOSED, ROOT_KIND, views

__version__ = "0.1.0"

REMARK = "замечание"
OPEN_QUESTION = "открытый вопрос"
ABOUT = "о карточке"
BLOCKING = "мешает"
NOTE = "заметка"
HEAVY_ASSUMPTION = 3            # допущение, от которого зависит столько карточек, — повод для заметки
SERVICE_KINDS = (REMARK, OPEN_QUESTION, ROOT_KIND)


def _title(store, card_id: str) -> str:
    return f"«{store.cards[card_id]['title']}»"


def code_findings(store) -> list[dict]:
    """Находки, которые видны без модели. У каждой есть устойчивый ключ ``key``."""
    cards = store.cards
    work = {cid: c for cid, c in cards.items() if c["kind"] != REMARK}
    found: list[dict] = []

    for card_id in views.unreachable(store):
        if card_id in work:
            found.append({
                "key": f"orphan:{card_id}", "code": "orphan", "level": BLOCKING, "cards": [card_id],
                "title": f"Карточка не связана с идеей: {cards[card_id]['title']}"[:200],
                "body": "От этой карточки нет ни одной цепочки связей до корня. Привяжите её к ветке или удалите.",
            })

    for cycle in views.detail_cycles(store):
        members = sorted(set(cycle), key=lambda c: int(c[1:]))
        found.append({
            "key": "cycle:" + ",".join(members), "code": "cycle", "level": BLOCKING, "cards": members,
            "title": "Круг в связях «детализирует»",
            "body": "Карточки детализируют друг друга по кругу: " + ", ".join(_title(store, c) for c in members)
                    + ". Уберите одну из связей.",
        })

    groups: dict[str, list[str]] = {}
    for card_id, card in work.items():
        if card["kind"] != ROOT_KIND:
            groups.setdefault(" ".join(card["title"].lower().split()), []).append(card_id)
    for title, members in groups.items():
        if len(members) > 1:
            members.sort(key=lambda c: int(c[1:]))
            found.append({
                "key": "same-title:" + ",".join(members), "code": "same-title", "level": NOTE, "cards": members,
                "title": f"Одинаковый заголовок у {len(members)} карточек: {cards[members[0]]['title']}"[:200],
                "body": "Возможно, это одно и то же. Оставьте одну карточку или уточните заголовки.",
            })

    proposed = [cid for cid, c in work.items() if c["status"] == PROPOSED and c["kind"] not in SERVICE_KINDS]
    if proposed:
        found.append({
            "key": "proposed", "code": "proposed", "level": NOTE, "cards": proposed[:12],
            "title": f"Не приняты и не удалены предложенные карточки: {len(proposed)}",
            "body": "Агент предложил эти карточки, но решения по ним нет. Примите их или удалите.",
        })

    has_children = {edge["to"] for edge in store.edges.values() if edge["kind"] == DETAIL}
    empty = [cid for cid, c in work.items() if not (c.get("body") or "").strip() and cid not in has_children
             and c["kind"] not in (*SERVICE_KINDS, "ветка")]
    if empty:
        found.append({
            "key": "empty", "code": "empty", "level": NOTE, "cards": empty[:12],
            "title": f"Карточки без текста и без потомков: {len(empty)}",
            "body": "По одному заголовку трудно понять, что именно решено. Допишите текст или удалите.",
        })

    for card_id, card in work.items():
        decision = card.get("decision")
        if isinstance(decision, dict) and decision.get("assumption"):
            count = len(views.dependents(store, card_id))
            if count >= HEAVY_ASSUMPTION:
                found.append({
                    "key": f"assumption:{card_id}", "code": "assumption", "level": NOTE, "cards": [card_id],
                    "title": f"На допущении держится много карточек: {card['title']}"[:200],
                    "body": f"Это решение принято без фактов, а от него зависит карточек: {count}. "
                            "Подтвердите его сами или проверьте пакетом исследования.",
                })
    return found


def sync_remarks(ctx) -> dict:
    """Привести замечания, сделанные кодом, в соответствие с графом: новые добавить, решённые снять."""
    store = ctx.store
    findings = {item["key"]: item for item in code_findings(store)}
    existing = {c["attrs"]["check.key"]: c for c in store.cards.values()
                if c["kind"] == REMARK and (c.get("attrs") or {}).get("check.key")}
    ops = []
    for key, card in existing.items():
        if key not in findings:
            ops.append({"op": "delete_card", "id": card["id"]})
    added = 0
    for key, item in findings.items():
        if key in existing:
            continue
        added += 1
        ops.append({
            "op": "add_card", "kind": REMARK, "title": item["title"], "body": item["body"], "status": PROPOSED,
            "attrs": {"check.level": item["level"], "check.code": item["code"], "check.key": key},
            "links": [{"to": card_id, "kind": ABOUT} for card_id in item["cards"]],
        })
    if ops:
        store.apply(ops, ctx.author, note="проверка кодом")
    return {"added": added, "removed": len(ops) - added}


def remarks(store) -> list[dict]:
    return [c for c in store.cards.values() if c["kind"] == REMARK]


def scope(ctx) -> tuple[str, list[str]]:
    """Что проверять моделью: всё (первая проверка) или изменённое с прошлой проверки и соседей."""
    store = ctx.store
    last = ctx.state.get("last_revision")
    work = [cid for cid, c in store.cards.items() if c["kind"] != REMARK]
    if not isinstance(last, int):
        return "full", work
    changed: set[str] = set()
    for item in store.changes_since(last, exclude_author=ctx.author):
        if item["type"] == "card":
            changed.add(item["id"])
        else:
            for state in (item["before"], item["after"]):
                if state:
                    changed.update((state["from"], state["to"]))
    changed = {cid for cid in changed if cid in store.cards and store.cards[cid]["kind"] != REMARK}
    near = set(changed)
    for card_id in changed:
        near.update(n["card"]["id"] for n in views.neighbours(store, card_id) if n["card"]["kind"] != REMARK)
    return "changed", sorted(near, key=lambda c: int(c[1:]))


def prepare(ctx) -> str:
    """Код перед сессией агента: проверка без модели и вводные для смысловой проверки."""
    synced = sync_remarks(ctx)
    store = ctx.store
    mode, cards = scope(ctx)
    current = remarks(store)
    blocking = sum(1 for c in current if (c.get("attrs") or {}).get("check.level") == BLOCKING)
    lines = [
        f"Проверка кодом выполнена: добавлено замечаний — {synced['added']}, снято решённых — {synced['removed']}.",
        f"Сейчас замечаний в графе: {len(current)} (мешают идти дальше: {blocking}).",
    ]
    if mode == "full":
        lines.append("Режим: ПОЛНАЯ смысловая проверка — проверь каждую ветку отдельно, затем ветки между собой.")
    elif cards:
        lines.append("Режим: проверка ИЗМЕНЁННОГО с прошлой проверки. Проверь только эти карточки и их связи с соседями:")
        lines.extend("  " + views.card_line(store.cards[card_id]) for card_id in cards[:60])
    else:
        lines.append("Режим: с прошлой проверки ничего не изменилось. Новых замечаний не ищи: разбери оставшиеся.")
    return "\n".join(lines)


def conclude(ctx, outcome: str) -> str:
    """Итог считает код, а не модель: мешающие замечания или открытые вопросы — значит, не чисто."""
    store = ctx.store
    blocking = any((c.get("attrs") or {}).get("check.level") == BLOCKING for c in remarks(store))
    questions = any(c["kind"] == OPEN_QUESTION for c in store.cards.values())
    ctx.state["last_revision"] = store.revision
    ctx.save_state()
    return "есть_мешающие" if blocking or questions else "чисто"


def manifest() -> dict:
    return {
        "id": "check",
        "name": "Проверка графа",
        "description": "Ищет противоречия, дубли, расплывчатые формулировки и пробелы. По каждой находке "
                       "задаёт вопрос; простое исправляет сам, сложное оставляет открытым вопросом.",
        "kind": "agent",
        "order": 40,
        "reads": ["идея", "ветка", "решение", "граница", "открытый вопрос", "требование", "факт"],
        "writes": [REMARK, OPEN_QUESTION],
        "asks": True,
        "web_search": False,
        "model_level": "сильная",
        "outcomes": ["чисто", "есть_мешающие"],
        "fresh_session": True,          # каждая проверка — новая сессия: старый разговор ей не нужен
        "skill": (Path(__file__).parent / "skill.md").read_text(encoding="utf-8"),
        "prepare": prepare,
        "conclude": conclude,
    }
