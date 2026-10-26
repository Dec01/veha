"""`veha init` — заготовка `atlas.yaml` для существующего проекта.

Главное свойство — код проекта не выполняется: `confpartest.py` разбирается деревом, и
файл, который на верхнем уровне выходит из процесса или ходит в сеть, команде не мешает.
Итог проверяется тем же кодом, что читает файл у агента и у сервера.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from partest_atlas import config as atlas_config

from veha import config as veha_config
from veha import scaffold
from veha.cli import main

TOKEN = "SECRET0123456789"


def _project(tmp_path: Path, confpartest: str = "", **files: str) -> Path:
    root = tmp_path / "shop-tests"
    root.mkdir()
    if confpartest:
        (root / "confpartest.py").write_text(confpartest, encoding="utf-8")
    for name, text in files.items():
        # pytest_ini → pytest.ini, specs__orders_json → specs/orders.json
        f = root / ".".join(name.replace("__", "/").rsplit("_", 1))
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    return root


def _init(root: Path, *extra: str) -> int:
    return main(["init", "--root", str(root), *extra])


def _both(root: Path):
    """Разбор итога агентом и сервером: оба читают без ошибки."""
    agent = atlas_config.load(root)
    assert agent.root == root.resolve()
    server = veha_config.load(root)
    assert server.error == "" and server.atlas is not None
    return agent, server


LITERAL = f'''
swagger_files = {{
    "orders": ["local", "specs/orders.json"],
    "billing": ["url", "https://bot:pa55word@api.example.invalid:8443/v1/openapi.json?token={TOKEN}&x=1#k={TOKEN}"],
}}
'''


def test_literal_spec_local_and_url_without_secrets(tmp_path, capsys):
    root = _project(tmp_path, LITERAL, specs__orders_json="{}")
    assert _init(root) == 0
    text = (root / "atlas.yaml").read_text(encoding="utf-8")
    assert "#   orders: local specs/orders.json" in text
    assert "#   billing: url https://api.example.invalid:8443/v1/openapi.json" in text
    for secret in (TOKEN, "pa55word", "bot:", "token=", "?", "#k"):
        assert secret not in text.split("Контракт OpenAPI")[1].split("Что вписать")[0], secret
    assert TOKEN not in text and "pa55word" not in text
    out = capsys.readouterr().out
    assert TOKEN not in out and "pa55word" not in out
    assert "orders (local), billing (url)" in out

    agent, server = _both(root)
    assert agent.artifacts_dir == root.resolve() / "atlas"
    assert server.artifacts_dir == root.resolve() / "atlas"
    assert server.coverage_file == root.resolve() / "coverage.json"
    assert server.allure_dir == root.resolve() / "allure-results"
    assert server.report_problems() == []
    suites = server.section("runner")["suites"]
    assert list(suites) == ["full"] and suites["full"]["full"] is True
    assert suites["full"]["args"] == []
    # подключение к базе задаёт человек: раздел — только шаблоном
    assert "database" not in agent.raw and "# database:" in text
    assert "#   schemas: auto" in text and "dsn_env: DATABASE_URL" in text
    for later in ("# containers:", "# logs:", "# screens:", "# container_logs:"):
        assert later in text
    # область — по имени каталога, в файл не пишется
    assert agent.raw["project"]["name"] == "shop-tests"
    assert "ATLAS_SYSTEM=shop-tests  ATLAS_ENV=local" in text


def test_next_steps_name_the_commands(tmp_path, capsys):
    root = _project(tmp_path, LITERAL)
    assert _init(root, "--system", "shop", "--env", "stage") == 0
    out = capsys.readouterr().out
    steps = out.split("Дальше:")[1]
    assert 'pip install "partest-atlas[postgres]"' in steps
    assert f"partest-atlas collect openapi --root {root.resolve()} --service orders" in steps
    assert "--service billing" in steps
    assert f"partest-atlas collect db-schema --root {root.resolve()}" in steps
    assert "PARTEST_COVERAGE_JSON=coverage.json pytest --alluredir allure-results" in steps
    assert f"veha serve --root {root.resolve()} --system shop --env stage" in steps
    assert "ATLAS_SYSTEM=shop  ATLAS_ENV=stage" in steps


def test_spec_that_is_not_a_literal_becomes_a_template(tmp_path, capsys):
    root = _project(tmp_path, "from x import load\nswagger_files = load('specs')\n")
    assert _init(root) == 0
    text = (root / "atlas.yaml").read_text(encoding="utf-8")
    assert "swagger_files — не литерал словаря (Call)" in text
    assert "# Что вписать:" in text
    assert "в atlas.yaml заготовка, что вписать" in capsys.readouterr().out
    _both(root)


def test_entry_that_is_not_a_literal_is_named(tmp_path, capsys):
    root = _project(tmp_path, 'import os\nswagger_files = {"a": ["url", os.getenv("A_URL")],'
                              ' "b": ["local", "b.yaml"]}\n')
    assert _init(root) == 0
    text = (root / "atlas.yaml").read_text(encoding="utf-8")
    assert "#   a: ?   ! не литерал — впишите вид и место" in text
    assert "#   b: local b.yaml   ! файла нет" in text
    out = capsys.readouterr().out
    # collect разрешит и непрочитанную запись — команда для неё тоже названа
    assert "--service a" in out and "--service b" in out


def test_local_kind_with_an_address_is_flagged_and_redacted(tmp_path, capsys):
    root = _project(tmp_path, f'swagger_files = {{"api": ["local", '
                              f'"http://h.example.invalid/o.json?key={TOKEN}"]}}\n')
    assert _init(root) == 0
    text = (root / "atlas.yaml").read_text(encoding="utf-8")
    assert TOKEN not in text
    assert "вид local, но это адрес" in text
    assert "вид local, но это адрес" in capsys.readouterr().out


def test_mutated_swagger_files_is_noted(tmp_path):
    root = _project(tmp_path, 'swagger_files = {"a": ["local", "a.json"]}\n'
                              'if True:\n    swagger_files["b"] = ["url", "x"]\n')
    assert _init(root) == 0
    assert "меняется и кодом" in (root / "atlas.yaml").read_text(encoding="utf-8")

    other = tmp_path / "other"
    other.mkdir()
    (other / "confpartest.py").write_text(
        'swagger_files = {"a": ["local", "a.json"]}\nfirst = swagger_files.get("a")\n'
        'swagger_files.update({"b": ["local", "b.json"]})\n', encoding="utf-8")
    assert scaffold.read_spec(other).notes == [
        "swagger_files меняется и кодом (под условием, по ключу или методом) — здесь "
        "только литерал верхнего уровня"]
    (other / "confpartest.py").write_text(
        'swagger_files = {"a": ["local", "a.json"]}\nfirst = swagger_files.get("a")\n',
        encoding="utf-8")
    assert scaffold.read_spec(other).notes == []      # чтение — не изменение


def test_reports_from_pytest_ini(tmp_path, capsys):
    ini = ("[pytest]\n"
           "# addopts = --alluredir=old\n"
           "addopts = -q --alluredir=build/allure -p no:cacheprovider\n"
           "env =\n    D:PARTEST_COVERAGE_JSON=build/coverage.json\n")
    root = _project(tmp_path, LITERAL, pytest_ini=ini)
    assert _init(root) == 0
    out = capsys.readouterr().out
    assert "reports.coverage: build/coverage.json — из pytest.ini" in out
    assert "reports.allure: build/allure — из pytest.ini" in out
    # проект сам задаёт и ключ, и переменную — команда прогона без добавок
    assert "прогон в каталоге проекта:  pytest\n" in out
    _, server = _both(root)
    assert server.coverage_file == root.resolve() / "build" / "coverage.json"
    assert server.allure_dir == root.resolve() / "build" / "allure"


def test_reports_from_pyproject_and_tox(tmp_path):
    pyproject = ('[tool.pytest.ini_options]\naddopts = ["-q", "--alluredir", "out/allure"]\n'
                 '[tool.pytest_env]\nPARTEST_COVERAGE_JSON = "out/coverage.json"\n')
    root = _project(tmp_path, LITERAL, pyproject_toml=pyproject)
    assert _init(root) == 0
    _, server = _both(root)
    assert server.coverage_file == root.resolve() / "out" / "coverage.json"
    assert server.allure_dir == root.resolve() / "out" / "allure"

    tox = "[testenv]\nsetenv =\n    PARTEST_COVERAGE_JSON={toxinidir}/rep/coverage.json\n"
    root2 = tmp_path / "two"
    root2.mkdir()
    (root2 / "tox.ini").write_text(tox, encoding="utf-8")
    assert _init(root2) == 0
    assert veha_config.load(root2).coverage_file == root2.resolve() / "rep" / "coverage.json"

    table = '[tool.pytest_env]\nPARTEST_COVERAGE_JSON = { value = "t/coverage.json" }\n'
    root3 = tmp_path / "three"
    root3.mkdir()
    (root3 / "pyproject.toml").write_text(table, encoding="utf-8")
    assert scaffold.find_reports(root3)[0].path == "t/coverage.json"


def test_substitution_is_not_guessed(tmp_path, capsys):
    tox = "[testenv]\nsetenv =\n    PARTEST_COVERAGE_JSON={envtmpdir}/coverage.json\n"
    root = _project(tmp_path, LITERAL, tox_ini=tox)
    assert _init(root) == 0
    assert "подстановка '{envtmpdir}/coverage.json'" in capsys.readouterr().out
    assert veha_config.load(root).coverage_file == root.resolve() / "coverage.json"


@pytest.mark.parametrize("value", ["../coverage.json", "/ci/out/coverage.json",
                                   "C:/ci/coverage.json", r"..\up\coverage.json"])
def test_report_outside_the_root_is_written_commented(tmp_path, capsys, value):
    root = _project(tmp_path, LITERAL,
                    pytest_ini=f"[pytest]\nenv =\n    PARTEST_COVERAGE_JSON={value}\n")
    assert _init(root) == 0
    text = (root / "atlas.yaml").read_text(encoding="utf-8")
    assert "  # coverage: " in text
    out = capsys.readouterr().out
    assert "reports.coverage: в pytest.ini путь вне корня" in out
    # сервер возьмёт умолчание внутри корня; прогон — с переменной на него
    agent, server = _both(root)
    assert "coverage" not in (agent.raw.get("reports") or {})
    assert server.coverage_file == root.resolve() / "coverage.json"
    assert "PARTEST_COVERAGE_JSON=coverage.json pytest" in out


def test_missing_confpartest_still_writes_a_template(tmp_path, capsys):
    root = _project(tmp_path)
    assert _init(root) == 0
    out = capsys.readouterr().out
    assert "нет confpartest.py в корне проекта" in out
    text = (root / "atlas.yaml").read_text(encoding="utf-8")
    assert "# ! нет confpartest.py" in text
    _both(root)


def test_existing_atlas_yaml_is_refused(tmp_path, capsys):
    root = _project(tmp_path, LITERAL)
    (root / "atlas.yaml").write_text("project: {name: hand-made}\n", encoding="utf-8")
    assert _init(root) == 2
    err = capsys.readouterr().err
    assert "уже есть — не перезаписываю" in err
    assert (root / "atlas.yaml").read_text(encoding="utf-8") == "project: {name: hand-made}\n"


def test_dangling_link_is_refused(tmp_path, capsys):
    root = _project(tmp_path, LITERAL)
    try:
        os.symlink(tmp_path / "nowhere.yaml", root / "atlas.yaml")
    except (OSError, NotImplementedError):
        pytest.skip("символические ссылки здесь не создать")
    assert _init(root) == 2
    assert not (tmp_path / "nowhere.yaml").exists()


def test_bad_arguments(tmp_path, capsys):
    assert _init(tmp_path / "nope") == 2
    assert "нет каталога" in capsys.readouterr().err
    root = _project(tmp_path, LITERAL)
    assert _init(root, "--system", "../up") == 2
    assert not (root / "atlas.yaml").exists()


def test_strings_from_the_project_cannot_break_out_of_comments(tmp_path):
    """Имя службы и адрес — чужой текст: перевод строки в нём не должен стать ключом."""
    root = _project(tmp_path, 'swagger_files = {"x\\nevil: 1": ["url", '
                              '"https://h.example.invalid/a\\nreports: 5"], '
                              '"y\\u2028z: 2": ["local", "p\\rq: 3"]}\n')
    assert _init(root) == 0
    agent, server = _both(root)
    assert set(agent.raw) == {"project", "artifacts_dir", "reports", "runner"}
    assert server.report_problems() == []


def test_init_does_not_execute_confpartest(tmp_path, monkeypatch):
    """Файл, который на верхнем уровне выходит из процесса, пишет флаг и ходит в сеть,
    команде не мешает: он не выполняется."""
    flag = tmp_path / "executed"
    root = _project(tmp_path,
                    "import pathlib, socket\n"
                    f"pathlib.Path({str(flag)!r}).write_text('1')\n"
                    "socket.create_connection(('192.0.2.1', 80), timeout=1)\n"
                    "raise SystemExit('confpartest executed')\n"
                    'swagger_files = {"api": ["url", "http://192.0.2.1/openapi.json"]}\n')
    attempts = []

    def deny(*args, **kwargs):
        attempts.append(args)
        raise OSError("исходящее соединение запрещено проверкой")

    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    assert _init(root) == 0
    assert not flag.exists() and attempts == []
    assert "#   api: url http://192.0.2.1/openapi.json" in \
        (root / "atlas.yaml").read_text(encoding="utf-8")


def test_init_from_the_project_directory_in_a_subprocess(tmp_path):
    """Как у человека: `python -m veha init` из корня проекта — текущий каталог первым в
    `sys.path`, рядом `confpartest.py`, который нельзя выполнять."""
    flag = tmp_path / "executed"
    root = _project(tmp_path,
                    "import pathlib\n"
                    f"pathlib.Path({str(flag)!r}).write_text('1')\n"
                    "raise SystemExit(3)\n"
                    'swagger_files = {"api": ["local", "openapi.json"]}\n')
    out = subprocess.run([sys.executable, "-m", "veha", "init"], cwd=root,
                         env=dict(os.environ), capture_output=True, text=True,
                         encoding="utf-8", timeout=120)
    assert out.returncode == 0, out.stderr
    assert not flag.exists()
    assert (root / "atlas.yaml").is_file()


# ------------------------------------------------------------------ части


@pytest.mark.parametrize("raw, shown", [
    ("https://u:p@h.example.invalid/x?token=1#f", "https://h.example.invalid/x"),
    ("http://h.example.invalid:81/a/b.json", "http://h.example.invalid:81/a/b.json"),
    ("https://:pw@h.example.invalid", "https://h.example.invalid"),
    ("not a url", "<адрес не разобран>"),
    ("http://[::1", "<адрес не разобран>"),
])
def test_safe_url(raw, shown):
    assert scaffold.safe_url(raw) == shown


def test_safe_url_masks_a_secret_in_the_path():
    assert "abcdef123456" not in scaffold.safe_url(
        "https://h.example.invalid/spec;password=abcdef123456/x")


@pytest.mark.parametrize("name, seg", [("my shop", "my-shop"), ("..", "project"),
                                       ("Тесты", "project"), ("a..b", "a.b")])
def test_segment(name, seg):
    assert scaffold.segment(name) == seg
