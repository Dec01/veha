"""Вкладка «Live»: состояние контейнеров стенда и очередь проверки merge request'ов.

Сети здесь нет: сервер **читает снимки**, которые положил рядом адаптер, и не
делает ни одного исходящего запроса.

Всё настраивается разделом `live` в `atlas.yaml`; умолчаний с путями и адресами нет:

```yaml
live:
  snapshot_dir: live                  # current.json или прежние containers/mrs
  container_logs_dir: logs/containers # legacy <контейнер>.log — хвост по клику
  containers_ttl: 20
  mrs_ttl: 120
```

Предпочтительный layout — неизменяемые поколения и один атомарно заменяемый pointer:

```
live/
  current.json                        # {"schemaVersion": 1, "generation": "<id>"}
  generations/<id>/containers.json
  generations/<id>/mrs.json
  generations/<id>/logs/<контейнер>.log
  mr_checks.json                      # отдельный файл сервера, не часть поколения
```

Адаптер сначала полностью собирает поколение в staging внутри корня и только затем
переключает `current.json`. Пока manifest отсутствует, читаются прежние
`containers.json`, `mrs.json` и `live.container_logs_dir`. Если manifest существует,
но недействителен, legacy не используется как запасной вариант: так исключается смесь
поколений. Все пути проверяются на ссылки и выход из корня при каждом чтении.

Это **не** артефакты `partest-atlas`: у агента нет вида ни для состояния контейнеров,
ни для очереди проверки. Поэтому файлы лежат отдельно от `atlas/` и контракт агента не
изображают. У каждого файла один писатель:

* поколения и `current.json` — адаптер; сервер только читает;
* `mr_checks.json` — отметки проверки; их пишет сервер, и только когда запись разрешена
  (`veha serve --allow-run`).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from veha import redact
from veha.config import Config, local_to

CONTAINERS_FILE = "containers.json"
MRS_FILE = "mrs.json"            # пишет адаптер
CHECKS_FILE = "mr_checks.json"   # пишет сервер
CURRENT_FILE = "current.json"    # атомарный pointer адаптера
_GENERATION_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}")

_ERROR_RE = re.compile(r"error|exception|fatal|traceback|caused by", re.I)
#: Внутренний id legacy-раскладки; такой id не может встретиться в manifest.
LEGACY_GENERATION = "@legacy"


class LiveGenerationError(RuntimeError):
    """Явно запрошенное поколение недействительно, удалено или недоступно."""


def _read_json(root: Path, path: Optional[Path]) -> Dict[str, Any]:
    if path is None or not local_to(root, path) or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_write(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Live:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.section = cfg.section("live")
        # Только внутри корня данных, без ссылок по дороге (`Config.local`): иначе
        # `../…` отдавал бы файлы машины; причина — в `meta.reportProblems`.
        self.snapshot_dir, snap_why = cfg.local("live.snapshot_dir",
                                                self.section.get("snapshot_dir"))
        self.logs_dir, logs_why = cfg.local("live.container_logs_dir",
                                            self.section.get("container_logs_dir"))
        self._snap_missing = snap_why or "в atlas.yaml не задан live.snapshot_dir"
        self._logs_missing = logs_why or "в atlas.yaml не задан live.container_logs_dir"
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- кэш
    def invalidate(self, key: str = "") -> None:
        if key:
            self._cache.pop(key, None)
            for cached in list(self._cache):
                if cached.startswith(key + ":"):
                    self._cache.pop(cached, None)
        else:
            self._cache.clear()

    def _cached(self, key: str, ttl: float, produce: Callable[[], Dict[str, Any]]):
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit["at"] < ttl:
            return hit["value"]
        value = produce()
        self._cache[key] = {"at": now, "value": value}
        return value

    def _file(self, name: str) -> Optional[Path]:
        """Файл вне поколений: legacy snapshot либо принадлежащий серверу checks."""
        return self.snapshot_dir / name if self.snapshot_dir else None

    def _layout(self, requested: str = "") -> Tuple[Optional[Path], Optional[Path], str, str]:
        """Разрешить current один раз либо открыть явно закреплённое поколение.

        Последний элемент — id для следующих связанных запросов. Явный id не читает
        pointer повторно: смена current между containers/MRs/logs не смешивает поколения.
        """
        if self.snapshot_dir is None:
            if requested:
                raise LiveGenerationError(f"поколение {requested!r} недоступно: {self._snap_missing}")
            return None, None, self._snap_missing, ""
        if requested:
            if requested == LEGACY_GENERATION:
                return self.snapshot_dir, self.logs_dir, "", LEGACY_GENERATION
            if not _GENERATION_RE.fullmatch(requested):
                raise LiveGenerationError("недействительный id поколения")
            generation_dir = self.snapshot_dir / "generations" / requested
            if not local_to(self.cfg.root, generation_dir) or not generation_dir.is_dir():
                raise LiveGenerationError(f"поколение {requested!r} не найдено или уже удалено")
            return generation_dir, generation_dir / "logs", "", requested

        pointer = self._file(CURRENT_FILE)
        if pointer is None or not local_to(self.cfg.root, pointer):
            return None, None, f"{CURRENT_FILE}: путь идёт через ссылку или вне корня", ""
        if not pointer.exists():
            return self.snapshot_dir, self.logs_dir, "", LEGACY_GENERATION

        manifest = _read_json(self.cfg.root, pointer)
        generation = manifest.get("generation")
        if (type(manifest.get("schemaVersion")) is not int
                or manifest.get("schemaVersion") != 1
                or not isinstance(generation, str)
                or not _GENERATION_RE.fullmatch(generation)):
            return None, None, f"{CURRENT_FILE}: недействительный manifest поколения", ""
        generation_dir = self.snapshot_dir / "generations" / generation
        if not local_to(self.cfg.root, generation_dir) or not generation_dir.is_dir():
            return None, None, f"{CURRENT_FILE}: поколение не найдено или идёт через ссылку", ""
        return generation_dir, generation_dir / "logs", "", generation

    def require_generation(self, generation: str) -> None:
        """Перепроверить явный pin перед cache hit или записью его отметки."""
        if generation:
            self._layout(generation)

    # ---------------------------------------------------------- контейнеры
    def containers(self, ttl: Optional[float] = None,
                   generation: str = "") -> Dict[str, Any]:
        if ttl is None:
            ttl = float(self.section.get("containers_ttl") or 20)
        self.require_generation(generation)

        def produce() -> Dict[str, Any]:
            snapshot_dir, _, layout_error, pinned = self._layout(generation)
            if snapshot_dir is None:
                return {"enabled": False, "containers": [], "total": 0,
                        "down": 0, "restarting": 0, "generation": pinned,
                        "note": f"{layout_error} — состояние контейнеров не показывается"}
            # Снимок кладёт адаптер; состояние и образ — свободный текст, маскируется.
            doc = redact.strings(_read_json(self.cfg.root, snapshot_dir / CONTAINERS_FILE),
                                 skip=frozenset({"container", "name", "state"}))
            keys = {c.key for c in self.cfg.containers}
            out: List[dict] = []
            for entry in doc.get("containers") or []:
                name = entry.get("name") or entry.get("container") or ""
                key = entry.get("container") or self.cfg.container_key(name)
                state = entry.get("state") or "unknown"
                out.append({
                    "id": key or name,
                    "name": name or key,
                    "state": state,
                    "status": entry.get("status") or state,
                    "image": entry.get("image") or "",
                    "up": state == "running",
                    "restarting": state == "restarting",
                    "down": state in ("exited", "dead", "created"),
                    "uptime": entry.get("uptime") or "",
                    "key": key if key in keys else None,
                })
            out.sort(key=lambda x: (not x["down"], not x["restarting"], x["name"]))
            result = {
                "enabled": True,
                "generation": pinned,
                "at": doc.get("scannedAt") or None,
                "total": len(out),
                "down": sum(1 for x in out if x["down"]),
                "restarting": sum(1 for x in out if x["restarting"]),
                "containers": out,
            }
            if not doc:
                result["note"] = f"снимка нет: {CONTAINERS_FILE} в {snapshot_dir.name}/"
            return result

        key = "containers" + (":" + generation if generation else "")
        return self._cached(key, ttl, produce)

    def container_logs(self, container_id: str, tail: int = 200,
                       generation: str = "") -> Dict[str, Any]:
        """Хвост лога именно закреплённого поколения либо legacy-раскладки."""
        if self.snapshot_dir is None and self.logs_dir is None and not generation:
            return {"lines": [], "note": self._logs_missing, "generation": ""}
        _, logs_dir, layout_error, pinned = self._layout(generation)
        if layout_error:
            return {"lines": [], "note": layout_error, "generation": pinned}
        if logs_dir is None:
            return {"lines": [], "note": self._logs_missing, "generation": pinned}
        safe = re.sub(r"[^a-zA-Z0-9._-]", "", container_id or "")
        path = logs_dir / f"{safe}.log"
        if not safe or not local_to(self.cfg.root, path) or not path.is_file():
            return {"lines": [], "note": "для этого контейнера лог не снят",
                    "generation": pinned}
        tail = max(1, min(int(tail), 5000))
        lines = [redact.text(ln.rstrip()) for ln in
                 path.read_text(encoding="utf-8", errors="replace").splitlines()][-tail:]
        bad = [ln for ln in lines if _ERROR_RE.search(ln)]
        return {"lines": lines, "errors": bad[-40:], "errorCount": len(bad),
                "generation": pinned}

    # ----------------------------------------------------------------- МРы
    def _checks(self) -> Dict[str, dict]:
        doc = _read_json(self.cfg.root, self._file(CHECKS_FILE))
        return {str(k): v for k, v in (doc.get("checks") or {}).items()}

    def set_mr_check(self, iid: int, status: str, note: str = "") -> Dict[str, dict]:
        """Отметить МР: проверен, ждёт прогона, пропущен.

        Пишется только файл отметок вне поколений. Список МР — снимок адаптера, и сервер
        его не переписывает: иначе у файла было бы два писателя, и один затирал бы другого.
        """
        path = self._file(CHECKS_FILE)
        if path is None:
            raise RuntimeError(f"{self._snap_missing} — отметку некуда записать")
        if not local_to(self.cfg.root, path):
            raise RuntimeError(f"{CHECKS_FILE}: путь идёт через ссылку или вне корня")
        with self._lock:
            checks = self._checks()
            checks[str(iid)] = {"status": status, "note": note, "at": time.time()}
            _atomic_write(path, {"checks": checks})
        return checks

    def merge_requests(self, ttl: Optional[float] = None,
                       generation: str = "") -> Dict[str, Any]:
        if ttl is None:
            ttl = float(self.section.get("mrs_ttl") or 60)
        self.require_generation(generation)

        def produce() -> Dict[str, Any]:
            snapshot_dir, _, layout_error, pinned = self._layout(generation)
            if snapshot_dir is None:
                return {"enabled": False, "mrs": [], "generation": pinned,
                        "note": f"{layout_error} — очереди нет"}
            doc = redact.strings(_read_json(self.cfg.root, snapshot_dir / MRS_FILE),
                                 skip=frozenset({"iid", "state"}))
            raw = doc.get("mrs") or []
            if not raw:
                return {"enabled": False, "mrs": [], "generation": pinned,
                        "note": f"в {MRS_FILE} нет списка merge request'ов"}
            checks = self._checks()
            out = []
            for mr in raw:
                chk = checks.get(str(mr.get("iid"))) or {}
                out.append({
                    "iid": mr.get("iid"),
                    "title": mr.get("title"),
                    "state": mr.get("state"),
                    "author": mr.get("author"),
                    "url": mr.get("url"),
                    "branch": mr.get("branch"),
                    "updated": mr.get("updated"),
                    "merged": mr.get("merged"),
                    "check": chk.get("status") or "",
                    "checkNote": chk.get("note") or "",
                })
            out.sort(key=lambda m: (m["state"] != "opened", m.get("updated") or ""))
            return {
                "enabled": True,
                "generation": pinned,
                "at": time.time(),
                "project": doc.get("project") or "",
                "warn": "",
                "opened": sum(1 for m in out if m["state"] == "opened"),
                "merged": sum(1 for m in out if m["state"] == "merged"),
                "unchecked": sum(1 for m in out
                                 if m["state"] == "merged" and not m["check"]),
                "mrs": out,
            }

        key = "mrs" + (":" + generation if generation else "")
        return self._cached(key, ttl, produce)
