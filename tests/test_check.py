import pytest

from ideagraph_check import (ABOUT, BLOCKING, NOTE, REMARK, code_findings, conclude, manifest, prepare, scope,
                             sync_remarks)
from ideagraph_core import DETAIL, HUMAN, Store


class Ctx:
    """То же, что даёт оболочка: граф, автор и словарь состояния пакета."""

    def __init__(self, store):
        self.store = store
        self.author = "package:check"
        self.state = {}
        self.saved = 0

    def save_state(self):
        self.saved += 1


@pytest.fixture
def store(tmp_path):
    store = Store.create(tmp_path, "Запись к врачу без звонков")
    store.apply([
        {"op": "add_card", "kind": "ветка", "title": "Функции", "parent": "c1", "ref": "f"},
        {"op": "add_card", "kind": "решение", "title": "Онлайн-запись", "body": "Пациент видит окна", "parent": "@f"},
    ], HUMAN)
    return store


def codes(store):
    return sorted(item["code"] for item in code_findings(store))


def test_clean_graph_has_no_findings(store):
    assert code_findings(store) == []


def test_code_findings(store):
    store.apply([
        {"op": "add_card", "kind": "решение", "title": "Сирота", "body": "x"},                       # c4
        {"op": "add_card", "kind": "решение", "title": "онлайн-запись", "body": "y", "parent": "c2"},  # c5 дубль
        {"op": "add_card", "kind": "решение", "title": "Без текста", "parent": "c2"},                 # c6
    ], HUMAN)
    store.apply([{"op": "add_card", "kind": "решение", "title": "Предложение", "body": "z", "parent": "c2"}],
                "package:clarify")                                                                 # c7
    store.apply([{"op": "add_edge", "from": "c2", "to": "c3", "kind": DETAIL}], HUMAN)                 # круг
    assert codes(store) == ["cycle", "empty", "orphan", "proposed", "same-title"]
    by_code = {item["code"]: item for item in code_findings(store)}
    assert by_code["orphan"]["level"] == BLOCKING and by_code["orphan"]["cards"] == ["c4"]
    assert by_code["same-title"]["cards"] == ["c3", "c5"] and by_code["proposed"]["level"] == NOTE


def test_heavy_assumption(store):
    store.apply([{"op": "update_card", "id": "c2", "decision": {"assumption": True, "by": "decider"}}], HUMAN)
    assert codes(store) == []                       # зависит только одна карточка
    store.apply([{"op": "add_card", "kind": "решение", "title": f"Д{i}", "body": "t", "parent": "c2"} for i in range(2)], HUMAN)
    assert codes(store) == ["assumption"]


def test_sync_adds_and_removes_remarks(store):
    ctx = Ctx(store)
    store.apply([{"op": "add_card", "kind": "решение", "title": "Сирота", "body": "x"}], HUMAN)
    assert sync_remarks(ctx) == {"added": 1, "removed": 0}
    remark = next(c for c in store.cards.values() if c["kind"] == REMARK)
    assert remark["attrs"]["check.level"] == BLOCKING and remark["author"] == "package:check"
    assert [e["kind"] for e in store.edges.values() if e["from"] == remark["id"]] == [ABOUT]
    assert sync_remarks(ctx) == {"added": 0, "removed": 0}          # повторно не дублирует
    assert conclude(ctx, "чисто") == "есть_мешающие"
    store.apply([{"op": "add_edge", "from": "c4", "to": "c2", "kind": DETAIL}], HUMAN)
    assert sync_remarks(ctx) == {"added": 0, "removed": 1}          # человек исправил — замечание снято
    assert conclude(ctx, "есть_мешающие") == "чисто" and ctx.saved == 2


def test_open_question_blocks(store):
    ctx = Ctx(store)
    store.apply([{"op": "add_card", "kind": "открытый вопрос", "title": "Что при отказе платежа?", "parent": "c2"}],
                "package:check")
    assert conclude(ctx, "чисто") == "есть_мешающие"


def test_scope_is_incremental(store):
    ctx = Ctx(store)
    assert scope(ctx) == ("full", ["c1", "c2", "c3"])
    assert "ПОЛНАЯ" in prepare(ctx)
    conclude(ctx, "чисто")
    assert scope(ctx) == ("changed", []) and "ничего не изменилось" in prepare(ctx)
    store.apply([
        {"op": "add_card", "kind": "ветка", "title": "Деньги", "parent": "c1", "ref": "m"},
        {"op": "add_card", "kind": "решение", "title": "Оплата по СБП", "body": "t", "parent": "@m"},
        {"op": "add_card", "kind": "решение", "title": "Далёкая", "body": "t", "parent": "c3"},
    ], HUMAN)
    store.apply([{"op": "delete_card", "id": "c6"}], HUMAN)
    mode, cards = scope(ctx)
    assert mode == "changed" and cards == ["c1", "c2", "c4", "c5"]      # изменённые и их соседи; созданное и тут же удалённое не в счёт
    text = prepare(ctx)
    assert "ИЗМЕНЁННОГО" in text and "Оплата по СБП" in text and "Функции" in text and "Далёкая" not in text


def test_manifest():
    m = manifest()
    assert m["outcomes"] == ["чисто", "есть_мешающие"] and m["fresh_session"] is True
    assert callable(m["prepare"]) and callable(m["conclude"]) and "о карточке" in m["skill"]
