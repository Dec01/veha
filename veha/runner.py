"""Раннер прогонов: запуск наборов pytest с живым прогрессом и историей.

Модуль знает только про pytest и про `atlas.yaml`: наборы описываются в разделе
`runner.suites`, поэтому на другом проекте меняется конфигурация, а не код.

Прогресс читается без инструментации: подпроцесс с `-v`, вывод разбирается построчно в
отдельном потоке. pytest печатает `nodeid PASSED [ 12%]`, и этого достаточно, чтобы
показать, где прогон и что уже упало. Визуал может отставать на секунду — тесты от этого
не страдают.

История прогонов — артефакт `runner.history` агента. Своего формата у сервера нет:
читает и дописывает `partest_atlas.collect.history`, в ту же область, что показывается.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from partest_atlas.artifacts import Scope
from partest_atlas.collect import history

from veha import redact
from veha.config import Config
from veha.core.sources import (normalize_optin, optin_phrases, optin_record,
                               optin_signature, partial_reasons)

#: Сколько прогонов держать, если `runner.history_limit` не задан.
HISTORY_LIMIT = 10

# `tests/x.py::TestY::test_z PASSED                          [ 12%]`
# Разбор вывода pytest -v. Формы сверены с настоящим выводом pytest 9.1 и
# pytest-xdist 3.8 (2026-10-06):
#   test_a.py::test_param[POST /orders] PASSED                               [ 16%]
#   test_a.py::test_skip SKIPPED (x)                                         [100%]
#   [gw1] [ 14%] PASSED test_a.py::test_ok
#   collecting ... collected 7 items / 1 deselected / 6 selected
#   2 workers [7 items]
#   ======== 1 failed, 4 passed, 1 skipped, 1 deselected, 1 error in 0.11s ========
#   !!!!!!!!!!!! xdist.dsession.Interrupted: stopping after 1 failures !!!!!!!!!!!!
#
# Разбор процедурный, без регулярок с ленивыми группами вокруг `::`: строка вывода — чужой
# текст, и такая регулярка на строке вида `a::b PASSED a::b PASSED …` давала кубическое
# время, держа GIL всего сервера. Длинные строки результатом не бывают и не разбираются.
STATUSES = ("PASSED", "FAILED", "ERROR", "SKIPPED", "XFAIL", "XPASS")
MAX_RESULT_LINE = 2048
_STATUS_AFTER_NODEID_RE = re.compile(r" (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?= |$)")
_TRAILING_PCT_RE = re.compile(r"\s\[\s{0,3}(\d{1,3})%\]$")
_XDIST_HEAD_RE = re.compile(r"\[gw\d{1,4}\] \[\s{0,3}(\d{1,3})%\] (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) ")
# `collected 7 items / 1 deselected / 6 selected`, `collected 7 items / 1 error`
_COLLECTED_RE = re.compile(r"collected (\d+) items?(?: / (\d+) deselected)?(?: / (\d+) selected)?")
# xdist: строки `collected` нет, есть `2 workers [7 items]` — уже после отбора
_WORKERS_RE = re.compile(r"^\d+ workers? \[(\d+) items?\]")
_DURATION_RE = re.compile(r"[\d.]+s(?: \([^)]{0,64}\))?")
#: Единственная рамка `=== … ===` до результатов. Любая другая (FAILURES, ERRORS,
#: PASSES, warnings summary, short test summary info, рамки плагинов) открывает раздел,
#: где идут трейсы, захваченный вывод теста и пересказ результатов (`-rfE`, `-rP`):
#: `other.py::test_fake PASSED`, напечатанное тестом, — не результат.
SESSION_START_BANNER = "test session starts"

#: Итоговая строка pytest: `1 failed, 2 passed, 2 rerun, 1 error in 0.10s`. Её счётчики
#: авторитетны — построчные результаты бывают неполны (`-q` в addopts проекта снимает
#: строку на каждый тест, плагины вроде pytest-sugar меняют вывод).
_COUNT_RE = re.compile(r"(\d+) ([a-z]+)")
_SUMMARY_KEYS = {
    "passed": "passed", "failed": "failed", "error": "broken", "errors": "broken",
    "skipped": "skipped", "xfailed": "xfailed", "xpassed": "xpassed",
    "rerun": "rerun", "reruns": "rerun", "deselected": "deselected",
}


def parse_counts(summary: str) -> Dict[str, int]:
    """Счётчики из итоговой строки; неизвестные слова (`warnings`) пропускаются."""
    out: Dict[str, int] = {}
    for part in summary.split(","):
        m = _COUNT_RE.fullmatch(part.strip())
        if m and m.group(2) in _SUMMARY_KEYS:
            key = _SUMMARY_KEYS[m.group(2)]
            out[key] = out.get(key, 0) + int(m.group(1))
    return out


def parse_result(line: str):
    """`(nodeid, status, pct)` строки результата или `None`.

    Строка, начинающаяся со слова статуса, результатом не считается: так выглядит
    краткая сводка (`FAILED nodeid - сообщение`), и сообщение в ней — чужой текст.
    """
    if len(line) > MAX_RESULT_LINE or "::" not in line:
        return None
    if line.startswith(STATUSES):
        return None
    m = _XDIST_HEAD_RE.match(line)
    if m:
        nodeid = line[m.end():].strip()
        return (nodeid, m.group(2), int(m.group(1))) if "::" in nodeid else None
    pct = None
    m = _TRAILING_PCT_RE.search(line)
    if m:
        pct = int(m.group(1))
        line = line[:m.start()]
    first = line.find("::")
    m = _STATUS_AFTER_NODEID_RE.search(line, first)
    if m is None:
        return None
    nodeid = line[:m.start()]
    if not nodeid or nodeid[0].isspace():
        return None
    return nodeid, m.group(1), pct


def parse_banner(line: str):
    """Строка вида `==== текст ====`: вернуть текст без рамки или `None`."""
    if len(line) > MAX_RESULT_LINE or not (line.startswith("=") and line.endswith("=")):
        return None
    inner = line.strip("=").strip()
    return inner or None


def parse_summary(inner: str):
    """`1 failed, 4 passed in 0.11s` → `1 failed, 4 passed`; иначе `None`."""
    head, sep, tail = inner.rpartition(" in ")
    if sep and head and _DURATION_RE.fullmatch(tail):
        return head
    return None


def parse_interrupt(line: str):
    """`!!! stopping after 1 failures !!!` → текст; иначе `None`."""
    if len(line) > MAX_RESULT_LINE or not (line.startswith("!!!") and line.endswith("!!!")):
        return None
    what = line.strip("!").strip()
    if "Interrupted" in what or "stopping after" in what or "KeyboardInterrupt" in what:
        return what
    return None


_STATUS_KEY = {
    "PASSED": "passed", "FAILED": "failed", "ERROR": "broken",
    "SKIPPED": "skipped", "XFAIL": "xfailed", "XPASS": "xpassed",
}


@dataclass
class Suite:
    key: str
    label: str
    args: List[str] = field(default_factory=list)
    note: str = ""
    #: Набор гоняет всё. Только такой прогон годится для тренда среднего покрытия:
    #: partest не помечает выбор по путям, а наборы раннера заданы путями.
    full: bool = False


def suites(cfg: Config) -> List[Suite]:
    out: List[Suite] = []
    for key, spec in (cfg.section("runner").get("suites") or {}).items():
        spec = spec or {}
        out.append(Suite(
            key=key,
            label=spec.get("label", key),
            args=list(spec.get("args") or []),
            note=spec.get("note", ""),
            full=bool(spec.get("full")),
        ))
    return out


def history_limit(cfg: Config) -> int:
    return int(cfg.section("runner").get("history_limit") or HISTORY_LIMIT)


#: Подпись состояния прогона для истории и интерфейса.
STATUS_LABEL = {"running": "идёт", "stopping": "останавливается", "stopped": "остановлен",
                "done": "завершён", "aborted": "оборван", "error": "ошибка запуска"}


@dataclass
class RunState:
    """Состояние одного прогона. Читается фронтом по таймеру."""

    id: str
    suite: str
    label: str
    args: List[str]
    started: float
    status: str = "running"          # running | stopping | done | stopped | error
    total: int = 0
    done: int = 0
    passed: int = 0
    failed: int = 0
    broken: int = 0
    skipped: int = 0
    xfailed: int = 0
    xpassed: int = 0
    rerun: int = 0
    #: код выхода pytest: 0 — всё прошло, 1 — есть падения, 5 — тестов не собрано
    returncode: Optional[int] = None
    #: счётчики итоговой строки — авторитетны, когда она есть
    counts: Dict[str, int] = field(default_factory=dict)
    current: str = ""
    finished: float = 0.0
    tail: List[str] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)
    touched: List[str] = field(default_factory=list)   # nodeid, чтобы подсветить сцену
    # Произвольная привязка прогона: зачем его гоняли. Раннер в содержимое не
    # заглядывает — иначе пришлось бы знать про систему контроля версий.
    context: Dict[str, Any] = field(default_factory=dict)
    #: последний статус каждого nodeid: ERROR в teardown после PASSED — это ERROR
    results: Dict[str, str] = field(default_factory=dict)
    pct: int = 0
    #: итоговая строка pytest (`1 failed, 4 passed in 0.11s`) — единственный признак, что
    #: прогон дошёл до конца; пересчитанные строки им быть не могут
    summary: str = ""
    #: почему прогон прерван (`stopping after 1 failures`, `Interrupted: …`)
    interrupted: str = ""
    #: что пошло не так у самого сервера (например, дерево процессов не убито)
    note: str = ""
    #: после любой рамки, кроме начала сессии и итога, результаты больше не разбираются
    results_closed: bool = False

    @property
    def complete(self) -> bool:
        """Прогон дошёл до итога и не был прерван (`-x`, `--maxfail`, ошибка сбора)."""
        return bool(self.summary) and not self.interrupted

    def feed(self, line: str) -> None:
        """Разобрать одну строку вывода pytest -v (обычного и под xdist)."""
        line = line.rstrip()
        if not line:
            return
        self.tail.append(line[:MAX_RESULT_LINE])
        if len(self.tail) > 400:
            del self.tail[:200]

        what = parse_interrupt(line)
        if what is not None:
            self.interrupted = self.interrupted or what
            return
        banner = parse_banner(line)
        if banner is not None:
            summary = parse_summary(banner)
            if summary is not None:
                self.summary = summary
                self.counts = parse_counts(summary)
                self._recount()
            elif not banner.startswith(SESSION_START_BANNER):
                # дальше трейсы, захваченный вывод и пересказ — не результаты
                self.results_closed = True
            return
        if self.results_closed:
            return

        m = _WORKERS_RE.match(line)
        if m:
            self.total = int(m.group(1))
            return
        if line.lstrip().startswith(("collected", "collecting")):
            m = _COLLECTED_RE.search(line)
            if m:
                collected, deselected, selected = m.groups()
                self.total = (int(selected) if selected is not None
                              else int(collected) - int(deselected or 0))
            return

        parsed = parse_result(line)
        if parsed is None:
            return
        nodeid, status, pct = parsed
        if pct is not None:
            self.pct = pct
        previous = self.results.get(nodeid)
        # ERROR (teardown) поверх результата теста — сам тест прошёл, а прогон нет
        if previous is None or status in ("ERROR", "FAILED") or previous == "SKIPPED":
            self.results[nodeid] = status
        self.current = nodeid
        if previous is None:
            self.touched.append(nodeid)
        self._recount()

    def _recount(self) -> None:
        """Счётчики: пока нет итога — по разобранным строкам (для прогресса), с итогом —
        из итоговой строки. Список упавших и подсветка — всегда по строкам."""
        counts = {"passed": 0, "failed": 0, "broken": 0, "skipped": 0,
                  "xfailed": 0, "xpassed": 0}
        failures = []
        for nodeid, status in self.results.items():
            key = _STATUS_KEY.get(status, "passed")
            counts[key] += 1
            if key in ("failed", "broken"):
                failures.append(nodeid)
        self.failures = failures
        self.done = len(self.results)
        if self.counts:
            counts = {k: self.counts.get(k, 0) for k in counts}
            self.rerun = self.counts.get("rerun", 0)
            if not self.results:
                # строк на каждый тест не было (`-q`): сколько прошло — по итогу. Итог
                # считает ошибку teardown отдельно от прошедшего теста, поэтому сумма
                # ограничена числом собранных
                total = sum(counts.values())
                self.done = min(total, self.total) if self.total else total
        self.passed, self.failed = counts["passed"], counts["failed"]
        self.broken, self.skipped = counts["broken"], counts["skipped"]
        self.xfailed, self.xpassed = counts["xfailed"], counts["xpassed"]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "suite": self.suite, "label": self.label,
            "status": self.status, "statusLabel": STATUS_LABEL.get(self.status, self.status),
            "startedAt": self.started,
            "finishedAt": self.finished or None,
            "wallMs": int(((self.finished or time.time()) - self.started) * 1000),
            "total": self.total, "done": self.done, "pct": self.pct,
            "complete": self.complete, "summary": self.summary,
            "interrupted": self.interrupted, "note": self.note,
            "passed": self.passed, "failed": self.failed,
            "broken": self.broken, "skipped": self.skipped,
            "xfailed": self.xfailed, "xpassed": self.xpassed, "rerun": self.rerun,
            "returncode": self.returncode,
            "current": self.current,
            "tail": [redact.text(ln) for ln in self.tail[-40:]],
            "failures": self.failures[-60:],
            "touched": self.touched[-120:],
            "context": dict(self.context),
        }




#: Сколько ждать, пока поток вывода дочитает пайп после смерти процесса. Пайп может
#: держать внук, переживший pytest; прогон из-за него висеть не должен.
PUMP_GRACE_SEC = 2.0


def popen_group_kwargs() -> Dict[str, Any]:
    """Запуск подпроцесса так, чтобы потом можно было убить его вместе с потомками.

    На POSIX — своя сессия (и группа процессов) для `os.killpg`. На Windows — своя
    группа; потомков `taskkill /T` находит по родству.
    """
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def taskkill_path() -> str:
    """`taskkill.exe` по полному пути: имя без пути ищется по PATH и текущему каталогу,
    а текущий каталог раннера — корень тестируемого проекта."""
    system_root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"  # publish-ok: стандартный каталог Windows как запасной, когда SystemRoot не задан
    return os.path.join(system_root, "System32", "taskkill.exe")


def kill_tree(proc: Any) -> str:
    """Убить процесс со всеми потомками. Возвращает пустую строку или объяснение, что
    пошло не так.

    Тест, запустивший сервис или браузер, иначе оставил бы их живыми, а они держали бы
    пайп, порт и стенд. Если дерево убить нечем (нет `taskkill`), убивается хотя бы сам
    процесс — и это говорится, а не замалчивается: остановка не должна ни падать с 500,
    ни висеть в «останавливается».
    """
    pid = getattr(proc, "pid", None)
    if pid is None:
        return ""
    note = ""
    if os.name == "nt":
        try:
            subprocess.run([taskkill_path(), "/T", "/F", "/PID", str(pid)],
                           capture_output=True, check=False, timeout=30)
        except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
            note = (f"дерево процессов не убито ({exc.__class__.__name__}: taskkill "
                    "недоступен) — остановлен только pytest, его потомки могли остаться")
    else:
        import signal  # noqa: PLC0415 — только для POSIX-ветки

        # pid и есть номер группы (start_new_session): `getpgid(pid)` здесь лишний и
        # опасен — если pid уже переиспользован, он вернул бы чужую группу.
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            note = f"группа процессов не убита ({exc}) — остановлен только pytest"
    if note:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=10)
    except Exception:
        pass
    return note


def run_tree(args: List[str], *, cwd: str, env: Dict[str, str], timeout: float,
             on_start: Optional[Callable[[Any], None]] = None) -> Tuple[int, List[str]]:
    """Запустить процесс, дождаться **его смерти** и вернуть код и вывод.

    То же правило, что у раннера: конец — смерть процесса, а не конец пайпа. Внук,
    унаследовавший stdout и переживший родителя, не держит ни ответ, ни замок; вывод
    дочитывается с запасом `PUMP_GRACE_SEC`. По таймауту дерево убивается, пока родитель
    жив, и поднимается `subprocess.TimeoutExpired` с объяснением в `output`.
    """
    proc = subprocess.Popen(
        args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", **popen_group_kwargs())
    if on_start is not None:
        on_start(proc)        # чтобы сервер мог убить дерево при собственной остановке
    lines: List[str] = []

    def pump() -> None:
        try:
            for line in proc.stdout:
                lines.append(line.rstrip("\n"))
        except (ValueError, OSError):
            pass

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        exc.output = kill_tree(proc)
        raise
    reader.join(PUMP_GRACE_SEC)
    return code, list(lines)


class Runner:
    """Один прогон за раз: тесты ходят на общий стенд.

    Прогон считается идущим, пока жив процесс pytest, — не пока открыт его вывод. Внук,
    унаследовавший пайп и переживший родителя, не держит ни замок, ни запись в истории.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._state: Optional[RunState] = None
        self._thread: Optional[threading.Thread] = None
        self._watch: Optional[threading.Thread] = None
        self._on_finish: Optional[Callable[[RunState], None]] = None
        self._stopping = False
        self._closed = False

    def close(self) -> None:
        """Сервер останавливается: новых прогонов нет, идущий убивается с деревом.

        Под тем же замком, что и запуск: прогон, стартующий в эту же секунду, либо
        успеет зарегистрироваться и будет убит, либо не стартует вовсе.
        """
        with self._lock:
            self._closed = True
        self.stop()

    @property
    def busy(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def status(self) -> Dict[str, Any]:
        st = self._state
        return {"busy": self.busy, "run": st.as_dict() if st else None}

    def start(self, suite_key: str, *, scope: Scope,
              context: Optional[Dict[str, Any]] = None,
              on_finish: Optional[Callable[[RunState], None]] = None) -> RunState:
        """Запустить набор из `runner.suites`.

        Аргументы pytest берутся **только** из конфигурации: дописать их из запроса
        значило бы отдать вебморде `-p`, `-c`, `--rootdir` и путь к любому `.py` —
        выполнение произвольного кода на стенде.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("сервер останавливается — прогон не запускается")
            if self.busy:
                raise RuntimeError("прогон уже идёт — дождитесь окончания или остановите")
            suite = next((s for s in suites(self.cfg) if s.key == suite_key), None)
            if suite is None:
                raise ValueError(f"нет набора {suite_key}")

            args = [sys.executable, "-m", "pytest", *suite.args,
                    "-v", "-p", "no:cacheprovider", "--color=no"]
            self._state = RunState(
                id=uuid.uuid4().hex[:12], suite=suite.key, label=suite.label,
                args=args[3:], started=time.time(), context=dict(context or {}),
            )
            self._on_finish = on_finish
            self._stopping = False
            # построчный вывод: без него прогресс приезжает пачкой в конце
            self._proc = subprocess.Popen(
                args, cwd=str(self.cfg.root), env=scope_env(scope), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1, **popen_group_kwargs(),
            )
            self._thread = threading.Thread(target=self._pump, daemon=True)
            self._thread.start()
            self._watch = threading.Thread(target=self._finish, daemon=True)
            self._watch.start()
            return self._state

    def stop(self) -> bool:
        """Остановить прогон вместе со всем, что он запустил."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return False
        self._stopping = True
        if self._state:
            self._state.status = "stopping"
        note = kill_tree(proc)
        if note and self._state:
            self._state.note = note
        return True

    def wait(self, timeout: Optional[float] = None) -> None:
        """Дождаться, пока прогон завершится и запишется (для проверок)."""
        if self._watch is not None:
            self._watch.join(timeout)

    def _pump(self) -> None:
        st, proc = self._state, self._proc
        if st is None or proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                st.feed(line)
        except (ValueError, OSError):
            pass        # пайп закрыт при убийстве дерева

    def _finish(self) -> None:
        """Конец прогона — смерть процесса pytest, а не конец его вывода."""
        st, proc = self._state, self._proc
        if st is None or proc is None:
            return
        code = proc.wait()
        if self._thread is not None:
            self._thread.join(PUMP_GRACE_SEC)
        st.finished = time.time()
        st.returncode = code
        if self._stopping:
            st.status = "stopped"
        else:
            # 1 — тесты упали, 5 — тестов не собрано: это не ошибки запуска
            st.status = "done" if code in (0, 1, 5) else "error"
            if st.status == "done" and not st.summary:
                # pytest умер, не напечатав итога: pytest-timeout в режиме thread
                # завершает процесс через os._exit(1). Это не «завершён», а «оборван»
                st.status = "aborted"
        if self._on_finish:
            try:
                self._on_finish(st)
            except Exception:
                pass


def scope_env(scope: Scope) -> Dict[str, str]:
    """Окружение подпроцесса pytest: область та же, что показывает сервер.

    Плагины агента выбирают, куда писать, по `ATLAS_SYSTEM/ATLAS_ENV`. Без этого прогон,
    запущенный из вебморды, писал бы артефакты в область окружения сервера, а сцена
    читала бы другую.
    """
    return {**os.environ, "ATLAS_SYSTEM": scope.system, "ATLAS_ENV": scope.env}


def append_history(cfg: Config, scope: Scope, entry: dict) -> List[dict]:
    """Дописать прогон в `runner.history` выбранной области через код агента."""
    path = scope.path(cfg.artifacts_dir, history.KIND)
    return history.append(path, entry, limit=history_limit(cfg), scope=scope,
                          collector="veha/runner")


def coverage_snapshot(root: Path, *, suite_full: Optional[bool] = None) -> Dict[str, Any]:
    """Срез покрытия на момент прогона — для тренда по истории.

    Частичный прогон в тренд со средним не кладётся: его процент считан по части набора
    и с полным несравним. Пометка остаётся — что прогон был и почему без среднего.
    Причины — :func:`veha.core.sources.partial_reasons`; набор, не объявленный полным,
    — тоже причина.

    Запись opt-in-наборов (`meta.selection.optin`, partest ≥ 2.5.0) кладётся в срез
    ключом ``optin`` у любого прогона, полного и частичного: по ней тренд не смешивает
    прогоны с разными наборами. Нет ключа в отчёте — нет и в срезе: «не знаем» не
    превращается в «ничего не снято».
    """
    f = Path(root) / "coverage.json"
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}
    snap = history.coverage_snapshot(data)
    meta = data.get("meta") or {}
    reasons = partial_reasons(meta, suite_full=suite_full)
    if reasons:
        snap.pop("avg", None)
        snap["partialRun"] = True
        snap["partialReasons"] = reasons
        # запись opt-in — своим ключом ниже, а не второй копией внутри выбора
        selection = {k: v for k, v in (meta.get("selection") or {}).items()
                     if k != "optin"}
        if selection:
            snap["selection"] = selection
    optin = optin_record(meta)
    if optin is not None:
        snap["optin"] = optin
    return snap


def _series_key(signature: Optional[tuple]) -> str:
    """Имя серии тренда: пусто — прогоны без записи opt-in."""
    if signature is None:
        return ""
    return "optin:" + ";".join(f"{name}={state or ''}" for name, state in signature)


def trend(runs: List[dict]) -> List[Dict[str, Any]]:
    """Среднее покрытие по истории — только полные прогоны со средним.

    Каждая точка помечена серией (``series``) — состоянием opt-in-наборов прогона.
    Точки разных серий в одну линию не сводятся: см. :func:`trend_series`.

    Срез с ``endpoints: 0`` в тренд не идёт: это отчёт, который ничего не посчитал
    (спецификация не загрузилась, `confpartest` не нашёлся), и его ``avg: 0`` — не
    «0 % покрытия», а отсутствие замера. До partest 2.6.0 такой отчёт выходил молча, и
    в истории проектов, где `confpartest` лежал в корне pytest вне `sys.path`, стоят
    нули; с 2.6.0 там появляются числа. Без отсечения тренд нарисовал бы рост с нуля,
    которого не было.
    """
    out = []
    for r in runs or []:
        cov = r.get("coverage") or {}
        if cov.get("partialRun") or cov.get("avg") is None:
            continue
        if cov.get("endpoints") == 0:
            continue
        optin = normalize_optin(cov.get("optin"))
        out.append({"startedAt": r.get("startedAt"), "avg": cov["avg"],
                    "suite": r.get("suite"), "optin": optin,
                    "series": _series_key(optin_signature(optin))})
    return out


def trend_series(runs: List[dict]) -> List[Dict[str, Any]]:
    """Тренд, разложенный на серии по состоянию opt-in-наборов.

    Прогон со снятым набором и прогон с включённым — оба полные (`partialRun` partest
    на opt-in не смотрит), но выполнили разные наборы тестов, и средний процент у них
    про разное. Правило сравнимости — как у `partest.reports.compare`: если запись есть
    у обоих и состояния наборов расходятся, прогоны несравнимы. Поэтому серия — это
    прогоны с одинаковыми состояниями; число снятых тестов серию не делит.

    Серии, а не исключение: ночной прогон с `RUN_SLOW=1` и дневной без него — два
    законных тренда, и выбросить один из них ради другого значит потерять ровно ту
    историю, которую смотрят после правки медленных тестов.

    Прогоны без записи (partest до 2.5.0, проект без наборов) — своя серия. `compare`
    для пары «есть запись / нет записи» ставит только пометку, но в тренде на этой
    границе обычно и появляется набор: до неё медленные тесты шли в общем прогоне,
    после — сняты. Свести их в одну линию значит нарисовать падение, которого не было.

    Первой идёт серия последнего полного прогона с замером (``current``): частичный
    прогон и отчёт, который ничего не посчитал, точек не дают и текущую серию не
    назначают — иначе серия без единой точки стала бы «текущей». Остальные — по
    свежести. ``relation`` — как серия соотносится с текущей: ``differs`` (записи у
    обеих, состояния разные — несравнимы), ``unknown`` (у одной из двух записи нет —
    сопоставимость не подтверждена).
    """
    points = trend(runs)
    if not points:
        return []
    groups: Dict[str, Dict[str, Any]] = {}
    for p in points:
        g = groups.setdefault(p["series"], {"series": p["series"], "points": []})
        g["points"].append({k: p[k] for k in ("startedAt", "avg", "suite")})
        g["optin"] = p["optin"]                      # подпись — по самой свежей точке
    current = points[-1]["series"]
    ordered = sorted(groups.values(), key=lambda g: (g["series"] != current,
                                                     -_last_started(g)))
    for g in ordered:
        g["current"] = g["series"] == current
        g["label"] = "; ".join(optin_phrases(g["optin"], counts=False))
        g["relation"] = ("same" if g["current"]
                         else "unknown" if "" in (g["series"], current)
                         else "differs")
    return ordered


def _last_started(group: Dict[str, Any]) -> float:
    try:
        return float(group["points"][-1].get("startedAt") or 0)
    except (TypeError, ValueError):
        return 0.0
