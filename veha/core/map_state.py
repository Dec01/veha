"""Накопленное состояние карты, отделённое от снимка последнего прогона.

Allure-store хранит историю просмотра, а этот модуль — структуру системы. Частичный
прогон обновляет только увиденные тесты и их наблюдения. Полный прогон области
(``map_full`` + ``map_scope``) заменяет только её; соседняя область сохраняется.

Это производное состояние Вехи, а не артефакт partest-atlas: контракт агента не меняется.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Iterable, List

from partest_atlas.artifacts import Scope
from partest_atlas.collect import ui_flow as atlas_ui_flow

from veha import redact
from veha.config import Config, is_link
from veha.core import sources as core_sources

VERSION = 1
OBSERVED_KINDS = ("ui.flow", "db.effects", "bus.flow", "containers.logs")
_ID_FIELDS = ("nodeid", "variant", "table", "topic", "screen", "control", "action",
              "method", "path", "container", "phase", "scope")


def state_path(cfg: Config, scope: Scope) -> Path:
    return cfg.root / ".veha" / "map" / scope.system / scope.env / "state.json"


def _checked_path(root: Path, path: Path) -> tuple[Path, Path, tuple[str, ...]]:
    """Лексическая граница и все существующие родители — до read/mkdir/write.

    ``resolve`` здесь намеренно не первый шаг: он перешёл бы по ссылке наружу до проверки.
    Корень уже должен существовать; создаются только проверенные компоненты под ним.
    """
    root = Path(os.path.abspath(root))
    path = Path(os.path.abspath(path))
    try:
        rel = path.relative_to(root)
    except ValueError:
        raise ValueError("состояние карты: путь вне корня проекта") from None
    if not rel.parts or ".." in rel.parts:
        raise ValueError("состояние карты: путь вне корня проекта")
    if is_link(root) or not root.is_dir():
        raise ValueError("состояние карты: корень проекта не обычный каталог")
    current = root
    for part in rel.parts[:-1]:
        current = current / part
        if is_link(current):
            raise ValueError("состояние карты: ссылка в пути не принимается")
        if current.exists() and not current.is_dir():
            raise ValueError("состояние карты: родитель не является каталогом")
    if is_link(path):
        raise ValueError("состояние карты: ссылка в пути не принимается")
    return root, path, rel.parts


def _read(root: Path, path: Path) -> Dict[str, Any]:
    _, path, _ = _checked_path(root, path)
    if not path.exists():
        return {}
    if not path.is_file():
        raise ValueError("состояние карты: ожидался обычный файл")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("version") != VERSION:
        raise ValueError("состояние карты: неизвестная версия")
    return doc


def _write(root: Path, path: Path, doc: Dict[str, Any]) -> None:
    root, path, parts = _checked_path(root, path)
    # Сначала проверены ВСЕ существующие компоненты. Только потом создаём отсутствующие,
    # по одному и строго под root: `.veha` или промежуточный junction не уведёт mkdir наружу.
    current = root
    for part in parts[:-1]:
        current = current / part
        if not current.exists():
            current.mkdir()
        if is_link(current) or not current.is_dir():
            raise ValueError("состояние карты: ссылка в пути не принимается")
    if is_link(path) or (path.exists() and not path.is_file()):
        raise ValueError("состояние карты: ожидался обычный файл")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(doc, stream, ensure_ascii=False, indent=1)
        _checked_path(root, path)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _test_key(test: Dict[str, Any]) -> str:
    return core_sources.test_identity(test)


def _test_keys(test: Dict[str, Any], ambiguous_aliases: set[str] | None = None) -> set[str]:
    """Canonical identity и однозначный alias observations Atlas для partial merge."""
    keys = {_test_key(test)}
    atlas_variant = test.get("atlasVariant")
    if isinstance(atlas_variant, str) and atlas_variant \
            and atlas_variant not in (ambiguous_aliases or set()):
        keys.add(atlas_variant)
    return keys


def _ambiguous_atlas_aliases(tests: Iterable[Dict[str, Any]]) -> set[str]:
    """Lossy aliases, которые одновременно принадлежат разным canonical identity."""
    owners: Dict[str, set[str]] = {}
    for test in tests:
        alias = test.get("atlasVariant")
        if isinstance(alias, str) and alias:
            owners.setdefault(alias, set()).add(_test_key(test))
    return {alias for alias, identities in owners.items() if len(identities) > 1}


def _record_key(record: Dict[str, Any]) -> str:
    return json.dumps([record.get(k) for k in _ID_FIELDS], ensure_ascii=False, sort_keys=True)


def _merge_records(old: Iterable[Any], new: Iterable[Any]) -> List[dict]:
    out = {(_record_key(r) if isinstance(r, dict) else json.dumps(r, sort_keys=True)): r
           for r in old}
    for record in new:
        key = _record_key(record) if isinstance(record, dict) else json.dumps(record, sort_keys=True)
        out[key] = record
    return list(out.values())


def _current_artifact(doc: Dict[str, Any], kind: str, run_id: str, *, full: bool,
                      problems: List[str]) -> Dict[str, Any] | None:
    """Оставить наблюдения текущего прогона, не импортируя историю append-артефакта.

    Новый ``ui.flow`` перезаписывается целиком, а его записи несут ``nodeid/variant/run``.
    Пустой документ — наблюдение текущего прогона: затронутые тесты известны из Allure.
    Непустой полностью неразмеченный legacy partial ничего доказать не может: сохраняем
    прежнее состояние и показываем проблему. Для полной замены legacy остаётся совместимым.
    """
    key = core_sources.PAYLOAD_KEY[kind]
    records = list(doc.get(key) or [])
    if kind == "ui.flow":
        if not records:
            return doc
        if not any(isinstance(r, dict) and r.get("run") for r in records):
            if full:
                return doc
            problems.append(
                "ui.flow: частичный прогон не обновил накопленную карту: артефакт без "
                "меток run; сохранено прежнее состояние")
            return None
    if not run_id:
        return None
    filtered = [r for r in records if isinstance(r, dict)
                and isinstance(r.get("run"), dict)
                and r["run"].get("id") == run_id]
    if kind == "ui.flow":
        return {**doc, **atlas_ui_flow.build(filtered)}
    return {**doc, key: filtered}


def _merge_artifact(old: Dict[str, Any], new: Dict[str, Any], kind: str, full: bool,
                    new_keys: set[str] | None = None) -> Dict[str, Any]:
    if full:
        return new
    key = core_sources.PAYLOAD_KEY[kind]
    old_records = list(old.get(key) or [])
    if new_keys is not None:
        old_records = [r for r in old_records
                       if not isinstance(r, dict) or _test_key(r) not in new_keys]
    merged = _merge_records(old_records, new.get(key) or [])
    if kind == "ui.flow":
        # Статистика принадлежит формату Atlas и пересчитывается его же сборщиком.
        return {**new, **atlas_ui_flow.build(merged)}
    return {**new, key: merged}


def apply(sources: core_sources.Sources) -> core_sources.Sources:
    """Наложить сохранённую структуру; битый state виден в reportProblems."""
    scope = sources.artifacts.scope
    path = state_path(sources.cfg, scope)
    try:
        doc = _read(sources.cfg.root, path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        sources.report_problems.append(
            f"накопленная карта не прочитана: {type(exc).__name__}; показаны текущие источники")
        return sources
    if not doc:
        return sources

    # State — внутренний файл, но его содержимое могло быть записано старой версией
    # без серверной границы redaction. Восстановление повторяет правила первичной загрузки:
    # Allure/coverage — структуры и текст, артефакты Atlas — текст без искажения ID.
    saved_allure = doc.get("allure") or {}
    allure = {
        "tests": list(redact.data(saved_allure.get("tests") or [])),
        # Ключи calls — endpoint ID, а не имена полей записи: `/token` не должен
        # превращать весь список вызовов в маску. Маскируем ключ как текст, записи — как данные.
        "calls": {redact.text(k): list(redact.data(v))
                  for k, v in (saved_allure.get("calls") or {}).items()},
        "attachments": set(redact.data(saved_allure.get("attachments") or [])),
    }
    cov_doc = redact.data(doc.get("coverage") or {})
    coverage = {e.get("id"): e for e in cov_doc.get("endpoints") or []
                if isinstance(e, dict) and e.get("id")}
    docs = dict(sources.artifacts.docs)
    for kind, artifact in (doc.get("artifacts") or {}).items():
        if kind in OBSERVED_KINDS and isinstance(artifact, dict):
            docs[kind] = redact.strings(artifact, skip=core_sources.IDENTIFIER_KEYS)
    artifacts = replace(sources.artifacts, docs=docs)
    sources.report_problems.extend(
        redact.text(p) for p in (doc.get("problems") or []) if isinstance(p, str))
    return replace(sources, artifacts=artifacts, coverage=coverage,
                   coverage_extras=dict(cov_doc.get("extras") or {}), allure=allure)


def _final_attachments(tests: Iterable[dict]) -> List[str]:
    """Вложения только финального набора тестов, по реальному ``source`` Allure."""
    found = set()
    for test in tests:
        sources = test.get("attachments")
        if isinstance(sources, list):
            found.update(a for a in sources
                         if isinstance(a, str) and core_sources.is_allure_attachment(a))
    return sorted(found)


def update(cfg: Config, scope: Scope, suite: Any, *, run_id: str = "") -> None:
    """Обновить state из только что завершённого опубликованного прогона.

    Атомарность между history и state не имитируется: вызывающая сторона показывает сбой
    записи. Повторное обновление того же прогона идемпотентно по ключам тестов и связей.
    ``run_id`` отделяет текущие наблюдения от истории в append-артефактах агента.
    """
    current = core_sources.load(cfg, scope)
    path = state_path(cfg, scope)
    old = _read(cfg.root, path)
    scope_name = str(getattr(suite, "map_scope", "") or "")
    full = bool(getattr(suite, "map_full", False))
    tests = list(current.allure.get("tests") or [])
    old_tests = list(((old.get("allure") or {}).get("tests")) or [])
    # В partial A один alias может выглядеть однозначным, пока не учтён сохранённый B.
    ambiguous_aliases = _ambiguous_atlas_aliases(old_tests + tests)
    new_keys = {key for test in tests
                for key in _test_keys(test, ambiguous_aliases=ambiguous_aliases)}
    touched_ambiguous = sorted(
        {test.get("atlasVariant") for test in tests} & ambiguous_aliases)

    if full:
        kept = [t for t in old_tests
                if (scope_name == "api" and t.get("isUi"))
                or (scope_name == "ui" and not t.get("isUi"))]
    else:
        kept = [t for t in old_tests if _test_key(t) not in new_keys]
    merged_tests = kept + tests

    old_calls = dict(((old.get("allure") or {}).get("calls")) or {})
    calls: Dict[str, List[dict]] = {}
    for eid, records in old_calls.items():
        if full:
            # Вызовы безопасно переувязываются по тестам: API full не трогает UI-only
            # наблюдения, а UI full заменяет вызовы только своих прежних UI-тестов.
            old_ui = {t.get("nodeid") for t in old_tests if t.get("isUi")}
            old_api = {t.get("nodeid") for t in old_tests if not t.get("isUi")}
            touched = old_ui if scope_name == "ui" else old_api
            records = [r for r in records if r.get("nodeid") not in touched]
        else:
            records = [r for r in records if _test_key(r) not in new_keys]
        if records:
            calls[eid] = list(records)
    for eid, records in (current.allure.get("calls") or {}).items():
        calls.setdefault(eid, []).extend(records)

    old_cov = list(((old.get("coverage") or {}).get("endpoints")) or [])
    if full and scope_name == "api":
        endpoints = [dict(e, id=e.get("id") or core_sources.api_id(
            e.get("method", ""), e.get("path", ""))) for e in current.coverage.values()]
        extras = dict(current.coverage_extras)
    else:
        by_id = {e.get("id") or core_sources.api_id(e.get("method", ""), e.get("path", "")): e
                 for e in old_cov if isinstance(e, dict)}
        for eid, endpoint in current.coverage.items():
            by_id[eid] = dict(endpoint, id=eid)
        endpoints = list(by_id.values())
        extras = dict((old.get("coverage") or {}).get("extras") or current.coverage_extras)

    problems: List[str] = []
    if not full and touched_ambiguous:
        problems.append(
            f"Atlas: общий alias {', '.join(touched_ambiguous)} неоднозначен; прежние "
            "observations с этим alias сохранены, точная partial-замена не утверждается")
    artifacts: Dict[str, Any] = dict(old.get("artifacts") or {})
    for kind in OBSERVED_KINDS:
        if kind == "ui.flow" and scope_name == "api":
            continue
        if kind != "ui.flow" and scope_name == "ui":
            continue
        status = current.artifacts.statuses.get(kind)
        if status is None or not status.usable:
            continue
        doc = current.artifacts.doc(kind)
        new_keys_for_kind = None
        if kind != "containers.logs":
            doc = _current_artifact(doc, kind, run_id, full=full, problems=problems)
            if doc is None:
                continue
            new_keys_for_kind = new_keys
        artifacts[kind] = _merge_artifact(artifacts.get(kind) or {}, doc, kind, full,
                                          new_keys=new_keys_for_kind)

    doc = {
        "version": VERSION,
        "scope": scope.as_dict(),
        "updatedAt": time.time(),
        "suite": getattr(suite, "key", ""),
        "mapScope": scope_name,
        "mapFull": full,
        "allure": {"tests": merged_tests, "calls": calls,
                   "attachments": _final_attachments(merged_tests)},
        "coverage": {"endpoints": endpoints, "extras": extras},
        "artifacts": artifacts,
        "problems": [redact.text(problem) for problem in problems],
    }
    _write(cfg.root, path, doc)
