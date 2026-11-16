"""Opt-in подключение истории Allure к runner.

Здесь живут конфигурация и безопасные файловые переходы, а не знание о проектных
наборах. Pytest запускает :mod:`veha.runner`; этот модуль очищает только стандартные
файлы Allure в ``reports.allure`` и делегирует публикацию :mod:`veha.allure_store`.
"""

from __future__ import annotations

import math
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from partest_atlas.artifacts import Scope

from veha import allure_store, redact
from veha.config import Config, report_path_problem
from veha.core.sources import is_allure_file

DEFAULT_STORE = ".veha/allure"
DEFAULT_COMMAND = ["allure", "generate"]
DEFAULT_TIMEOUT = 300.0
_ALLOWED = {"store", "keep", "command", "timeout"}


@dataclass(frozen=True)
class Settings:
    """Проверенная конфигурация ``runner.allure``."""

    results: Path
    store: Path
    keep: int
    command: List[str]
    timeout: float


def _is_link(path: Path) -> bool:
    """Ссылка или Windows reparse point; нечитаемый объект считается небезопасным."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _inside(cfg: Config, path: Path) -> bool:
    try:
        return cfg.inside_root(path)
    except (OSError, RuntimeError, ValueError):
        return False


def _ensure_dir(cfg: Config, path: Path, where: str) -> None:
    if not _inside(cfg, path):
        raise ValueError(f"{where}: каталог обязан лежать строго внутри корня проекта")
    if os.path.lexists(path):
        if _is_link(path) or not path.is_dir():
            raise ValueError(f"{where}: ожидался обычный каталог, не ссылка или файл")
    else:
        path.mkdir(parents=True, exist_ok=True)


def load_settings(cfg: Config) -> Optional[Settings]:
    """Прочитать opt-in раздел; ``None`` означает прежнее поведение runner.

    Ошибки конфигурации поднимаются до запуска pytest. Пустой ``allure: {}`` включает
    умолчания, а отсутствующий или YAML-null раздел интеграцию не включает.
    """
    section = cfg.section("runner")
    if "allure" not in section or section["allure"] is None:
        return None
    raw = section["allure"]
    if not isinstance(raw, dict):
        raise ValueError("runner.allure: ожидался словарь настроек")
    unknown = set(raw) - _ALLOWED
    if unknown:
        name = str(min(unknown, key=str))[:40]
        raise ValueError(f"runner.allure.{name}: неизвестный ключ")

    store_raw = raw.get("store", DEFAULT_STORE)
    if not isinstance(store_raw, str) or not store_raw:
        raise ValueError("runner.allure.store: ожидалась непустая строка пути")
    problem = report_path_problem("runner.allure.store", store_raw)
    if problem:
        raise ValueError(problem)
    store = cfg.path(store_raw)
    results = cfg.allure_dir
    if store is None or results is None:
        raise ValueError("runner.allure: негодный путь store или reports.allure")
    keep = raw.get("keep", allure_store.DEFAULT_KEEP)
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise ValueError("runner.allure.keep: нужно целое >= 1")
    command = raw.get("command", DEFAULT_COMMAND)
    if (not isinstance(command, list) or not command
            or any(not isinstance(part, str) or not part or "\x00" in part for part in command)):
        raise ValueError("runner.allure.command: нужен непустой argv-массив строк")
    timeout = raw.get("timeout", DEFAULT_TIMEOUT)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("runner.allure.timeout: нужно положительное конечное число")

    # Все значения и отношения путей проверяются до создания каталогов.
    if not _inside(cfg, results) or not _inside(cfg, store):
        raise ValueError("runner.allure: store и reports.allure обязаны быть внутри корня")
    real_results, real_store = results.resolve(), store.resolve()
    if (real_results == real_store or real_results in real_store.parents
            or real_store in real_results.parents):
        raise ValueError("runner.allure.store и reports.allure не должны пересекаться")
    _ensure_dir(cfg, results, "reports.allure")
    _ensure_dir(cfg, store, "runner.allure.store")
    return Settings(results=results, store=store, keep=keep, command=list(command),
                    timeout=float(timeout))


def clean_results(path: Path) -> int:
    """Удалить файлы прежнего прогона, сохраняя проектные файлы и подкаталоги."""
    removed = 0
    for item in path.iterdir():
        if not is_allure_file(item.name):
            continue
        info = item.lstat()
        if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            item.unlink()
            removed += 1
        else:
            raise ValueError(f"reports.allure: неизвестный объект не удаляется: {item.name}")
    return removed


def has_results(path: Path) -> bool:
    """Есть ли вход для Allure; ссылка с именем результата тоже требует проверки store."""
    return any(item.name.endswith("-result.json") and is_allure_file(item.name)
               and os.path.lexists(item) for item in path.iterdir())


def prepare(cfg: Config, settings: Settings, scope: Scope, suite: str) -> Dict[str, Any]:
    """Повторить ротацию после сбоя и не дать новому прогону смешаться со старым raw."""
    recovered = allure_store.recover(
        settings.store, system=scope.system, env=scope.env, suite=suite, keep=settings.keep)
    clean_results(settings.results)
    if recovered.rotation_status == "failed":
        return {"recovery": "failed", "recoveryError": safe_text(cfg, recovered.rotation_error)}
    return {"recovery": "ok"}


def safe_text(cfg: Config, value: Any) -> str:
    """Краткая причина для API без абсолютных путей и распознанных секретов."""
    text = redact.text(str(value))
    paths = [cfg.root, Path.home()]
    for path in paths:
        raw = str(path)
        if raw and len(raw) > 3:
            text = text.replace(raw, "<каталог>").replace(raw.replace("\\", "/"), "<каталог>")
    return text[:300]


def publish(cfg: Config, settings: Settings, scope: Scope, suite: str,
            generate: Callable[[Path, Path], None], previous: Optional[Dict[str, Any]] = None,
            ) -> Dict[str, Any]:
    """Опубликовать отчёт и вернуть только публичное состояние без путей и env."""
    report: Dict[str, Any] = dict(previous or {})
    if report.get("recovery") == "ok":
        report.pop("recoveryError", None)
    if not has_results(settings.results):
        return {**report, "status": "no_results"}
    try:
        result = allure_store.publish(
            settings.store, system=scope.system, env=scope.env, suite=suite,
            results=settings.results, generate=generate, keep=settings.keep)
    except Exception as exc:
        return {**report, "status": "failed", "error": safe_text(cfg, exc)}
    out = {**report, "status": "published", "snapshot": result.run_id,
           "runs": len(result.runs)}
    if result.rotation_status != "ok":
        out["status"] = "rotation_failed"
        out["error"] = safe_text(cfg, result.rotation_error)
    return out
