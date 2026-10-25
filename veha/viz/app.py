"""Вебморда «Вехи»: FastAPI отдаёт `/api/*` и статику, сцена рисуется в браузере.

Приложение собирается фабрикой :func:`create_app` над конкретным корнем данных, а не
читает его из окружения при импорте: так один процесс поднимает несколько наборов, а
проверки — свои, не трогая окружение.

Ни одного исходящего запроса: ни шрифтов, ни библиотек, ни телеметрии. Вебморда обязана
работать на машине без интернета, и это проверяется тестами (`tests/test_no_egress.py`).

Защита. Сервер слушает только loopback, но этого мало: страница любого сайта в том же
браузере может слать запросы на `127.0.0.1` (CSRF), а сайт с подменой DNS — читать
ответы (DNS rebinding). Поэтому:

* `Host` — только `127.0.0.1:<порт>` или `localhost:<порт>`;
* изменяющие методы — только с того же источника (`Origin`, иначе `Referer`, если есть);
* тело изменяющего запроса — только `application/json` (своя проверка и
  `strict_content_type` fastapi): форма с чужой страницы не обходит CORS;
* CSP, `X-Frame-Options`, `nosniff` на каждом ответе;
* всё, что меняет состояние или запускает процессы, закрыто режимом просмотра.

Аутентификации нет — это известный пробел (`docs/architecture.md §4.6`).
"""

from __future__ import annotations

import json
import subprocess
from contextlib import asynccontextmanager
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from partest_atlas.artifacts import Scope
from partest_atlas.collect import history as atlas_history
from pydantic import BaseModel, ConfigDict

from veha import __version__, redact
from veha import config as veha_config
from veha import runner as veha_runner
from veha.core import graph as core_graph
from veha.core import sources as core_sources
from veha.viz import assets
from veha.viz.live import Live

STATIC = Path(__file__).resolve().parent / "static"
DEFAULT_PORT = 8770
#: Сколько ждать перезапуска одного кейса, секунд. По истечении убивается всё дерево.
RERUN_TIMEOUT = 600

# Тяжёлое, что нужно только карточке узла: она и так ходит в /api/node/{id}.
_NODE_HEAVY = ("observed", "failedTests")

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

#: Ничего inline: ни скриптов, ни обработчиков, ни стилей в разметке. Стиль из
#: собираемой строками разметки задаётся `data-css="…"` и ставится через CSSOM
#: (`static/api.js`) — так та же страница работает и под CSP сайта со снимком.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; "
       "img-src 'self'; connect-src 'self'; font-src 'self'; object-src 'none'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


class MrCheck(BaseModel):
    iid: int
    status: str = "checked"     # checked | todo | skip
    note: str = ""


class RunRequest(BaseModel):
    # Только набор из `runner.suites` и привязка к МР. Произвольных аргументов pytest
    # здесь нет и не будет: `-p`, `-c`, `--rootdir` из запроса — это выполнение кода.
    # Лишнее поле — ошибка, а не тихий пропуск: клиент должен узнать, что его не слушают.
    model_config = ConfigDict(extra="forbid")

    suite: str
    mr: Optional[int] = None    # прогон в счёт проверки merge request


class RerunRequest(BaseModel):
    nodeid: str


class Server:
    """Состояние одного поднятого набора: конфигурация, область, кэш графа."""

    def __init__(self, root: Path, scope: Optional[Scope] = None, readonly: bool = True):
        self.root = Path(root).resolve()
        self.requested_scope = scope
        self.readonly = readonly
        self.cfg = veha_config.load(self.root)
        self.live = Live(self.cfg)
        self.runner = veha_runner.Runner(self.cfg)
        self._graph: Optional[dict] = None
        self.last_sources: Optional[core_sources.Sources] = None
        self._lock = threading.Lock()
        self.rerun_lock = threading.Lock()
        # Прогон набора и перезапуск кейса ходят на один стенд и пишут в одну область:
        # одновременно их запускать нельзя. Проверка «занято ли» и запуск — под одним
        # замком, иначе два запроса проскочили бы между проверкой и стартом.
        self.exec_lock = threading.Lock()
        self._rerun_proc: Any = None
        # Регистрация процесса перезапуска и остановка сервера — под одним замком с
        # флагом: иначе процесс, запущенный в момент остановки, ускользнул бы.
        self._proc_lock = threading.Lock()
        self.closing = False
        # Чем кончилась последняя запись прогона в историю (`_remember_run`): окно
        # раннера показывает пропуск и сбой. `None` — с запуска сервера не писали.
        self.history_write: Optional[dict] = None

    def remember_rerun(self, proc: Any) -> None:
        """Запомнить процесс перезапуска. Если сервер уже останавливается — убить сразу."""
        with self._proc_lock:
            if proc is not None and self.closing:
                late = proc
            else:
                self._rerun_proc = proc
                late = None
        if late is not None:
            veha_runner.kill_tree(late)

    def shutdown(self) -> None:
        """Сервер останавливается — вместе с ним всё, что он запустил.

        Иначе после Ctrl+C прогон продолжал бы гонять общий стенд без хозяина: ни
        остановить его из вебморды, ни узнать, что он идёт, было бы уже нельзя.
        Повторный вызов безвреден.
        """
        with self._proc_lock:
            self.closing = True
            proc = self._rerun_proc
        self.runner.close()
        if proc is not None and proc.poll() is None:
            veha_runner.kill_tree(proc)

    @property
    def allure_dir(self) -> Path:
        return self.root / "allure-results"

    def sources(self) -> core_sources.Sources:
        return core_sources.load(self.cfg, self.requested_scope)

    def graph(self, refresh: bool = False) -> dict:
        with self._lock:
            if refresh or self._graph is None:
                self.last_sources = self.sources()
                self._graph = core_graph.build(self.last_sources)
            return self._graph

    def drop_graph(self) -> None:
        with self._lock:
            self._graph = None

    @property
    def scope(self) -> Scope:
        a = self.graph()["artifacts"]["scope"]
        return Scope(system=a["system"], env=a["env"])

    @property
    def scope_chosen(self) -> bool:
        return bool(self.graph()["artifacts"]["scopeChosen"])

    def require_writable(self, what: str) -> None:
        if self.readonly:
            raise HTTPException(409, f"режим просмотра: {what} "
                                     "(сервер поднят без --allow-run)")

    def require_scope(self) -> None:
        """Прогон пишет артефакты и историю в область. Если она не выбрана, писать
        некуда: запись создала бы `default/default`, и при следующем старте сервер
        молча выбрал бы её как «по умолчанию»."""
        if not self.scope_chosen:
            raise HTTPException(409, "область не выбрана: на диске несколько областей — "
                                     "поднимите сервер с --system и --env")


def mr_verdict(entry: dict) -> tuple:
    """Отметка МР по прогону — из **кода выхода pytest**, а не из разобранных строк.

    Построчные результаты бывают пусты (`-q` в addopts проекта, плагины, меняющие
    вывод), и «0 упало» по ним выдавало упавший прогон за проверку. Код выхода
    авторитетен (коды pytest — `pytest.ExitCode`):

    * 0 — проверен; но если не прошёл ни один тест (всё пропущено) — `empty`;
    * 1 — тесты упали: `failed`, единственный случай, когда МР «не прошёл»;
    * 2 — прервано (`-x`, Ctrl+C): пока `failed`, вопрос открыт в proposals;
    * 3, 4 — внутренняя ошибка pytest или плагина, ошибка вызова, неверный путь набора:
      это сказано о запуске, а не о коде МР, — `partial`, «не проверен» с причиной;
    * 5 — тестов не собрано: `empty`;
    * остановлен вручную, оборван без итога, неизвестный код — `partial`.
    """
    code = entry.get("returncode")
    passed = entry.get("passed") or 0
    bad = (entry.get("failed") or 0) + (entry.get("broken") or 0)
    reason = ""
    if entry.get("status") == "stopped" or code is None:
        status, reason = "partial", "остановлен вручную"
    elif entry.get("status") == "aborted":
        status, reason = "partial", "оборван: pytest не напечатал итог"
    elif code == 0:
        status = "checked" if passed else "empty"
        reason = "" if passed else "ни один тест не прошёл — все пропущены"
    elif code in (1, 2):
        status = "failed"
    elif code in (3, 4):
        status, reason = "partial", (f"не проверен: ошибка pytest или вызова (код {code})")
    elif code == 5:
        status, reason = "empty", "тестов не собрано"
    else:
        status, reason = "partial", f"не проверен: неизвестный код выхода {code}"
    note = (f"прогон «{entry.get('label')}»: {passed} прошло"
            + (f", {bad} упало" if bad else "")
            + (f" — {reason}" if reason else "")
            + (f" (код выхода {code})" if status == "failed" and not bad else ""))
    # Неполный прогон (`-x`, `--maxfail`, без итога) — так и подписан: «N прошло» в нём
    # описывает часть набора.
    if status in ("checked", "failed", "empty") and not entry.get("complete", True):
        note += " — прогон неполный"
    return status, note


def _origin_of(value: str) -> str:
    parts = urlsplit(value or "")
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def create_app(root: Path, *, scope: Optional[Scope] = None, readonly: bool = True,
               port: int = DEFAULT_PORT) -> FastAPI:
    """Приложение над корнем *root*, которое отвечает на `127.0.0.1:<port>`.

    *readonly* — показывать собранное, но не запускать процессы и ничего не записывать.
    Включён по умолчанию: над набором артефактов без тестов и без стенда кнопка
    «запустить» только вводила бы в заблуждение.
    """
    srv = Server(root, scope=scope, readonly=readonly)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    @asynccontextmanager
    async def lifespan(_app):
        yield
        srv.shutdown()

    app = FastAPI(title="Веха — карта тестирования", version=__version__,
                  docs_url=None, redoc_url=None, openapi_url=None,
                  strict_content_type=True, lifespan=lifespan)
    app.state.veha = srv
    app.state.allowed_hosts = allowed_hosts

    @app.middleware("http")
    async def _guard(request, call_next):
        host = request.headers.get("host", "")
        if host not in allowed_hosts:
            response = PlainTextResponse("недопустимый Host", status_code=400)
        elif request.method not in _SAFE_METHODS and not _same_origin(request, host):
            response = PlainTextResponse("запрос не с этой страницы", status_code=403)
        elif (request.method not in _SAFE_METHODS and _has_body(request)
              and not _is_json(request)):
            # То же, что `strict_content_type` у fastapi, но не зависит от его версии:
            # на fastapi старше 0.132 параметр молча уходит в `**extra`, и защиты нет.
            response = PlainTextResponse("тело принимается только как application/json",
                                         status_code=415)
        else:
            response = await call_next(request)
            # Статику не кэшируем: браузер по ETag отдавал старый atlas.js, и казалось,
            # что правка не приехала.
            if request.url.path.startswith("/static") or request.url.path == "/":
                response.headers["Cache-Control"] = "no-store, must-revalidate"
        for k, v in SECURITY_HEADERS.items():
            response.headers[k] = v
        return response

    def _has_body(request) -> bool:
        length = request.headers.get("content-length")
        return bool(request.headers.get("transfer-encoding")) or (
            length is not None and length.strip() not in ("", "0"))

    def _is_json(request) -> bool:
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        return ctype == "application/json"

    def _same_origin(request, host: str) -> bool:
        """Изменяющий запрос — только со своей страницы. Нет ни `Origin`, ни `Referer`
        — это не браузер (curl, проверки), и CSRF тут не о чем говорить."""
        own = f"http://{host}"
        origin = request.headers.get("origin")
        if origin is not None:
            return origin == own
        referer = request.headers.get("referer")
        if referer is not None:
            return _origin_of(referer) == own
        return True

    @app.get("/api/graph")
    def get_graph(refresh: bool = False) -> dict:
        """Граф для сцены — без того, что читается только в карточке узла."""
        g = srv.graph(refresh=refresh)
        return {
            **{k: v for k, v in g.items() if k not in ("nodes", "tests")},
            "nodes": [
                {**n, "meta": {k: v for k, v in n["meta"].items() if k not in _NODE_HEAVY}}
                for n in g["nodes"]
            ],
        }

    @app.get("/api/artifacts")
    def get_artifacts(refresh: bool = False) -> dict:
        """Что прочитано из артефактов агента, что нет и почему."""
        g = srv.graph(refresh=refresh)
        return {**g["artifacts"], "config": {
            "path": str(srv.cfg.root / veha_config.CONFIG_NAME),
            "exists": srv.cfg.exists, "error": srv.cfg.error}}

    @app.get("/api/node/{node_id:path}")
    def get_node(node_id: str) -> dict:
        g = srv.graph()
        node = next((n for n in g["nodes"] if n["id"] == node_id), None)
        if node is None:
            raise HTTPException(404, f"нет узла {node_id}")
        incoming = [e for e in g["edges"] if e["target"] == node_id]
        outgoing = [e for e in g["edges"] if e["source"] == node_id]
        tests = [t for t in g["tests"] if node_id in t["endpoints"]]
        return {"node": node, "incoming": incoming, "outgoing": outgoing, "tests": tests}

    @app.get("/api/log")
    def get_log(name: str = "", tail: int = 400) -> dict:
        """Хвост лог-файла проекта. Читаются только файлы из раздела `logs:`."""
        allowed = dict(srv.cfg.section("logs"))
        if not allowed:
            return {"files": [], "name": "", "lines": [],
                    "note": "в atlas.yaml не объявлена секция logs"}
        key = name or next(iter(allowed))
        rel = allowed.get(key)
        if rel is None:
            raise HTTPException(404, f"нет лога {key}")
        path = (srv.root / rel).resolve()
        if srv.root not in path.parents and path != srv.root:
            raise HTTPException(400, "путь вне проекта")
        if not path.is_file():
            return {"files": list(allowed), "name": key, "lines": [],
                    "note": f"файла нет: {rel}"}
        size = path.stat().st_size
        if not size:
            return {"files": list(allowed), "name": key, "path": rel, "size": 0,
                    "lines": [], "note": f"файл пуст: {rel} (лог пишется при прогоне)"}
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-max(1, min(tail, 5000)):]
        return {"files": list(allowed), "name": key, "path": rel, "size": size,
                "lines": [redact.text(ln.rstrip()) for ln in lines]}

    @app.get("/api/tests")
    def list_tests(status: str = "", q: str = "", limit: int = 400) -> dict:
        """Список тестов последнего прогона для вкладки «Отчёт»."""
        g = srv.graph()
        run = g.get("run") or {}
        latest = {t["nodeid"] for t in (run.get("failures") or [])}
        tests = g.get("tests") or []
        started = run.get("startedAt") or 0
        rows = [t for t in tests if not started or t.get("start", 0) >= started]
        if status:
            rows = [t for t in rows if t.get("status") == status]
        if q:
            low = q.lower()
            rows = [t for t in rows
                    if low in (t.get("name") or "").lower()
                    or low in (t.get("nodeid") or "").lower()
                    or low in (t.get("feature") or "").lower()]
        rows.sort(key=lambda t: (t.get("status") != "failed", t.get("status") != "broken",
                                 t.get("feature") or "", t.get("name") or ""))
        return {"total": len(rows), "failedKnown": len(latest),
                "tests": rows[:max(1, min(limit, 2000))]}

    @app.get("/api/test")
    def test_detail(nodeid: str = "", file: str = "") -> dict:
        """Полная карточка теста: дерево шагов, вложения, разбор падения."""
        g = srv.graph()
        rec = None
        for t in g.get("tests") or []:
            if (file and t.get("file") == file) or (nodeid and t.get("nodeid") == nodeid):
                rec = t
                break
        if rec is None:
            raise HTTPException(404, "тест не найден в текущем allure-results")
        path = srv.allure_dir / (rec.get("file") or "")
        if not path.is_file():
            raise HTTPException(404, f"нет файла результата: {rec.get('file')}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {
            "test": rec,
            "labels": {lb.get("name"): redact.text(lb.get("value"))
                       for lb in raw.get("labels") or []},
            "parameters": redact.parameters(raw.get("parameters") or []),
            "statusDetails": redact.data(raw.get("statusDetails") or {}),
            "attachments": raw.get("attachments") or [],
            "steps": redact.data(raw.get("steps") or [], depth=64),
            "start": raw.get("start"), "stop": raw.get("stop"),
        }

    @app.get("/api/attachment/{source}")
    def attachment(source: str):
        """Вложение Allure: только простое имя файла внутри allure-results.

        `Path(source).name == source` отсекает разделители, `:` — имена вида `D:x` на
        Windows, которые `Path` склеивает в путь на другом диске. И на всякий случай путь
        после разрешения обязан лежать внутри каталога — как у `/api/log`.
        """
        if not source or Path(source).name != source or ":" in source or ".." in source:
            raise HTTPException(400, "недопустимое имя вложения")
        base = srv.allure_dir.resolve()
        path = (base / source).resolve()
        if path.parent != base:
            raise HTTPException(400, "недопустимое имя вложения")
        if not path.is_file():
            raise HTTPException(404, "вложение не найдено")
        if path.stat().st_size > 4_000_000:
            raise HTTPException(413, "вложение слишком большое для просмотра")
        if source.endswith(".png"):
            return FileResponse(str(path), media_type="image/png")
        text = redact.document(path.read_text(encoding="utf-8", errors="replace"))
        return {"source": source, "size": path.stat().st_size, "text": text[:400_000]}

    # ------------------------------------------------------------------ live
    @app.get("/api/live/containers")
    def live_containers() -> dict:
        return srv.live.containers()

    @app.get("/api/live/container/{cid}/logs")
    def live_container_logs(cid: str, tail: int = 200) -> dict:
        return srv.live.container_logs(cid, tail=tail)

    @app.get("/api/live/mrs")
    def live_mrs() -> dict:
        return srv.live.merge_requests()

    @app.post("/api/live/mr")
    def live_mr_check(req: MrCheck) -> dict:
        """Отметить МР как проверенный или поставить в очередь на прогон."""
        srv.require_writable("отметки МР не записываются")
        if req.status not in ("checked", "todo", "skip", ""):
            raise HTTPException(400, "статус: checked | todo | skip")
        try:
            srv.live.set_mr_check(req.iid, req.status, redact.text(req.note))
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        srv.live.invalidate("mrs")
        return srv.live.merge_requests()

    # ---------------------------------------------------------------- раннер
    def _remember_run(state, scope: Scope) -> None:
        """Записать прогон в историю вместе с состоянием стенда и покрытием.

        Чем кончилась запись — в ``srv.history_write`` (окно раннера показывает его
        строкой): пропуск и сбой не теряются молча. Граф сбрасывается в любом случае —
        и после сбоя: прогон мог переписать артефакты и `coverage.json`.
        """
        try:
            srv.history_write = _write_run(state, scope)
        except Exception as exc:
            srv.history_write = _history_outcome(
                "error", f"запись прогона не удалась: {_error_text(exc)}",
                veha_runner.history_limit_checked(srv.cfg)[1])
        finally:
            srv.drop_graph()

    def _error_text(exc: BaseException) -> str:
        """Причина сбоя для вебморды — без абсолютных путей.

        У ``OSError`` — тип, ``strerror`` и только имя файла: путь называет каталог
        данных и домашний каталог. У прочих — тип и текст через `redact`, с корнем
        данных и домашним каталогом, заменёнными на метки.
        """
        if isinstance(exc, OSError):
            name = Path(str(exc.filename)).name if exc.filename else ""
            why = exc.strerror or "ошибка ввода-вывода"
            return f"{type(exc).__name__}: {why}" + (f" ({name})" if name else "")
        text = redact.text(str(exc))
        for path, mark in ((str(srv.root), "<каталог данных>"),
                           (str(Path.home()), "<домашний каталог>")):
            if path and len(path) > 3:
                text = text.replace(path, mark).replace(path.replace("\\", "/"), mark)
        return f"{type(exc).__name__}: {text[:300]}"

    def _history_outcome(state: str, reason: str = "", note: str = "") -> dict:
        return {"state": state, "reason": reason, "at": time.time(),
                **({"note": note} if note else {})}

    def _write_run(state, scope: Scope) -> dict:
        entry = state.as_dict()
        suite = next((s for s in veha_runner.suites(srv.cfg) if s.key == state.suite), None)
        entry["suiteFull"] = bool(suite and suite.full)
        # Срез покрытия — только если прогон дошёл до конца и coverage.json записан им,
        # а не лежит с прошлого раза: иначе остановленный на середине прогон получил бы
        # в историю чужой процент.
        cov = srv.root / "coverage.json"
        # Дошёл ли прогон до конца — решает итоговая строка pytest и отсутствие
        # прерывания (`-x`, `--maxfail`, ошибка сбора), а не пересчёт строк: под
        # xdist строки другие, и пересчёт молча давал нули.
        finished = state.status == "done" and state.complete
        fresh = cov.is_file() and cov.stat().st_mtime >= state.started
        if finished and fresh:
            try:
                entry["coverage"] = veha_runner.coverage_snapshot(
                    srv.root, suite_full=entry["suiteFull"])
            except Exception as exc:
                # Срез снимает код агента над чужим файлом; не снялся — прогон всё
                # равно записывается, с причиной вместо среза.
                entry["coverage"] = {"note": "срез покрытия не снят: "
                                             f"{type(exc).__name__}"}
        else:
            entry["coverage"] = {"note": "покрытие этим прогоном не записано"}
        try:
            srv.live.invalidate("containers")
            c = srv.live.containers(ttl=0)
            entry["containers"] = {
                "total": c.get("total"), "down": c.get("down"),
                "restarting": c.get("restarting"),
                "bad": [x["name"] for x in (c.get("containers") or [])
                        if x.get("down") or x.get("restarting")],
            }
        except Exception:
            entry["containers"] = {}
        entry.pop("tail", None)

        # Прогон под МР: поднимаем привязку наверх записи и сразу ставим отметку
        # проверки — ради этого кнопка и нужна.
        mr = (entry.get("context") or {}).get("mr")
        if mr and mr.get("iid"):
            entry["mr"] = mr
            status, note = mr_verdict(entry)
            try:
                srv.live.set_mr_check(int(mr["iid"]), status, note)
                srv.live.invalidate("mrs")
            except Exception:
                pass
        # Пока пол partest-atlas ниже 0.2.1: `history.append` агента 0.1–0.2.0 дописывает
        # поверх файла, которого не понял (новее схемы, битый), и уничтожает его записи;
        # с 0.2.1 он такой файл не трогает сам. До подъёма пола файл проверяется здесь.
        # Состояние берётся с диска сейчас, а не из закэшированного графа: файл мог
        # поменяться за время прогона.
        path = scope.path(srv.cfg.artifacts_dir, atlas_history.KIND)
        hist, _ = core_sources.inspect(path, atlas_history.KIND, scope, srv.root)
        if hist.state not in ("ok", "missing"):
            return _history_outcome(
                "skipped", f"файл истории не понят ({hist.state}) — прогон не записан, "
                           "файл не тронут")
        # Предел — только целое ≥ 1 (`history_limit_checked`): с пределом < 1 агент стёр
        # бы историю. Негодное значение — предел по умолчанию и видимая пометка.
        _, limit_note = veha_runner.history_limit_checked(srv.cfg)
        runs = veha_runner.append_history(srv.cfg, scope, entry)
        # Агент 0.2.1 не трогает файл, которого не понял, и не записанный файл
        # (занят, нет места или прав): предупреждение `UnreadableHistory` и пустой
        # список. Предупреждение из потока раннера ловить нечестно — фильтр
        # `warnings` общий на процесс, — поэтому судим по возврату: при пределе ≥ 1
        # записанная история не бывает пустой.
        if not runs:
            return _history_outcome(
                "skipped", "partest-atlas не записал прогон: файл истории не понят или "
                           "не записан, файл не тронут (причина — в выводе сервера)",
                limit_note)
        return _history_outcome("written", note=limit_note)

    @app.get("/api/runner/suites")
    def runner_suites() -> dict:
        arts = srv.graph()["artifacts"]
        hist = next((a for a in arts["artifacts"] if a["kind"] == atlas_history.KIND), {})
        last = srv.last_sources
        history = last.artifacts.payload(atlas_history.KIND) if last else []
        series = veha_runner.trend_series(history)
        current_rule = series[0]["avgRule"] if series else None
        neutral_rule = veha_runner.current_rule(history)
        return {
            "suites": [{"key": s.key, "label": s.label, "args": s.args, "note": s.note,
                        "full": s.full}
                       for s in veha_runner.suites(srv.cfg)],
            "history": [_for_window(h, current_rule, neutral_rule, bool(series))
                        for h in history],
            "historyState": hist.get("state"),
            # Последняя запись прогона в историю: `written`, `skipped` (файл не понят —
            # не тронут) или `error`, с причиной. Состояние файла — `historyState`.
            "historyWrite": srv.history_write,
            # Тренд среднего покрытия — только по полным прогонам: процент частичного
            # прогона посчитан по части набора и с полным несравним. Точки помечены
            # серией opt-in; показывается разложение `trendSeries`, а не одна линия.
            "trend": veha_runner.trend(history),
            "trendSeries": series,
            "readonly": srv.readonly,
            **srv.runner.status(),
        }

    def _for_window(entry: Any, current_rule: Any, neutral_rule: int,
                    has_series: bool) -> Any:
        """Запись истории для окна раннера: к записи opt-in — готовая подпись; к среднему,
        посчитанному не по правилу текущей серии тренда, — пометка правила (``ruleMark``).

        Копия, а не правка: история — артефакт агента, и в нём остаётся то, что
        записано.
        """
        cov = entry.get("coverage") if isinstance(entry, dict) else None
        if not isinstance(cov, dict):
            return entry
        extra: Dict[str, Any] = {}
        if "optin" in cov:
            optin = core_sources.normalize_optin(cov.get("optin"))
            extra["optinText"] = core_sources.optin_phrases(optin)
        if has_series and cov.get("avg") is not None:
            rule = veha_runner.effective_rule(cov, neutral_rule)
            if rule != current_rule:
                extra["ruleMark"] = ("правило не записано" if rule is None
                                     else f"правило {rule}")
        return {**entry, "coverage": {**cov, **extra}} if extra else entry

    def _mr_context(iid: int) -> dict:
        try:
            for mr in (srv.live.merge_requests().get("mrs") or []):
                if mr.get("iid") == iid:
                    return {"iid": iid, "title": mr.get("title"),
                            "branch": mr.get("branch"), "url": mr.get("url"),
                            "state": mr.get("state")}
        except Exception:
            pass
        return {"iid": iid}

    @app.post("/api/runner/start")
    def runner_start(req: RunRequest) -> dict:
        srv.require_writable("прогон не запускается")
        srv.require_scope()
        scope = srv.scope
        context = {"mr": _mr_context(req.mr)} if req.mr else {}
        with srv.exec_lock:
            if srv.rerun_lock.locked():
                raise HTTPException(409, "идёт перезапуск кейса — дождись окончания")
            try:
                srv.runner.start(req.suite, scope=scope, context=context,
                                 on_finish=lambda st: _remember_run(st, scope))
            except RuntimeError as exc:
                raise HTTPException(409, str(exc))
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        return srv.runner.status()

    @app.get("/api/runner/status")
    def runner_status() -> dict:
        """Состояние прогона плюс эндпоинты, которых он уже коснулся."""
        st = srv.runner.status()
        run = st.get("run")
        if run and run.get("touched"):
            by_node: Dict[str, Any] = {}
            for t in (srv.graph().get("tests") or []):
                if t.get("nodeid"):
                    by_node.setdefault(t["nodeid"], t.get("endpoints") or [])
            hit, failed = set(), set()
            for nid in run["touched"]:
                hit.update(by_node.get(nid) or [])
            for nid in run.get("failures") or []:
                failed.update(by_node.get(nid) or [])
            run["touchedEndpoints"] = sorted(hit)[:400]
            run["failedEndpoints"] = sorted(failed)[:200]
        return st

    @app.post("/api/runner/stop")
    def runner_stop() -> dict:
        srv.require_writable("прогон не останавливается")
        stopped = srv.runner.stop()
        return {"stopped": stopped, **srv.runner.status()}

    @app.post("/api/runner/clean")
    def runner_clean() -> dict:
        """Очистить allure-results: иначе прогоны копятся, и сводка склеивает их."""
        srv.require_writable("набор результатов не очищается")
        if srv.runner.busy or srv.rerun_lock.locked():
            raise HTTPException(409, "идёт прогон — очистка может испортить отчёт")
        removed = 0
        if srv.allure_dir.is_dir():
            for f in srv.allure_dir.iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass
        srv.drop_graph()
        return {"removed": removed}

    @app.post("/api/rerun")
    def rerun(req: RerunRequest) -> dict:
        """Перегнать один кейс. Один запуск за раз — прогон трогает общий стенд."""
        srv.require_writable("кейс не перезапускается")
        srv.require_scope()
        nodeid = (req.nodeid or "").strip()
        if not nodeid or ".py::" not in nodeid or nodeid.startswith("-"):
            raise HTTPException(400, "нужен pytest nodeid вида path.py::Class::test")
        target = (srv.root / nodeid.split("::")[0]).resolve()
        if srv.root not in target.parents or not target.is_file():
            raise HTTPException(400, f"нет файла теста: {nodeid.split('::')[0]}")
        with srv.exec_lock:
            if srv.closing:
                raise HTTPException(409, "сервер останавливается")
            if srv.runner.busy:
                raise HTTPException(409, "идёт прогон набора — дождись окончания")
            if not srv.rerun_lock.acquire(blocking=False):
                raise HTTPException(409, "другой кейс уже выполняется — дождись окончания")
        try:
            # Конец — смерть pytest, а не конец пайпа: внук, державший stdout, давал
            # ложный таймаут и занимал замок на всё время ожидания.
            code, lines = veha_runner.run_tree(
                [sys.executable, "-m", "pytest", nodeid, "-q", "-p", "no:cacheprovider"],
                cwd=str(srv.root), env=veha_runner.scope_env(srv.scope),
                timeout=RERUN_TIMEOUT, on_start=srv.remember_rerun)
        except subprocess.TimeoutExpired as exc:
            # Убито всё дерево: тест мог запустить сервис или браузер, и они держали бы
            # и пайп, и стенд. Замок отпускается, только когда дерево мертво.
            note = f" ({exc.output})" if exc.output else ""
            raise HTTPException(504, f"прогон не уложился в {RERUN_TIMEOUT} с — "
                                     f"остановлен вместе с дочерними процессами{note}")
        finally:
            srv.remember_rerun(None)
            srv.rerun_lock.release()
        tail = [redact.text(ln) for ln in lines if ln.strip()][-25:]
        srv.drop_graph()
        return {"nodeid": nodeid, "returncode": code, "passed": code == 0,
                "output": "\n".join(tail)}

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    shots = core_sources.screens_dir(srv.cfg)
    if shots:
        app.mount("/screens", StaticFiles(directory=str(shots)), name="screens")

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        # Та же страница, что у снимка, — с версией по содержимому в адресах статики
        # (`veha.viz.assets`). Считается на каждый запрос: поправленный на диске скрипт
        # сразу получает новый адрес. У сервера кеш и так выключен (`no-store` выше),
        # версия — второй замок и один код со снимком.
        return HTMLResponse(assets.page(STATIC, assets.read_static(STATIC)))

    return app
