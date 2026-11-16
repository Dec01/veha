"""Пути к отчётам прогона — раздел `reports` в `atlas.yaml`.

`coverage.json` и `allure-results` лежат не обязательно в корне данных: проект говорит,
куда их кладёт его прогон (`reports.coverage`, `reports.allure`), — относительно корня
или абсолютным путём. Без раздела — прежняя раскладка корня. Негодное значение не
подменяется умолчанием, а называется; очистка Allure вне корня не трогает чужое.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from tests.conftest import artifact_path, make_client

RESULT = {
    "uuid": "r1", "fullName": "src.api.tests.orders.test_list.TestList#test_open",
    "name": "test_open", "status": "passed", "start": 1_700_000_000_000,
    "stop": 1_700_000_001_000,
    "attachments": [{"name": "ответ", "source": "0a100000-0000-4000-8000-000000000001-attachment.txt", "type": "text/plain"}],
}


def _reports(root: Path, body: str) -> None:
    """Дописать раздел `reports` в `atlas.yaml` корня."""
    cfg = root / "atlas.yaml"
    text = cfg.read_text(encoding="utf-8")
    cfg.write_text(text.rstrip("\n") + "\n\nreports:\n" + body, encoding="utf-8")


def _allure(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "0e100000-0000-4000-8000-000000000001-result.json").write_text(json.dumps(RESULT, ensure_ascii=False),
                                      encoding="utf-8")
    (d / "0a100000-0000-4000-8000-000000000001-attachment.txt").write_text("тело ответа", encoding="utf-8")
    return d


def _meta(root: Path) -> dict:
    from veha.core.graph import build_root

    return build_root(root)["meta"]


# ---------------------------------------------------------------- пути


def test_defaults_are_the_root_layout(small_root):
    from veha import config

    cfg = config.load(small_root)
    assert cfg.coverage_file == small_root.resolve() / "coverage.json"
    assert cfg.allure_dir == small_root.resolve() / "allure-results"
    assert cfg.report_problems() == []
    m = _meta(small_root)
    assert m["hasCoverage"] and not m["hasAllure"] and m["reportProblems"] == []


def test_relative_paths_are_from_the_root(small_root):
    from veha import config

    out = small_root / "out"
    out.mkdir()
    shutil.move(str(small_root / "coverage.json"), str(out / "cov.json"))
    _allure(out / "allure")
    _reports(small_root, "  coverage: out/cov.json\n  allure: out/allure\n")

    cfg = config.load(small_root)
    assert cfg.coverage_file == small_root.resolve() / "out" / "cov.json"
    assert cfg.allure_dir == small_root.resolve() / "out" / "allure"
    m = _meta(small_root)
    assert m["hasCoverage"] and m["hasAllure"] and m["tests"] == 1
    assert m["reportProblems"] == []


def test_absolute_paths_outside_the_root_win_over_the_root(small_root, tmp_path):
    """Корень держит прежние отчёты, конфигурация указывает в другое место — читается
    указанное, а не то, что лежит в корне по умолчанию."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    doc = json.loads((small_root / "coverage.json").read_text(encoding="utf-8"))
    doc["endpoints"] = doc["endpoints"][:3]
    (elsewhere / "coverage.json").write_text(json.dumps(doc, ensure_ascii=False),
                                             encoding="utf-8")
    _allure(elsewhere / "allure")
    _allure(small_root / "allure-results")          # чужой прогон в корне
    (small_root / "allure-results" / "r2-result.json").write_text(
        json.dumps({**RESULT, "uuid": "r2", "name": "test_other",
                    "fullName": RESULT["fullName"].replace("test_open", "test_other")}),
        encoding="utf-8")
    _reports(small_root, f"  coverage: {(elsewhere / 'coverage.json').as_posix()}\n"
                         f"  allure: {(elsewhere / 'allure').as_posix()}\n")

    from veha.core.graph import build_root

    g = build_root(small_root)
    assert g["meta"]["tests"] == 1
    measured = [n for n in g["nodes"] if n["kind"] == "endpoint"
                and n["meta"].get("coverage") is not None]
    assert 0 < len(measured) <= 3


def test_missing_configured_reports_are_just_missing(small_root, capsys):
    """Путь задан, файла нет — то же состояние, что «нет coverage.json» в корне, и при
    старте названы ключ и путь."""
    from veha.cli import _report
    from veha.core.graph import build_root

    _reports(small_root, "  coverage: nowhere/coverage.json\n  allure: nowhere/allure\n")
    g = build_root(small_root)
    assert not g["meta"]["hasCoverage"] and not g["meta"]["hasAllure"]
    assert g["meta"]["reportProblems"] == []
    _report(g, small_root)
    out = capsys.readouterr().out
    assert "! нет coverage.json (reports.coverage)" in out
    assert "! нет allure-results (reports.allure)" in out
    assert "coverage.json (reports.coverage) → " in out


@pytest.mark.parametrize("body, needle", [
    ("  coverage: 12\n", "reports.coverage: ожидалась строка пути, получено int"),
    ("  coverage: [a, b]\n", "reports.coverage: ожидалась строка пути, получено list"),
    ('  coverage: ""\n', "reports.coverage: пустой путь"),
    # `\t` и `\n` в двойных кавычках YAML — табуляция и перевод строки посреди пути
    # publish-ok: условный путь Windows для проверки диагностики управляющих символов YAML
    ('  coverage: "C:\\temp\\new.json"\n',
     "reports.coverage: управляющий символ в пути — путь Windows в двойных кавычках? "
     "пишите C:/… или в одинарных кавычках"),
    # publish-ok: заведомо несуществующие UNC-пути для проверки отказа до обращения к сети
    *[(f"  coverage: '{unc}'\n",
       "reports.coverage: сетевой путь (UNC) не читается — открыть его значит обратиться "
       "по сети; отчёт кладут на локальный диск")
      for unc in (r"\\host\share\coverage.json", "//host/share/coverage.json",  # publish-ok: вымышленные UNC-пути
                  r"\\?\C:\coverage.json", r"\\.\pipe\x", r"/\host\share")],  # publish-ok: продолжение списка вымышленных путей
    ("  coverage: 'C:coverage.json'\n",
     "reports.coverage: диск без косой черты (C:отчёт) уводит из корня; пишите C:/…"),
])
def test_bad_value_is_named_not_replaced_by_the_default(small_root, body, needle):
    """Негодный путь не подменяется умолчанием: coverage.json в корне есть, но проект
    указал другое — показать корневой значило бы показать не тот прогон."""
    from veha import config

    _reports(small_root, body)
    cfg = config.load(small_root)
    assert cfg.error == "" and cfg.coverage_file is None
    assert cfg.allure_dir == small_root.resolve() / "allure-results"   # соседний ключ цел
    m = _meta(small_root)
    assert not m["hasCoverage"] and m["reportProblems"] == [needle]


def test_reports_not_a_mapping(small_root):
    from veha import config

    cfg_path = small_root / "atlas.yaml"
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8") + "\nreports: out\n",
                        encoding="utf-8")
    cfg = config.load(small_root)
    assert cfg.coverage_file is None and cfg.allure_dir is None
    assert len(cfg.report_problems()) == 2
    assert cfg.report_problems()[0].startswith("reports: ожидался словарь")


def test_wrong_kind_and_broken_json_are_named_and_do_not_crash(small_root):
    (small_root / "covdir").mkdir()
    (small_root / "allure.txt").write_text("не каталог", encoding="utf-8")
    _reports(small_root, "  coverage: covdir\n  allure: allure.txt\n")
    m = _meta(small_root)
    assert m["reportProblems"] == [
        "coverage.json (reports.coverage): путь указывает на каталог, а нужен файл",
        "allure-results (reports.allure): путь указывает на файл, а нужен каталог",
    ]
    assert not m["hasCoverage"] and not m["hasAllure"]


def test_coverage_that_is_not_json_is_a_state_not_a_500(small_root):
    (small_root / "coverage.json").write_text("{не json", encoding="utf-8")
    c = make_client(small_root)
    r = c.get("/api/graph")
    assert r.status_code == 200
    meta = r.json()["meta"]
    assert not meta["hasCoverage"]
    assert meta["reportProblems"] == ["coverage.json: не прочитан как JSON (JSONDecodeError)"]
    assert str(small_root) not in json.dumps(meta, ensure_ascii=False)


def test_agent_parses_the_file_with_reports(small_root):
    """`atlas.yaml` разбирает `partest-atlas`: раздел Вехи он пропускает, не падая."""
    from partest_atlas import config as atlas_config

    _reports(small_root, "  coverage: out/cov.json\n  allure: out/allure\n")
    cfg = atlas_config.load(small_root)
    assert cfg.raw["reports"] == {"coverage": "out/cov.json", "allure": "out/allure"}
    assert cfg.artifacts_dir == small_root / "atlas"


# ---------------------------------------------------------------- вебморда


def test_test_card_and_attachment_come_from_the_configured_dir(small_root, tmp_path):
    _allure(tmp_path / "ci" / "allure")
    _reports(small_root, f"  allure: {(tmp_path / 'ci' / 'allure').as_posix()}\n")
    c = make_client(small_root)
    assert c.get("/api/test", params={"file": "0e100000-0000-4000-8000-000000000001-result.json"}).status_code == 200
    r = c.get("/api/attachment/0a100000-0000-4000-8000-000000000001-attachment.txt")
    assert r.status_code == 200 and r.json()["text"] == "тело ответа"


def test_bad_allure_value_gives_404_not_500(small_root):
    _reports(small_root, "  allure: 7\n")
    c = make_client(small_root)
    assert c.get("/api/attachment/0a100000-0000-4000-8000-000000000001-attachment.txt").status_code == 404


# ---------------------------------------------------------------- раннер


class WritesCoverage:
    """pytest проекта, который пишет отчёт покрытия туда, куда настроен проект."""

    target: Path = Path()
    source: bytes = b""

    def __init__(self, args, **kwargs):
        WritesCoverage.target.parent.mkdir(parents=True, exist_ok=True)
        WritesCoverage.target.write_bytes(WritesCoverage.source)
        self.stdout = io.StringIO(
            "collecting ... collected 1 item\n"
            "t.py::test_x PASSED                                       [100%]\n"
            "============================== 1 passed in 0.01s ==============================\n")

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


def _wait_for(path: Path, before: bytes, timeout: float = 5.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if path.is_file() and path.read_bytes() != before:
            return
        time.sleep(0.05)


def _run_full(root: Path) -> dict:
    hist = artifact_path(root, "runner.history")
    before = hist.read_bytes()
    c = make_client(root, readonly=False)
    assert c.post("/api/runner/start", json={"suite": "full"}).status_code == 200
    _wait_for(hist, before)
    return json.loads(hist.read_text(encoding="utf-8"))["runs"][-1]


def test_runner_reads_the_coverage_its_run_wrote_at_the_configured_path(
        small_root, tmp_path, monkeypatch):
    from veha import runner

    target = tmp_path / "ci" / "reports" / "coverage.json"
    WritesCoverage.target = target
    WritesCoverage.source = (small_root / "coverage.json").read_bytes()
    (small_root / "coverage.json").unlink()            # в корне отчёта нет
    monkeypatch.setattr(runner.subprocess, "Popen", WritesCoverage)
    _reports(small_root, f"  coverage: {target.as_posix()}\n")

    last = _run_full(small_root)
    assert last["suite"] == "full" and last["coverage"]["avg"] == 66.05
    assert not (small_root / "coverage.json").exists()


def test_runner_does_not_take_the_root_file_when_another_path_is_configured(
        small_root, monkeypatch):
    """Прогон записал отчёт в корень, а проект указал другое место — срез не снят."""
    from veha import runner

    WritesCoverage.target = small_root / "coverage.json"
    WritesCoverage.source = (small_root / "coverage.json").read_bytes()
    monkeypatch.setattr(runner.subprocess, "Popen", WritesCoverage)
    _reports(small_root, "  coverage: out/coverage.json\n")

    assert _run_full(small_root)["coverage"] == {"note": "покрытие этим прогоном не записано"}


class WritesCoverageInThePast(WritesCoverage):
    """pytest, чей отчёт получает mtime раньше момента запуска: так бывает на Windows,
    где время изменения файла и `time.time()` идут с разной гранулярностью."""

    def __init__(self, args, **kwargs):
        super().__init__(args, **kwargs)
        past = time.time_ns() - 60 * 10**9
        os.utime(WritesCoverage.target, ns=(past, past))


def test_runner_takes_its_own_report_even_with_an_mtime_before_the_start(
        small_root, tmp_path, monkeypatch):
    """Свежесть — по отпечатку файла до запуска, а не по часам: отчёт, который прогон
    записал, берётся, даже если его mtime меньше `started` (была нестабильность на 3.14:
    `KeyError: 'avg'` примерно раз на 40 прогонов)."""
    from veha import runner

    target = tmp_path / "ci" / "coverage.json"
    WritesCoverage.target = target
    WritesCoverage.source = (small_root / "coverage.json").read_bytes()
    monkeypatch.setattr(runner.subprocess, "Popen", WritesCoverageInThePast)
    _reports(small_root, f"  coverage: {target.as_posix()}\n")

    assert _run_full(small_root)["coverage"]["avg"] == 66.05


class WritesNothing(WritesCoverage):
    def __init__(self, args, **kwargs):
        self.stdout = io.StringIO(
            "collecting ... collected 1 item\n"
            "t.py::test_x PASSED                                       [100%]\n"
            "============================== 1 passed in 0.01s ==============================\n")


def test_runner_ignores_an_untouched_report_even_with_a_future_mtime(small_root, monkeypatch):
    """Обратное: отчёт с прошлого раза, которого прогон не касался, не берётся, даже если
    его mtime позже запуска (часы сдвинуты, файл скопирован с другой машины)."""
    from veha import runner

    cov = small_root / "coverage.json"
    future = time.time_ns() + 3600 * 10**9
    os.utime(cov, ns=(future, future))
    monkeypatch.setattr(runner.subprocess, "Popen", WritesNothing)

    assert _run_full(small_root)["coverage"] == {"note": "покрытие этим прогоном не записано"}


def test_run_state_wrote_compares_signatures(tmp_path):
    from veha.runner import RunState, file_signature

    f = tmp_path / "coverage.json"
    assert file_signature(f) is None and file_signature(None) is None
    absent = RunState(id="a", suite="s", label="s", args=[], started=0.0)
    assert not absent.wrote(f)                         # не было и нет
    f.write_text("{}", encoding="utf-8")
    assert absent.wrote(f)                             # появился
    before = RunState(id="b", suite="s", label="s", args=[], started=0.0,
                      coverage_before=file_signature(f))
    assert not before.wrote(f)                         # не тронут
    f.write_text('{"a": 1}', encoding="utf-8")
    assert before.wrote(f)                             # изменился
    assert file_signature(tmp_path) is None            # каталог — не отчёт


def test_runner_with_a_bad_coverage_value_says_so(small_root, monkeypatch):
    from veha import runner

    WritesCoverage.target = small_root / "coverage.json"
    WritesCoverage.source = (small_root / "coverage.json").read_bytes()
    monkeypatch.setattr(runner.subprocess, "Popen", WritesCoverage)
    _reports(small_root, "  coverage: 1\n")

    note = _run_full(small_root)["coverage"]["note"]
    assert "reports.coverage" in note and "негоден" in note


# ---------------------------------------------------------------- очистка


def test_clean_outside_the_root_touches_nothing(small_root, tmp_path):
    foreign = _allure(tmp_path / "ci" / "allure")
    _reports(small_root, f"  allure: {foreign.as_posix()}\n")
    c = make_client(small_root, readonly=False)
    r = c.post("/api/runner/clean")
    assert r.status_code == 409 and "вне корня" in r.json()["detail"]
    assert sorted(p.name for p in foreign.iterdir()) == ["0a100000-0000-4000-8000-000000000001-attachment.txt", "0e100000-0000-4000-8000-000000000001-result.json"]


def test_clean_never_empties_the_root_itself(small_root):
    _reports(small_root, "  allure: .\n")
    before = sorted(p.name for p in small_root.iterdir())
    c = make_client(small_root, readonly=False)
    assert c.post("/api/runner/clean").status_code == 409
    assert sorted(p.name for p in small_root.iterdir()) == before


def test_clean_inside_the_root_keeps_the_coverage_report(small_root):
    out = _allure(small_root / "out")
    shutil.move(str(small_root / "coverage.json"), str(out / "coverage.json"))
    _reports(small_root, "  coverage: out/coverage.json\n  allure: out\n")
    c = make_client(small_root, readonly=False)
    r = c.post("/api/runner/clean")
    assert r.status_code == 200 and r.json()["removed"] == 2
    assert [p.name for p in out.iterdir()] == ["coverage.json"]


def test_clean_with_the_default_dir_still_works(small_root):
    d = _allure(small_root / "allure-results")
    c = make_client(small_root, readonly=False)
    r = c.post("/api/runner/clean")
    assert r.status_code == 200 and r.json()["removed"] == 2
    assert list(d.iterdir()) == []


# ---------------------------------------------------------------- снимок


def test_export_reads_allure_at_the_configured_path(small_root, tmp_path):
    from veha.viz.export import export_static

    _allure(tmp_path / "ci" / "allure")
    _reports(small_root, f"  allure: {(tmp_path / 'ci' / 'allure').as_posix()}\n")
    out = tmp_path / "site"
    rep = export_static(small_root, out)
    assert rep.counts.get("tests") == 1 and rep.counts.get("attachments") == 1
    assert (out / "api" / "test" / "0e100000-0000-4000-8000-000000000001-result.json").is_file()
    att = json.loads((out / "api" / "attachment" / "0a100000-0000-4000-8000-000000000001-attachment.txt.json")
                     .read_text(encoding="utf-8"))
    assert att["text"] == "тело ответа"


# ------------------------------------------------- раунд исправлений по ревью


def test_windows_drive_path_on_posix_is_named(small_root, monkeypatch):
    from veha import config

    monkeypatch.setattr(config, "_WINDOWS", False)
    _reports(small_root, "  coverage: 'C:/ci/coverage.json'\n")
    assert config.load(small_root).report_problems() == [
        "reports.coverage: путь Windows; на этой машине он не абсолютный"]


def test_unknown_key_in_reports_is_named(small_root):
    _reports(small_root, "  coverge: out/coverage.json\n")
    m = _meta(small_root)
    assert m["reportProblems"] == [
        "reports.coverge: неизвестный ключ (есть coverage, allure)"]
    assert m["hasCoverage"]          # умолчание в корне по-прежнему читается


def test_missing_allure_dir_is_not_called_cleared(small_root, capsys):
    """Каталога нет — не «очищены»: путь никуда не ведёт, и это сказано без пути машины."""
    from veha.cli import _report
    from veha.core.graph import build_root

    g = build_root(small_root)                      # каталога по умолчанию нет
    assert g["collected"]["reportsCleared"] is False
    assert g["collected"]["reportsMissing"] == "каталога allure-results нет"

    _reports(small_root, "  allure: nowhere/allure\n")
    g = build_root(small_root)
    assert g["collected"]["reportsCleared"] is False
    assert g["collected"]["reportsMissing"] == "по пути reports.allure каталога нет"
    _report(g, small_root)
    out = capsys.readouterr().out
    assert "очищены" not in out
    if g["collected"]["dbEffects"]:
        assert "! по пути reports.allure каталога нет — записи агента не с чем соединить" in out

    (small_root / "nowhere" / "allure").mkdir(parents=True)
    g = build_root(small_root)
    assert g["collected"]["reportsCleared"] is True and g["collected"]["reportsMissing"] == ""


def test_clean_in_a_directory_shared_with_the_project_removes_only_allure_files(small_root):
    """`reports.allure: tests` — каталог тестов проекта: очистка не трогает conftest.py,
    модули тестов, отчёт покрытия и подкаталоги."""
    d = _allure(small_root / "tests")
    project = ["conftest.py", "test_orders.py", "coverage.json", "notes-attachment-list.md",
               # похоже на Allure, но без UUID в начале — файлы проекта
               "expected-result.json", "conftest-attachment.py", "a1-attachment.txt",
               # их кладёт пользователь или плагин, не прогон
               "executor.json", "environment.properties", "categories.json"]
    for name in project:
        (d / name).write_text("# проект\n", encoding="utf-8")
    allure = ["0c100000-0000-4000-8000-000000000001-container.json",
              "0a200000-0000-4000-8000-000000000002-attachment",
              "0a300000000040008000000000000003-attachment.png"]
    for name in allure:
        (d / name).write_text("{}", encoding="utf-8")
    (d / "sub").mkdir()
    (d / "sub" / "0e900000-0000-4000-8000-000000000009-result.json").write_text("{}", encoding="utf-8")
    _reports(small_root, "  allure: tests\n")
    c = make_client(small_root, readonly=False)
    r = c.post("/api/runner/clean")
    assert r.status_code == 200 and r.json()["removed"] == 2 + len(allure)
    assert sorted(p.name for p in d.iterdir()) == sorted(project + ["sub"])
    assert (d / "sub" / "0e900000-0000-4000-8000-000000000009-result.json").is_file()


def _shared_root(small_root: Path) -> None:
    """`reports.allure: .`: результат лежит в корне и называет вложениями файлы корня."""
    # publish-ok: заведомо несуществующий пароль для проверки запрета отдачи .env
    (small_root / ".env").write_text("DB_PASSWORD=hunter2\n", encoding="utf-8")
    result = dict(RESULT, attachments=[
        {"name": "ответ", "source": "0a100000-0000-4000-8000-000000000001-attachment.txt"},
        {"name": "конфиг", "source": "atlas.yaml"},
        {"name": "окружение", "source": ".env"},
        {"name": "модуль", "source": "conftest-attachment.py"},
    ])
    (small_root / "0e100000-0000-4000-8000-000000000001-result.json").write_text(json.dumps(result, ensure_ascii=False),
                                               encoding="utf-8")
    (small_root / "0a100000-0000-4000-8000-000000000001-attachment.txt").write_text("тело ответа", encoding="utf-8")
    (small_root / "0a900000-0000-4000-8000-000000000009-attachment.txt").write_text("никем не назван", encoding="utf-8")
    _reports(small_root, "  allure: .\n")


def test_attachment_is_served_only_if_a_test_names_it_as_an_allure_attachment(small_root):
    _shared_root(small_root)
    c = make_client(small_root)
    assert c.get("/api/attachment/0a100000-0000-4000-8000-000000000001-attachment.txt").json()["text"] == "тело ответа"
    (small_root / "conftest-attachment.py").write_text("SECRET = 1\n", encoding="utf-8")
    for name in ("atlas.yaml", ".env", "conftest-attachment.py", "0a900000-0000-4000-8000-000000000009-attachment.txt", "coverage.json"):
        assert c.get(f"/api/attachment/{name}").status_code == 404, name


def test_export_takes_only_named_allure_attachments(small_root, tmp_path):
    from veha.viz.export import export_static

    _shared_root(small_root)
    out = tmp_path / "site"
    rep = export_static(small_root, out)
    assert rep.counts.get("attachments") == 1
    names = sorted(p.name for p in (out / "api" / "attachment").iterdir())
    assert names == ["0a100000-0000-4000-8000-000000000001-attachment.txt.json"]
    assert not any("hunter2" in p.read_text(encoding="utf-8", errors="replace")
                   for p in out.rglob("*") if p.is_file())
    assert any("atlas.yaml" in s and "не вложение Allure" in s for s in rep.skipped)


@pytest.mark.parametrize("payload, needle", [
    ({"endpoints": "oops", "summary": {}}, "endpoints — ожидался список, получено str"),
    ({"summary": {"avg": 50}}, "нет списка endpoints"),
])
def test_coverage_without_an_endpoint_list_says_why(small_root, payload, needle):
    (small_root / "coverage.json").write_text(json.dumps(payload), encoding="utf-8")
    m = _meta(small_root)
    assert not m["hasCoverage"]
    assert len(m["reportProblems"]) == 1 and needle in m["reportProblems"][0]
    assert m["reportProblems"][0].startswith("coverage.json: ")      # без пути машины


def test_export_static_prints_report_problems(small_root, tmp_path, capsys):
    from veha.cli import main

    _reports(small_root, "  coverge: x.json\n")
    assert main(["export-static", "--root", str(small_root),
                 "--out", str(tmp_path / "site")]) == 0
    out = capsys.readouterr().out
    assert "  ! reports.coverge: неизвестный ключ (есть coverage, allure)" in out


def test_clean_refuses_a_default_dir_that_links_outside(small_root, tmp_path):
    foreign = _allure(tmp_path / "foreign")
    try:
        os.symlink(foreign, small_root / "allure-results", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("символические ссылки здесь не создать")
    c = make_client(small_root, readonly=False)
    r = c.post("/api/runner/clean")
    assert r.status_code == 409 and "вне корня" in r.json()["detail"]
    assert sorted(p.name for p in foreign.iterdir()) == ["0a100000-0000-4000-8000-000000000001-attachment.txt", "0e100000-0000-4000-8000-000000000001-result.json"]
