"""Проектные обработчики жизненного цикла прогона.

Регистрация — только в доверенном ``atlas.yaml``. Браузер меняет enabled и scalar-настройки,
явно объявленные проектом; command, cwd, stages и env allow-list через HTTP не приходят.
Команда выполняется без shell, в своей process group, с ограниченным выводом и минимальным
окружением. Снимок команды и настроек в manifest позволяет безопасно повторить recovery после
перезапуска сервера; изменившаяся реализация запрещает автоматическое восстановление.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from veha import redact
from veha.config import Config, local_to, report_path_problem

SECTION = "lifecycle_plugins"
CONTRACT_VERSION = 1
STATE_VERSION = 1
STAGES = ("validate", "after_run", "recovery")
OUTPUT_LIMIT = 65536
OUTPUT_LINES = 200
PUMP_GRACE_SEC = 2.0
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_PLUGIN_KEYS = {
    "label", "kind", "command", "cwd", "stages", "required", "timeout", "suites",
    "env", "implementation", "editable", "settings",
}
_SETTING_KEYS = {"type", "default"}


class LifecycleError(RuntimeError):
    """Негодная конфигурация, состояние или восстановление: продолжать небезопасно."""


@dataclass(frozen=True)
class Setting:
    key: str
    kind: str
    default: Any = None

    @property
    def schema(self) -> dict[str, Any]:
        return {"type": self.kind, "default": self.default}

    def value(self, value: Any) -> Any:
        where = f"настройка {self.key}"
        if self.kind == "boolean":
            if not isinstance(value, bool):
                raise LifecycleError(f"{where}: ожидался boolean")
            return value
        if self.kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int) or abs(value) > 10**12:
                raise LifecycleError(f"{where}: ожидалось целое в пределах ±10^12")
            return value
        if self.kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise LifecycleError(f"{where}: ожидалось конечное число")
            if not math.isfinite(float(value)):
                raise LifecycleError(f"{where}: ожидалось конечное число")
            return value
        if not isinstance(value, str) or len(value) > 4096 \
                or any(ord(c) < 0x20 and c not in "\t\n" for c in value):
            raise LifecycleError(f"{where}: ожидалась строка до 4096 символов без управляющих")
        return value


@dataclass(frozen=True)
class Plugin:
    key: str
    label: str
    command: tuple[str, ...]
    cwd: str = "."
    stages: tuple[str, ...] = STAGES
    required: bool = False
    timeout: float = 600.0
    suites: tuple[str, ...] = ()
    env: tuple[str, ...] = ()
    implementation: str = ""
    editable: bool = True
    settings: dict[str, Setting] = field(default_factory=dict)

    @property
    def schema(self) -> dict[str, Any]:
        return {key: setting.schema for key, setting in self.settings.items()}

    def applies(self, suite: str) -> bool:
        return not self.suites or suite in self.suites

    def snapshot(self, root: Path, settings: dict[str, Any]) -> dict[str, Any]:
        return {
            "contract_version": CONTRACT_VERSION, "id": self.key, "label": self.label,
            "kind": "command", "command": list(self.command), "cwd": self.cwd,
            "stages": list(self.stages), "required": self.required, "timeout": self.timeout,
            "suites": list(self.suites), "env": list(self.env),
            "implementation": self.implementation, "editable": self.editable,
            "settings_schema": self.schema, "settings": dict(settings),
            "implementation_digest": _digest_at(root, self),
        }


def _bool(value: Any, where: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise LifecycleError(f"{where}: ожидался boolean")
    return value


def _strings(value: Any, where: str, *, pattern: re.Pattern | None = None) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise LifecycleError(f"{where}: ожидался список строк")
    out = tuple(value)
    if len(set(out)) != len(out):
        raise LifecycleError(f"{where}: повторы не допускаются")
    if pattern and any(not pattern.fullmatch(v) for v in out):
        raise LifecycleError(f"{where}: негодное имя")
    return out


def _local_path(root: Path, value: str, where: str, *, directory: bool) -> Path:
    problem = report_path_problem(where, value)
    if problem:
        raise LifecycleError(problem)
    path = Path(value)
    if path.is_absolute():
        raise LifecycleError(f"{where}: путь должен быть относительным и внутри корня")
    norm = Path(os.path.normpath(root / path))
    if norm != root and not local_to(root, norm):
        raise LifecycleError(f"{where}: путь вне корня или идёт через ссылку")
    if directory and not norm.is_dir():
        raise LifecycleError(f"{where}: каталог не существует")
    if not directory and not norm.is_file():
        raise LifecycleError(f"{where}: файл не существует")
    return norm


def _parse_settings(raw: Any, where: str) -> dict[str, Setting]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise LifecycleError(f"{where}: ожидался словарь схем настроек")
    out: dict[str, Setting] = {}
    for key, spec in raw.items():
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key) or redact.sensitive_key(key):
            raise LifecycleError(f"{where}: негодное или чувствительное имя настройки")
        if not isinstance(spec, dict) or set(spec) - _SETTING_KEYS:
            raise LifecycleError(f"{where}.{key}: ожидались только type и default")
        kind = spec.get("type")
        if kind not in ("string", "integer", "number", "boolean"):
            raise LifecycleError(f"{where}.{key}: неподдерживаемый type")
        if "default" not in spec:
            raise LifecycleError(f"{where}.{key}: нужен безопасный default")
        setting = Setting(key, kind, spec["default"])
        setting.value(setting.default)
        out[key] = setting
    return out


def parse_plugin(root: Path, key: str, raw: Any) -> Plugin:
    where = f"{SECTION}.{key}"
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        raise LifecycleError(f"{SECTION}: негодный идентификатор плагина")
    if not isinstance(raw, dict) or set(raw) - _PLUGIN_KEYS:
        raise LifecycleError(f"{where}: ожидался словарь без неизвестных ключей")
    if raw.get("kind", "command") != "command":
        raise LifecycleError(f"{where}.kind: поддерживается только command")
    command = _strings(raw.get("command"), f"{where}.command")
    if not command or any(not v or len(v) > 4096 or "\x00" in v for v in command):
        raise LifecycleError(f"{where}.command: нужен непустой argv-массив")
    cwd = raw.get("cwd", ".")
    if not isinstance(cwd, str):
        raise LifecycleError(f"{where}.cwd: ожидалась строка")
    stages = _strings(raw.get("stages"), f"{where}.stages")
    if not stages or any(s not in STAGES for s in stages):
        raise LifecycleError(f"{where}.stages: допустимы validate, after_run, recovery")
    timeout = raw.get("timeout", 600)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
            or not math.isfinite(float(timeout)) or not 1 <= timeout <= 86400:
        raise LifecycleError(f"{where}.timeout: ожидалось число от 1 до 86400")
    implementation = raw.get("implementation", "")
    if not isinstance(implementation, str) or len(implementation) > 256:
        raise LifecycleError(f"{where}.implementation: ожидалась строка до 256 символов")
    label = raw.get("label", key)
    if not isinstance(label, str) or not label.strip() or len(label) > 200:
        raise LifecycleError(f"{where}.label: ожидалась непустая строка до 200 символов")
    plugin = Plugin(
        key=key, label=label, command=command, cwd=cwd, stages=stages,
        required=_bool(raw.get("required"), f"{where}.required", False),
        timeout=float(timeout), suites=_strings(raw.get("suites"), f"{where}.suites"),
        env=_strings(raw.get("env"), f"{where}.env", pattern=_ENV_RE),
        implementation=implementation,
        editable=_bool(raw.get("editable"), f"{where}.editable", True),
        settings=_parse_settings(raw.get("settings"), f"{where}.settings"),
    )
    _local_path(root, cwd, f"{where}.cwd", directory=True)
    _validate_command(root, plugin)
    return plugin


def _validate_command(root: Path, plugin: Plugin) -> None:
    first = plugin.command[0]
    python = first.lower() in ("python", "python3", "py") or Path(first).name.lower() in (
        "python", "python3", "python.exe")
    if python:
        if len(plugin.command) < 2:
            raise LifecycleError(f"{SECTION}.{plugin.key}.command: не назван скрипт или модуль")
        script = plugin.command[1]
        if not script.startswith("-"):
            _local_path(root, script, f"{SECTION}.{plugin.key}.command[1]", directory=False)
    elif "/" in first or "\\" in first or Path(first).is_absolute():
        _local_path(root, first, f"{SECTION}.{plugin.key}.command[0]", directory=False)


def plugins(cfg: Config) -> list[Plugin]:
    raw = cfg.raw.get(SECTION)
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise LifecycleError(f"{SECTION}: ожидался словарь плагинов")
    return [parse_plugin(cfg.root, key, spec) for key, spec in raw.items()]


def configured(cfg: Config) -> bool:
    return SECTION in cfg.raw


def state_dir(cfg: Config) -> Path:
    return cfg.root / ".veha" / "lifecycle"


def manifest_path(cfg: Config, run_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{12}", run_id):
        raise LifecycleError("негодный run id")
    return state_dir(cfg) / "runs" / f"{run_id}.json"


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("x", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def write_manifest(cfg: Config, manifest: dict[str, Any]) -> None:
    _atomic_json(manifest_path(cfg, str(manifest.get("run_id", ""))), manifest)


class FileLock:
    """Неблокирующий OS lock; handle живёт у владельца до конца lifecycle."""

    def __init__(self, path: Path):
        self.path = path
        self.handle: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+b")
        try:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            fh.close()
            return False
        self.handle = fh
        return True

    def release(self) -> None:
        if self.handle is None:
            return
        fh, self.handle = self.handle, None
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


class StateStore:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.path = state_dir(cfg) / "state.json"
        self.audit_path = state_dir(cfg) / "audit.jsonl"

    def read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": STATE_VERSION, "plugins": {}}
        except (OSError, ValueError) as exc:
            raise LifecycleError(f"состояние плагинов не прочитано: {type(exc).__name__}")
        if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION \
                or not isinstance(raw.get("plugins"), dict):
            raise LifecycleError("состояние плагинов имеет непонятный формат")
        return raw

    def binding(self, plugin: Plugin) -> tuple[bool, dict[str, Any]]:
        saved = self.read()["plugins"].get(plugin.key) or {}
        if not isinstance(saved, dict):
            raise LifecycleError(f"состояние {plugin.key}: ожидался объект")
        enabled = saved.get("enabled", True)
        if not isinstance(enabled, bool):
            raise LifecycleError(f"состояние {plugin.key}.enabled: ожидался boolean")
        if plugin.required:
            enabled = True
        values = saved.get("settings") or {}
        if not isinstance(values, dict) or set(values) - set(plugin.settings):
            raise LifecycleError(f"состояние {plugin.key}.settings: неизвестная настройка")
        settings = {key: setting.value(values.get(key, setting.default))
                    for key, setting in plugin.settings.items()}
        return enabled, settings

    def update(self, plugin: Plugin, enabled: Any, settings: Any, *, scope: Any) -> dict[str, Any]:
        if not plugin.editable:
            raise LifecycleError("плагин доступен только для чтения")
        if enabled is not None and not isinstance(enabled, bool):
            raise LifecycleError("enabled: ожидался boolean")
        if plugin.required and enabled is False:
            raise LifecycleError("обязательный плагин нельзя отключить")
        if settings is None:
            settings = {}
        if not isinstance(settings, dict) or set(settings) - set(plugin.settings):
            raise LifecycleError("settings: настройка не объявлена проектом")
        with locked(state_dir(cfg=self.cfg) / "state.lock"):
            state = self.read()
            old_enabled, old = self.binding(plugin)
            new_enabled = old_enabled if enabled is None else enabled
            new = {**old, **{key: plugin.settings[key].value(value)
                             for key, value in settings.items()}}
            state["plugins"][plugin.key] = {"enabled": new_enabled, "settings": new}
            _atomic_json(self.path, state)
            self._audit({
                "at": time.time(), "event": "settings", "plugin": plugin.key,
                "actor": "web-ui (identity supplied by deployment auth)",
                "scope": {"system": scope.system, "env": scope.env},
                "before": {"enabled": old_enabled, "settings": old},
                "after": {"enabled": new_enabled, "settings": new},
            })
        return {"enabled": new_enabled, "settings": new}

    def record(self, result: PluginResult) -> None:
        with locked(state_dir(self.cfg) / "state.lock"):
            state = self.read()
            entry = state["plugins"].setdefault(result.plugin, {})
            if not isinstance(entry, dict):
                raise LifecycleError(f"состояние {result.plugin}: ожидался объект")
            entry["last"] = result.as_dict()
            _atomic_json(self.path, state)

    def _audit(self, entry: dict[str, Any]) -> None:
        line = json.dumps(redact.data(entry), ensure_ascii=False, sort_keys=True) + "\n"
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())


@contextmanager
def locked(path: Path) -> Iterator[FileLock]:
    lock = FileLock(path)
    if not lock.acquire():
        raise LifecycleError("состояние плагинов занято другим процессом")
    try:
        yield lock
    finally:
        lock.release()


def snapshots(cfg: Config, suite: str) -> list[dict[str, Any]]:
    store = StateStore(cfg)
    out = []
    for plugin in plugins(cfg):
        if not plugin.applies(suite):
            continue
        enabled, settings = store.binding(plugin)
        if enabled:
            out.append(plugin.snapshot(cfg.root, settings))
    return out


def _from_snapshot(root: Path, raw: Any) -> tuple[Plugin, dict[str, Any], str]:
    if not isinstance(raw, dict) or raw.get("contract_version") != CONTRACT_VERSION:
        raise LifecycleError("snapshot плагина: неподдерживаемая версия контракта")
    key = raw.get("id")
    if not isinstance(key, str):
        raise LifecycleError("snapshot плагина: нет id")
    schema = raw.get("settings_schema")
    spec = {k: raw.get(k) for k in _PLUGIN_KEYS if k in raw}
    spec["settings"] = schema
    plugin = parse_plugin(root, key, spec)
    values = raw.get("settings") or {}
    if not isinstance(values, dict) or set(values) - set(plugin.settings):
        raise LifecycleError(f"snapshot {key}: неизвестная настройка")
    settings = {name: setting.value(values.get(name, setting.default))
                for name, setting in plugin.settings.items()}
    digest = raw.get("implementation_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise LifecycleError(f"snapshot {key}: негодный implementation digest")
    return plugin, settings, digest


def implementation_digest(plugin: Plugin) -> str:
    """Digest объявленной реализации; `_digest_at` добавляет байты локальных файлов."""
    digest = hashlib.sha256()
    payload = [plugin.key, plugin.implementation, plugin.cwd, *plugin.command]
    for item in payload:
        digest.update(item.encode("utf-8", errors="strict"))
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _digest_at(root: Path, plugin: Plugin) -> str:
    base = implementation_digest(plugin)
    digest = hashlib.sha256(base.encode("ascii"))
    candidates: list[str] = []
    first = plugin.command[0]
    python = first.lower() in ("python", "python3", "py") or Path(first).name.lower() in (
        "python", "python3", "python.exe")
    if python and len(plugin.command) > 1 and not plugin.command[1].startswith("-"):
        candidates.append(plugin.command[1])
    elif not python:
        candidates.append(first)
    for value in candidates:
        path = _local_path(root, value, f"{SECTION}.{plugin.key}.command", directory=False)
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(block)
    return "sha256:" + digest.hexdigest()


def _argv(root: Path, plugin: Plugin) -> list[str]:
    args = list(plugin.command)
    first = args[0].lower()
    if first in ("python", "python3", "py") or Path(first).name.lower() in (
            "python", "python3", "python.exe"):
        args[0] = sys.executable
        if len(args) > 1 and not args[1].startswith("-"):
            args[1] = str(_local_path(root, args[1], "plugin script", directory=False))
    elif "/" in args[0] or "\\" in args[0] or Path(args[0]).is_absolute():
        args[0] = str(_local_path(root, args[0], "plugin executable", directory=False))
    return args


def _environment(plugin: Plugin, event: dict[str, Any]) -> dict[str, str]:
    missing = [name for name in plugin.env if name not in os.environ]
    if missing:
        raise LifecycleError("не заданы разрешённые переменные: " + ", ".join(missing))
    env = {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
           "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    if os.name == "nt":
        for name in ("SystemRoot", "windir", "COMSPEC", "PATHEXT", "TEMP", "TMP"):
            if name in os.environ:
                env[name] = os.environ[name]
        env["PATH"] = os.environ.get("PATH", os.defpath)
    for name in plugin.env:
        env[name] = os.environ[name]
    scope = event.get("scope") or {}
    if isinstance(scope, dict):
        env["ATLAS_SYSTEM"] = str(scope.get("system", "default"))
        env["ATLAS_ENV"] = str(scope.get("env", "default"))
    return env


def _mask_env_text(value: Any, secrets: Iterable[str]) -> Any:
    """Сначала общие признаки, затем точные значения доверенных env.

    Плагин — чужой код и может напечатать DSN или токен без ключа ``password=``:
    одной эвристики здесь мало. Сами значения живут только в памяти процесса.
    """
    out = redact.text(value)
    if isinstance(out, str):
        for secret in secrets:
            if secret:
                out = out.replace(secret, redact.MASK)
    return out


def _mask_env_data(value: Any, secrets: Iterable[str]) -> Any:
    """То же для JSON, включая ключи: plugin-controlled metrics не доверенный вывод."""
    out = redact.data(value)
    if isinstance(out, dict):
        return {_mask_env_text(key, secrets): _mask_env_data(item, secrets)
                for key, item in out.items()}
    if isinstance(out, list):
        return [_mask_env_data(item, secrets) for item in out]
    return _mask_env_text(out, secrets) if isinstance(out, str) else out


class _Tail:
    def __init__(self):
        self.lines: deque[str] = deque(maxlen=OUTPUT_LINES)
        self.size = 0
        self.truncated = False
        self.secrets: tuple[str, ...] = ()

    def add(self, line: str) -> None:
        line = line.rstrip("\r\n")
        if len(line) > OUTPUT_LIMIT:
            line = line[-OUTPUT_LIMIT:]
            self.truncated = True
        self.lines.append(line)
        self.size += len(line) + 1
        while self.size > OUTPUT_LIMIT and self.lines:
            self.size -= len(self.lines.popleft()) + 1
            self.truncated = True

    def text(self) -> str:
        value = _mask_env_text("\n".join(self.lines), self.secrets)
        return ("… вывод ограничен …\n" if self.truncated else "") + value


@dataclass
class PluginResult:
    plugin: str
    stage: str
    status: str
    required: bool
    summary: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    returncode: int | None = None
    output: str = ""
    implementation_digest: str = ""

    def as_dict(self) -> dict[str, Any]:
        return redact.data({
            "id": self.plugin, "stage": self.stage, "status": self.status,
            "required": self.required, "summary": self.summary, "metrics": self.metrics,
            "durationMs": self.duration_ms, "returncode": self.returncode,
            "output": self.output, "implementationDigest": self.implementation_digest,
            "finishedAt": time.time(),
        })


def execute(root: Path, snapshot: dict[str, Any], stage: str, event: dict[str, Any],
            *, on_start: Any = None) -> PluginResult:
    plugin, settings, expected = _from_snapshot(root, snapshot)
    if stage not in plugin.stages:
        raise LifecycleError(f"{plugin.key}: стадия {stage} не объявлена")
    started = time.monotonic()
    digest = _digest_at(root, plugin)
    base = PluginResult(plugin.key, stage, "failed", plugin.required,
                        implementation_digest=digest)
    if digest != expected:
        base.summary = "реализация изменилась после snapshot — автоматический запуск запрещён"
        return base
    payload = {
        "contract_version": CONTRACT_VERSION, "run_id": event.get("run_id"),
        "suite": event.get("suite"), "stage": stage,
        "tests_status": event.get("tests_status", "not_run"),
        "report_status": event.get("report_status", "not_run"),
        "scope": event.get("scope") or {}, "settings": settings,
        "artifacts": event.get("artifacts") or {},
    }
    tail = _Tail()
    try:
        env = _environment(plugin, payload)
        tail.secrets = tuple(env[name] for name in plugin.env if env[name])
        cwd = _local_path(root, plugin.cwd, "plugin cwd", directory=True)
        from veha import runner as runner_mod
        proc = subprocess.Popen(
            _argv(root, plugin), cwd=str(cwd), env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1, **runner_mod.popen_group_kwargs())
    except (OSError, LifecycleError) as exc:
        base.summary = _mask_env_text(
            f"запуск не состоялся: {type(exc).__name__}: {exc}", tail.secrets)[:4000]
        return base
    if on_start is not None:
        on_start(proc)

    def pump() -> None:
        try:
            for line in proc.stdout:
                tail.add(line)
        except (ValueError, OSError):
            pass

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        proc.stdin.close()
    except (BrokenPipeError, OSError, ValueError):
        pass
    try:
        code = proc.wait(timeout=plugin.timeout)
    except subprocess.TimeoutExpired:
        note = runner_mod.kill_tree(proc)
        reader.join(PUMP_GRACE_SEC)
        base.duration_ms = int((time.monotonic() - started) * 1000)
        base.summary = f"таймаут {plugin.timeout:g} с; process group остановлена"
        if note:
            base.summary += "; " + _mask_env_text(note, tail.secrets)
        base.output = tail.text()
        return base
    finally:
        if on_start is not None:
            on_start(None)
    group_note = runner_mod.kill_tree(proc)
    reader.join(PUMP_GRACE_SEC)
    base.duration_ms = int((time.monotonic() - started) * 1000)
    base.returncode = code
    base.output = tail.text()
    if group_note:
        base.summary = _mask_env_text(group_note, tail.secrets)
        return base
    lines = [line for line in tail.lines if line.strip()]
    document: Any = None
    if lines:
        try:
            document = json.loads(lines[-1])
        except (ValueError, RecursionError):
            pass
    if not isinstance(document, dict):
        base.summary = "последняя непустая строка stdout не содержит JSON-объект"
        base.output = tail.text()
        return base
    status = document.get("status")
    summary = document.get("summary", "")
    metrics = document.get("metrics", {})
    if status not in ("passed", "failed", "skipped") or not isinstance(summary, str) \
            or not isinstance(metrics, dict):
        base.summary = "JSON результата: status passed|failed|skipped, summary строка, metrics объект"
        base.output = tail.text()
        return base
    base.status = status
    base.summary = _mask_env_text(summary, tail.secrets)[:4000]
    base.metrics = _mask_env_data(metrics, tail.secrets)
    base.output = tail.text()
    if code != 0:
        base.status = "failed"
        if not base.summary:
            base.summary = f"команда завершилась с кодом {code}"
    return base


def run_stage(cfg: Config, snapshots_: Iterable[dict[str, Any]], stage: str,
              event: dict[str, Any], *, on_start: Any = None) -> list[PluginResult]:
    store = StateStore(cfg)
    out = []
    for snapshot in snapshots_:
        if stage not in (snapshot.get("stages") or []):
            continue
        result = execute(cfg.root, snapshot, stage, event, on_start=on_start)
        try:
            store.record(result)
        except LifecycleError as exc:
            result.status = "failed"
            result.summary = "; ".join(filter(None, [result.summary, str(exc)]))
        out.append(result)
    return out


def stage_failed(results: Iterable[PluginResult]) -> bool:
    return any(r.required and r.status != "passed" for r in results)


def recover_pending(cfg: Config) -> list[dict[str, Any]]:
    """Recovery под уже занятым общим OS lock: живой владелец сюда не попадает."""
    directory = state_dir(cfg) / "runs"
    if not directory.is_dir():
        return []
    current = {p.key: p for p in plugins(cfg)}
    recovered = []
    for path in sorted(directory.glob("*.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LifecycleError(f"manifest не прочитан ({path.name}): {type(exc).__name__}")
        if not isinstance(manifest, dict) or manifest.get("state") not in ("running", "recovery_failed", "finish_failed"):
            continue
        saved = manifest.get("plugins") or []
        if not isinstance(saved, list):
            raise LifecycleError(f"manifest {path.name}: plugins не список")
        # Ни один recovery не начинается, пока вся сохранённая реализация не сверена.
        for snapshot in saved:
            plugin, _, expected = _from_snapshot(cfg.root, snapshot)
            if "recovery" not in plugin.stages:
                continue
            actual = current.get(plugin.key)
            if actual is None or _digest_at(cfg.root, actual) != expected \
                    or actual.implementation != plugin.implementation:
                raise LifecycleError(
                    f"recovery {plugin.key}: реализация изменилась или плагин удалён; "
                    "нужен ручной разбор — новый прогон заблокирован")
        event = {
            "run_id": manifest.get("run_id"), "suite": manifest.get("suite"),
            "tests_status": manifest.get("tests_status", "interrupted"),
            "report_status": manifest.get("report_status", "unknown"),
            "scope": manifest.get("scope") or {}, "artifacts": manifest.get("artifacts") or {},
        }
        results = run_stage(cfg, saved, "recovery", event)
        manifest["recovery_results"] = [r.as_dict() for r in results]
        manifest["state"] = "recovery_failed" if stage_failed(results) else "interrupted"
        manifest["recovered_at"] = time.time()
        _atomic_json(path, manifest)
        recovered.append({"run_id": manifest.get("run_id"), "state": manifest["state"]})
        if manifest["state"] == "recovery_failed":
            raise LifecycleError("required recovery завершился неуспешно — новый прогон заблокирован")
    return recovered


def public_state(cfg: Config) -> dict[str, Any]:
    store = StateStore(cfg)
    saved = store.read()["plugins"]
    out = []
    for plugin in plugins(cfg):
        enabled, settings = store.binding(plugin)
        last = saved.get(plugin.key, {}).get("last") if isinstance(saved.get(plugin.key), dict) else None
        out.append({
            "id": plugin.key, "label": plugin.label, "kind": "command",
            "stages": list(plugin.stages), "required": plugin.required,
            "editable": plugin.editable, "enabled": enabled, "settings": settings,
            "settingsSchema": plugin.schema, "suites": list(plugin.suites),
            "timeout": plugin.timeout, "last": last,
            "implementation": plugin.implementation,
            "implementationDigest": _digest_at(cfg.root, plugin),
        })
    pending = []
    directory = state_dir(cfg) / "runs"
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pending.append({"run_id": path.stem, "state": "unreadable"})
                continue
            if isinstance(doc, dict) and doc.get("state") in ("running", "recovery_failed", "finish_failed"):
                pending.append({"run_id": doc.get("run_id"), "suite": doc.get("suite"),
                                "state": doc.get("state")})
    return {"contractVersion": CONTRACT_VERSION, "plugins": out, "pending": pending}
