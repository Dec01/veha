"""Атомарная история сгенерированных отчётов Allure для каждого набора.

Хранилище не запускает pytest или Allure и не знает проектных имён. Вызывающая сторона
передаёт каталог сырых результатов и функцию генерации. Отчёт собирается во временном
каталоге на той же файловой системе, а ``latest`` переключается одним ``os.replace``.
По умолчанию остаются текущий и предыдущий снапшоты каждого ``system/env/suite``.

Раскладка::

    <root>/<system>/<env>/<suite>/
        runs/<run_id>/            неизменяемый сгенерированный отчёт
        raw/<run_id>/             сырые результаты того же прогона
        latest -> runs/<run_id>   относительная символическая ссылка
        state.json                опубликованные run_id, от старого к новому

Один набор должен публиковать один writer. Каталоги из ``runs``, не названные в state
или latest, считаются неопубликованными: recovery не поднимает их в историю по mtime.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from veha.core.sources import valid_segment

STATE_VERSION = 1
MANIFEST = "veha-allure.json"
DEFAULT_KEEP = 2

Generator = Callable[[Path, Path], None]


class AllureStoreError(RuntimeError):
    """Негодная раскладка или незавершённая операция хранилища."""


@dataclass(frozen=True)
class StoreResult:
    """Результат публикации или восстановления; пути относятся к одному набору."""

    system: str
    env: str
    suite: str
    run_id: str
    latest: Optional[Path]
    runs: Tuple[str, ...]
    report_status: str                 # published | recovered
    rotation_status: str = "ok"       # ok | failed
    rotation_error: str = ""


def _lexists(path: Path) -> bool:
    return os.path.lexists(str(path))


def _is_link(path: Path) -> bool:
    """Символическая ссылка или Windows reparse point, включая junction.

    Проверка по ``lstat`` не разворачивает ссылку. Ошибка чтения закрывает путь:
    неизвестный объект нельзя считать безопасным каталогом или файлом.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _read_link(path: Path) -> str:
    return os.readlink(path)


def _create_link(target: Path, path: Path) -> None:
    os.symlink(target, path, target_is_directory=True)


def _plain_dir(path: Path, what: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise AllureStoreError(f"{what}: каталога нет") from None
    if _is_link(path) or not stat.S_ISDIR(mode):
        raise AllureStoreError(f"{what}: ожидался обычный каталог, не ссылка")


def _scope(root: Path, system: str, env: str, suite: str, create: bool) -> Path:
    for name, value in (("system", system), ("env", env), ("suite", suite)):
        if not valid_segment(value):
            raise ValueError(f"{name}: нужен один безопасный сегмент пути")
    root = Path(root)
    if create:
        root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise AllureStoreError("корень хранилища: ожидался каталог")
    current = root
    for value in (system, env, suite):
        current = current / value
        if _lexists(current):
            _plain_dir(current, "путь области хранилища")
        elif create:
            current.mkdir()
        else:
            return current
    return current


def _managed_dir(scope: Path, name: str, create: bool = True) -> Path:
    path = scope / name
    if _lexists(path):
        _plain_dir(path, name)
    elif create:
        path.mkdir()
    return path


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{stamp}-{uuid.uuid4().hex}"


def _copy_tree(source: Path, target: Path) -> None:
    """Копия без ссылок и специальных файлов: отчёт не должен уводить nginx наружу."""
    _plain_dir(source, "источник копии")
    target.mkdir(parents=True, exist_ok=True)
    for src in sorted(source.iterdir(), key=lambda p: p.name):
        mode = src.lstat().st_mode
        dst = target / src.name
        if _is_link(src):
            raise AllureStoreError(f"символическая ссылка в данных Allure не принимается: {src.name}")
        if stat.S_ISDIR(mode):
            _copy_tree(src, dst)
        elif stat.S_ISREG(mode):
            shutil.copy2(src, dst, follow_symlinks=False)
        else:
            raise AllureStoreError(f"специальный файл в данных Allure не принимается: {src.name}")


def _check_plain_tree(root: Path) -> None:
    """Проверить отчёт, не заходя внутрь ссылок, включая Windows junction.

    ``Path.rglob`` может рекурсировать в junction до того, как вернёт саму ссылку.
    Поэтому обход явный: сначала ``lstat`` и отказ от reparse point, затем рекурсия.
    """
    _plain_dir(root, "сгенерированный отчёт")
    for path in sorted(root.iterdir(), key=lambda p: p.name):
        mode = path.lstat().st_mode
        if _is_link(path):
            raise AllureStoreError(
                f"символическая ссылка в отчёте Allure не принимается: {path.name}")
        if stat.S_ISDIR(mode):
            _check_plain_tree(path)
        elif not stat.S_ISREG(mode):
            raise AllureStoreError(f"специальный файл в отчёте Allure не принимается: {path.name}")


def _read_json(path: Path, what: str) -> dict:
    if _is_link(path) or not path.is_file():
        raise AllureStoreError(f"{what}: ожидался обычный JSON-файл")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AllureStoreError(f"{what}: не прочитан ({type(exc).__name__})") from exc
    if not isinstance(value, dict):
        raise AllureStoreError(f"{what}: ожидался JSON-объект")
    return value


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_state(scope: Path, system: str, env: str, suite: str,
                 latest: Optional[str], runs: List[str]) -> None:
    """Атомарная замена state; временный файл лежит рядом, на той же ФС."""
    tmp = scope / f".state-{uuid.uuid4().hex}.tmp"
    try:
        with tmp.open("x", encoding="utf-8") as stream:
            json.dump({"version": STATE_VERSION, "system": system, "env": env,
                       "suite": suite, "latest": latest, "runs": runs},
                      stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, scope / "state.json")
    finally:
        if _lexists(tmp):
            tmp.unlink()


def _read_state(scope: Path, system: str, env: str, suite: str) -> List[str]:
    path = scope / "state.json"
    if not _lexists(path):
        return []
    doc = _read_json(path, "state.json")
    if doc.get("version") != STATE_VERSION or doc.get("runs") is None:
        raise AllureStoreError("state.json: неизвестная версия или нет списка runs")
    if (doc.get("system"), doc.get("env"), doc.get("suite")) != (system, env, suite):
        raise AllureStoreError("state.json: область не совпадает с путём")
    runs = doc["runs"]
    if not isinstance(runs, list) or any(not isinstance(x, str) or not valid_segment(x)
                                         for x in runs) or len(set(runs)) != len(runs):
        raise AllureStoreError("state.json: runs должен быть списком уникальных run_id")
    return list(runs)


def _read_latest(scope: Path) -> Optional[str]:
    """Принять только управляемую относительную ссылку ``runs/<run_id>``."""
    path = scope / "latest"
    if not _lexists(path):
        return None
    if not _is_link(path):
        raise AllureStoreError("latest: старый каталог или файл не мигрируется автоматически")
    target = Path(_read_link(path))
    if target.is_absolute() or len(target.parts) != 2 or target.parts[0] != "runs" \
            or not valid_segment(target.parts[1]):
        raise AllureStoreError("latest: ссылка указывает вне управляемой раскладки")
    run_id = target.parts[1]
    _managed_dir(scope, "runs", create=False)
    _plain_dir(scope / "runs" / run_id, "latest")
    return run_id


def _check_manifest(scope: Path, system: str, env: str, suite: str, run_id: str) -> None:
    run = scope / "runs" / run_id
    if not _lexists(run):
        return                         # остаток незавершённой ротации уберёт prune
    _plain_dir(run, "снапшот")
    doc = _read_json(run / MANIFEST, MANIFEST)
    expected = {"version": STATE_VERSION, "system": system, "env": env,
                "suite": suite, "run_id": run_id}
    if any(doc.get(key) != value for key, value in expected.items()):
        raise AllureStoreError(f"{MANIFEST}: снапшот не принадлежит этой области")
    index = run / "index.html"
    if _is_link(index) or not index.is_file():
        raise AllureStoreError("снапшот: нет обычного index.html")


def _recover_ids(scope: Path, system: str, env: str, suite: str) -> Tuple[List[str], Optional[str]]:
    """State — журнал публикаций, latest — возможная незаписанная последняя публикация.

    Прочие каталоги runs намеренно не сканируются: сбой до переключения latest оставляет
    неопубликованный снапшот, который не должен вытеснять настоящую историю.
    """
    latest = _read_latest(scope)
    ids = _read_state(scope, system, env, suite)
    if latest is not None:
        ids = [run_id for run_id in ids if run_id != latest] + [latest]
    for run_id in ids:
        _check_manifest(scope, system, env, suite, run_id)
    return ids, latest


def _remove_tree(path: Path) -> None:
    if not _lexists(path):
        return
    if _is_link(path) or not path.is_dir():
        raise AllureStoreError(f"ротация: неизвестный объект не удаляется: {path.name}")
    shutil.rmtree(path)


def _prune(scope: Path, system: str, env: str, suite: str,
           ids: List[str], latest: Optional[str], keep: int) -> List[str]:
    kept = ids[-keep:]
    obsolete = ids[:-keep]
    for run_id in obsolete:
        if run_id == latest:            # защита даже от повреждённого порядка журнала
            raise AllureStoreError("ротация: текущий latest нельзя удалять")
        _remove_tree(scope / "runs" / run_id)
        _remove_tree(scope / "raw" / run_id)
    _write_state(scope, system, env, suite, latest, kept)
    return kept


def recover(root: Path, *, system: str, env: str, suite: str,
            keep: int = DEFAULT_KEEP) -> StoreResult:
    """Свести state с latest и повторить безопасную ротацию после сбоя.

    Неопубликованные staging/runs не выбираются по времени. Отсутствующий report и оставшийся
    raw одного старого run_id обрабатываются повторным вызовом без пропуска очистки.
    """
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise ValueError("keep: нужно целое >= 1")
    scope = _scope(Path(root), system, env, suite, create=False)
    if not _lexists(scope):
        return StoreResult(system, env, suite, "", None, (), "recovered")
    ids, latest = _recover_ids(scope, system, env, suite)
    status, error = "ok", ""
    try:
        kept = _prune(scope, system, env, suite, ids, latest, keep)
    except (OSError, AllureStoreError) as exc:
        kept, status, error = ids, "failed", str(exc)
    latest_path = scope / "latest" if latest is not None else None
    return StoreResult(system, env, suite, latest or "", latest_path, tuple(kept),
                       "recovered", status, error)


def publish(root: Path, *, system: str, env: str, suite: str, results: Path,
            generate: Generator, keep: int = DEFAULT_KEEP) -> StoreResult:
    """Сгенерировать и опубликовать снапшот, не изменяя прежний отчёт при сбое.

    ``generate(raw_copy, report_dir)`` получает копию сырых данных: в её ``history`` уже
    лежит история предыдущего снапшота, как того ожидает Allure. Функция должна создать
    ``index.html`` в новом ``report_dir``. Код возврата внешней команды вызывающая сторона
    проверяет сама (например, ``check=True``).

    Сбой ротации не скрывается и не откатывает уже переключённый latest: результат имеет
    ``report_status='published'``, ``rotation_status='failed'``. ``recover`` повторяет prune.
    """
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise ValueError("keep: нужно целое >= 1")
    if not callable(generate):
        raise TypeError("generate: ожидалась функция")
    results = Path(results)
    _plain_dir(results, "каталог результатов Allure")
    scope = _scope(Path(root), system, env, suite, create=True)
    runs = _managed_dir(scope, "runs")
    raw = _managed_dir(scope, "raw")
    staging = _managed_dir(scope, ".staging")
    previous_ids, previous_latest = _recover_ids(scope, system, env, suite)

    run_id = _new_run_id()
    staged_report = staging / run_id
    staged_raw = staging / f"{run_id}.raw"
    final_report = runs / run_id
    final_raw = raw / run_id
    staged_report.mkdir()
    try:
        _copy_tree(results, staged_raw)
        if previous_latest is not None:
            history = runs / previous_latest / "history"
            if _lexists(history):
                # Allure читает прежнюю историю из входного каталога результатов.
                _copy_tree(history, staged_raw / "history")
        generate(staged_raw, staged_report)
        _check_plain_tree(staged_report)
        index = staged_report / "index.html"
        if index.is_symlink() or not index.is_file():
            raise AllureStoreError("генератор Allure не создал обычный index.html")
        _write_json(staged_report / MANIFEST, {
            "version": STATE_VERSION, "system": system, "env": env, "suite": suite,
            "run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(),
        })
        os.replace(staged_report, final_report)
        os.replace(staged_raw, final_raw)
    except Exception:
        _remove_tree(staged_report)
        _remove_tree(staged_raw)
        raise

    tmp_latest = scope / f".latest-{uuid.uuid4().hex}.tmp"
    switched = False
    try:
        _create_link(Path("runs") / run_id, tmp_latest)
        os.replace(tmp_latest, scope / "latest")
        switched = True
    finally:
        if _lexists(tmp_latest):
            tmp_latest.unlink()
        if not switched:
            # До переключения новый снапшот не опубликован; прежний latest не тронут.
            _remove_tree(final_report)
            _remove_tree(final_raw)

    ids = [x for x in previous_ids if x != run_id] + [run_id]
    status, error = "ok", ""
    kept = ids
    try:
        _write_state(scope, system, env, suite, run_id, ids)
        kept = _prune(scope, system, env, suite, ids, run_id, keep)
    except (OSError, AllureStoreError) as exc:
        status, error = "failed", str(exc)
    return StoreResult(system, env, suite, run_id, scope / "latest", tuple(kept),
                       "published", status, error)
