"""Вкладка «Live»: состояние контейнеров стенда и очередь проверки merge request'ов.

Сети здесь нет: сервер **читает снимки**, которые положил рядом кто-то другой, и не
делает ни одного исходящего запроса. Откуда снимки берутся — дело адаптеров; их пока
нет.

Всё настраивается разделом `live` в `atlas.yaml`; умолчаний с путями и адресами нет:

```yaml
live:
  snapshot_dir: live                  # containers.json, mrs.json, mr_checks.json
  container_logs_dir: logs/containers # <контейнер>.log — хвост по клику
  containers_ttl: 20
  mrs_ttl: 120
```

Это **не** артефакты `partest-atlas`: у агента нет вида ни для состояния контейнеров,
ни для очереди проверки. Поэтому файлы лежат отдельно от `atlas/` и контракт агента не
изображают. У каждого файла один писатель:

* `containers.json`, `mrs.json` — снимки, которые кладёт адаптер; сервер только читает;
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
from typing import Any, Callable, Dict, List, Optional

from veha import redact
from veha.config import Config

CONTAINERS_FILE = "containers.json"
MRS_FILE = "mrs.json"            # пишет адаптер
CHECKS_FILE = "mr_checks.json"   # пишет сервер

_ERROR_RE = re.compile(r"error|exception|fatal|traceback|caused by", re.I)


def _read_json(path: Optional[Path]) -> Dict[str, Any]:
    if path is None or not path.is_file():
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
        self.snapshot_dir = cfg.path(self.section.get("snapshot_dir"))
        self.logs_dir = cfg.path(self.section.get("container_logs_dir"))
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- кэш
    def invalidate(self, key: str = "") -> None:
        if key:
            self._cache.pop(key, None)
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
        return self.snapshot_dir / name if self.snapshot_dir else None

    # ---------------------------------------------------------- контейнеры
    def containers(self, ttl: Optional[float] = None) -> Dict[str, Any]:
        if ttl is None:
            ttl = float(self.section.get("containers_ttl") or 20)

        def produce() -> Dict[str, Any]:
            if self.snapshot_dir is None:
                return {"enabled": False, "containers": [], "total": 0,
                        "down": 0, "restarting": 0,
                        "note": "в atlas.yaml не задан live.snapshot_dir — состояние "
                                "контейнеров не показывается"}
            # Снимок кладёт адаптер; состояние и образ — свободный текст, маскируется.
            doc = redact.strings(_read_json(self._file(CONTAINERS_FILE)),
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
                "at": doc.get("scannedAt") or None,
                "total": len(out),
                "down": sum(1 for x in out if x["down"]),
                "restarting": sum(1 for x in out if x["restarting"]),
                "containers": out,
            }
            if not doc:
                result["note"] = f"снимка нет: {CONTAINERS_FILE} в {self.snapshot_dir.name}/"
            return result

        return self._cached("containers", ttl, produce)

    def container_logs(self, container_id: str, tail: int = 200) -> Dict[str, Any]:
        """Хвост лога контейнера из файла в `live.container_logs_dir`."""
        if self.logs_dir is None:
            return {"lines": [], "note": "в atlas.yaml не задан live.container_logs_dir"}
        safe = re.sub(r"[^a-zA-Z0-9._-]", "", container_id or "")
        path = self.logs_dir / f"{safe}.log"
        if not safe or not path.is_file():
            return {"lines": [], "note": "для этого контейнера лог не снят"}
        tail = max(1, min(int(tail), 5000))
        lines = [redact.text(ln.rstrip()) for ln in
                 path.read_text(encoding="utf-8", errors="replace").splitlines()][-tail:]
        bad = [ln for ln in lines if _ERROR_RE.search(ln)]
        return {"lines": lines, "errors": bad[-40:], "errorCount": len(bad)}

    # ----------------------------------------------------------------- МРы
    def _checks(self) -> Dict[str, dict]:
        doc = _read_json(self._file(CHECKS_FILE))
        return {str(k): v for k, v in (doc.get("checks") or {}).items()}

    def set_mr_check(self, iid: int, status: str, note: str = "") -> Dict[str, dict]:
        """Отметить МР: проверен, ждёт прогона, пропущен.

        Пишется только файл отметок. Список МР — снимок адаптера, и сервер его не
        переписывает: иначе у файла было бы два писателя, и один затирал бы другого.
        """
        path = self._file(CHECKS_FILE)
        if path is None:
            raise RuntimeError("в atlas.yaml не задан live.snapshot_dir — отметку некуда записать")
        with self._lock:
            checks = self._checks()
            checks[str(iid)] = {"status": status, "note": note, "at": time.time()}
            _atomic_write(path, {"checks": checks})
        return checks

    def merge_requests(self, ttl: Optional[float] = None) -> Dict[str, Any]:
        if ttl is None:
            ttl = float(self.section.get("mrs_ttl") or 60)

        def produce() -> Dict[str, Any]:
            if self.snapshot_dir is None:
                return {"enabled": False, "mrs": [],
                        "note": "в atlas.yaml не задан live.snapshot_dir — очереди нет"}
            doc = redact.strings(_read_json(self._file(MRS_FILE)),
                                 skip=frozenset({"iid", "state"}))
            raw = doc.get("mrs") or []
            if not raw:
                return {"enabled": False, "mrs": [],
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
                "at": time.time(),
                "project": doc.get("project") or "",
                "warn": "",
                "opened": sum(1 for m in out if m["state"] == "opened"),
                "merged": sum(1 for m in out if m["state"] == "merged"),
                "unchecked": sum(1 for m in out
                                 if m["state"] == "merged" and not m["check"]),
                "mrs": out,
            }

        return self._cached("mrs", ttl, produce)
