"""`veha init` — заготовка `atlas.yaml` для существующего проекта.

Главное свойство — код проекта не выполняется: `confpartest.py` разбирается деревом, и
файл, который на верхнем уровне выходит из процесса или ходит в сеть, команде не мешает.
Всё, что init читает у проекта, — чужой текст: в файл и в терминал он попадает без
секретов, без управляющих символов и без возможности стать ключом YAML или командой
оболочки. Итог проверяется тем же кодом, что читает файл у агента и у сервера.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import pytest
import yaml
from partest_atlas import config as atlas_config

from veha import config as veha_config
from veha import scaffold
from veha.cli import main

# publish-ok: заведомо несуществующий токен для проверки маскирования
TOKEN = "SECRET0123456789"


@pytest.fixture(autouse=True)
def _no_scope_env(monkeypatch):
    """Область — ключом или окружением; окружение машины в проверки не протекает."""
    monkeypatch.delenv("ATLAS_SYSTEM", raising=False)
    monkeypatch.delenv("ATLAS_ENV", raising=False)


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


def _text(root: Path) -> str:
    return (root / "atlas.yaml").read_text(encoding="utf-8")


LITERAL = f'''
swagger_files = {{
    "orders": ["local", "specs/orders.json"],
    "billing": ["url", "https://bot:pa55word@api.example.invalid:8443/v1/openapi.json?token={TOKEN}&x=1#k={TOKEN}"],
}}
'''


def test_literal_spec_local_and_url_without_secrets(tmp_path, capsys):
    root = _project(tmp_path, LITERAL, specs__orders_json="{}")
    assert _init(root) == 0
    text = _text(root)
    assert "#   orders: local specs/orders.json" in text
    assert "#   billing: url https://api.example.invalid:8443/v1/openapi.json" in text
    assert TOKEN not in text and "pa55word" not in text and "bot:" not in text
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
    # имя каталога — только подпись проекта; область — умолчания всех команд, в файл не
    # пишется
    assert agent.raw["project"]["name"] == "shop-tests"
    assert "ATLAS_SYSTEM=default  ATLAS_ENV=default" in text


def test_next_steps_name_the_commands(tmp_path, capsys):
    root = _project(tmp_path, LITERAL)
    assert _init(root, "--system", "shop", "--env", "stage") == 0
    steps = capsys.readouterr().out.split("Дальше:")[1]
    assert 'pip install "partest-atlas>=0.4" allure-pytest' in steps
    assert "allure-pytest в зависимостях проекта не найден" in steps
    scope = "--system shop --env stage"
    assert (f"partest-atlas collect openapi --root {root.resolve()} {scope} --service orders"
            in steps)
    assert "служб 2: contract.openapi один на область" in steps
    assert "--service billing" not in steps
    # признаков базы нет — шага схемы нет
    assert "db-schema" not in steps and "раскомментируйте раздел database" not in steps
    assert "pytest --alluredir allure-results" in steps
    assert f"veha serve --root {root.resolve()} {scope}" in steps
    # переменные — в трёх оболочках, с PARTEST_COVERAGE_JSON: проект её не задаёт
    assert "export ATLAS_SYSTEM=shop ATLAS_ENV=stage PARTEST_COVERAGE_JSON=coverage.json" in steps
    assert 'set "ATLAS_SYSTEM=shop" & set "ATLAS_ENV=stage"' in steps
    assert '$env:ATLAS_SYSTEM="shop"; $env:ATLAS_ENV="stage"' in steps


def test_scope_defaults_come_from_the_environment_first(tmp_path, capsys, monkeypatch):
    """Как у всех команд обоих пакетов (контракт atlas-config): ключ, иначе
    ATLAS_SYSTEM/ATLAS_ENV, иначе `default` — то же, что возьмут collect и serve без ключей."""
    root = _project(tmp_path, LITERAL)
    assert _init(root) == 0
    out = capsys.readouterr().out
    assert "область для подсказок: default/default" in out
    assert "--system default --env default" in out
    (root / "atlas.yaml").unlink()

    monkeypatch.setenv("ATLAS_SYSTEM", "from-env")
    monkeypatch.setenv("ATLAS_ENV", "ci")
    assert _init(root) == 0
    out = capsys.readouterr().out
    assert "область для подсказок: from-env/ci" in out
    assert "--system from-env --env ci" in out

    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("ATLAS_SYSTEM", "../up")
    assert _init(other) == 2
    assert "ATLAS_SYSTEM" in capsys.readouterr().err
    assert not (other / "atlas.yaml").exists()


def test_db_signals_add_the_schema_step(tmp_path, capsys):
    root = _project(tmp_path, LITERAL, requirements_txt="pytest\npsycopg2-binary==2.9\n"
                                                        "allure-pytest\n")
    assert _init(root, "--system", "s", "--env", "e") == 0
    out = capsys.readouterr().out
    assert "база: psycopg2-binary в requirements.txt" in out
    steps = out.split("Дальше:")[1]
    assert 'pip install "partest-atlas[postgres]>=0.4"\n' in steps    # allure-pytest уже есть
    assert f"partest-atlas collect db-schema --root {root.resolve()} --system s --env e" in steps
    assert "# Признак базы: psycopg2-binary в requirements.txt." in _text(root)


def test_dsn_variable_in_settings_is_a_db_signal(tmp_path, capsys):
    root = _project(tmp_path, LITERAL, pytest_ini="[pytest]\nenv =\n    DATABASE_URL=x\n")
    assert _init(root) == 0
    assert "база: переменная DATABASE_URL в pytest.ini" in capsys.readouterr().out


def test_spec_that_is_not_a_literal_says_what_and_where(tmp_path, capsys):
    root = _project(tmp_path, "from x import load\nswagger_files = load('specs')\n")
    assert _init(root) == 0
    text = _text(root)
    assert "swagger_files в confpartest.py вычисляется кодом (Call)" in text
    assert "# Что и куда вписать: ничего — collect openapi разрешит swagger_files сам." in text
    out = capsys.readouterr().out
    assert "в atlas.yaml вписывать ничего не нужно" in out
    assert "впишите в confpartest.py литерал swagger_files" in out
    _both(root)


def test_entry_that_is_not_a_literal_is_named(tmp_path, capsys):
    root = _project(tmp_path, 'import os\nswagger_files = {"a": ["url", os.getenv("A_URL")],'
                              ' "b": ["local", "b.yaml"]}\n')
    assert _init(root) == 0
    text = _text(root)
    assert "#   a: ?   ! не литерал" in text
    assert "#   b: local b.yaml   ! файла нет" in text
    # collect разрешит и непрочитанную запись — команда называет первую службу
    assert "--service a" in capsys.readouterr().out


def test_confpartest_in_a_subdirectory_is_found(tmp_path, capsys):
    root = _project(tmp_path, "", tests__confpartest_py='swagger_files = {"api": '
                                                         '["local", "openapi.json"]}\n',
                    tests__openapi_json="{}")
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / ".venv" / "lib" / "confpartest.py").write_text("swagger_files = {}\n",
                                                           encoding="utf-8")
    assert _init(root) == 0
    out = capsys.readouterr().out
    assert "спецификации из tests/confpartest.py: api (local)" in out
    assert f"collect openapi --root {(root / 'tests').resolve()}" in out
    assert "#   api: local openapi.json\n" in _text(root)        # файл найден от tests/


def test_local_kind_with_an_address_is_flagged_and_redacted(tmp_path, capsys):
    root = _project(tmp_path, f'swagger_files = {{"api": ["local", '
                              f'"http://h.example.invalid/o.json?key={TOKEN}"]}}\n')
    assert _init(root) == 0
    text = _text(root)
    assert TOKEN not in text
    assert "вид local, но это адрес" in text
    assert "вид local, но это адрес" in capsys.readouterr().out


# publish-ok: заведомо несуществующие UNC-пути для проверки отказа до обращения к сети
UNC = [r"\\evil\share\spec.json", "//evil/share/spec.json", r"\\?\UNC\evil\share\x.json",
       r"\\.\pipe\x", r"\\?\C:\spec.json"]  # publish-ok: продолжение списка вымышленных UNC-путей


@pytest.mark.parametrize("where", UNC + ["C:/abs/spec.json", "/abs/spec.json",
                                         "../outside.json", "a\tb.json"])
def test_local_path_outside_the_confpartest_dir_is_never_touched(tmp_path, monkeypatch, where):
    """Проверка существования — тоже обращение: сетевой путь на Windows ушёл бы по сети.
    Для таких путей ни `is_file`, ни `stat` не вызываются."""
    if where in UNC:
        assert PureWindowsPath(where).anchor, "на Windows это сетевой или устройственный путь"
    touched = []
    real_is_file, real_stat = Path.is_file, Path.stat

    def spy_is_file(self):
        touched.append(str(self))
        return real_is_file(self)

    def spy_stat(self, *a, **kw):
        touched.append(str(self))
        return real_stat(self, *a, **kw)

    root = _project(tmp_path)
    monkeypatch.setattr(Path, "is_file", spy_is_file)
    monkeypatch.setattr(Path, "stat", spy_stat)
    service = scaffold._entry("api", "local", where, root)
    assert service.note and "файла нет" not in service.note
    assert not any("evil" in t or "abs" in t or "outside" in t or "\t" in t for t in touched)


def test_mutated_swagger_files_is_noted(tmp_path):
    root = _project(tmp_path, 'swagger_files = {"a": ["local", "a.json"]}\n'
                              'if True:\n    swagger_files["b"] = ["url", "x"]\n')
    assert _init(root) == 0
    assert "меняется и кодом" in _text(root)

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
    assert "пути отчётов искались в: pytest.ini" in out
    assert "reports.coverage: build/coverage.json — из pytest.ini" in out
    assert "reports.allure: build/allure — из pytest.ini" in out
    # проект сам задаёт и ключ, и переменную — команда прогона без добавок
    assert "прогон в каталоге проекта:  pytest\n" in out
    assert "PARTEST_COVERAGE_JSON" not in out.split("Дальше:")[1]
    _, server = _both(root)
    assert server.coverage_file == root.resolve() / "build" / "coverage.json"
    assert server.allure_dir == root.resolve() / "build" / "allure"


def test_reports_from_pyproject_tox_and_setup_cfg(tmp_path, capsys):
    pyproject = ('[tool.pytest.ini_options]\naddopts = ["-q", "--alluredir", "out/allure"]\n'
                 '[tool.pytest_env]\nPARTEST_COVERAGE_JSON = "out/coverage.json"\n')
    root = _project(tmp_path, LITERAL, pyproject_toml=pyproject)
    assert _init(root) == 0
    _, server = _both(root)
    assert scaffold.find_reports(root)[0].where == "pyproject.toml"
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

    root4 = tmp_path / "four"
    root4.mkdir()
    (root4 / "setup.cfg").write_text("[tool:pytest]\naddopts = --alluredir sc/allure\n",
                                     encoding="utf-8")
    assert scaffold.find_reports(root4)[1].path == "sc/allure"


def test_substitution_is_not_guessed(tmp_path, capsys):
    tox = "[testenv]\nsetenv =\n    PARTEST_COVERAGE_JSON={envtmpdir}/coverage.json\n"
    root = _project(tmp_path, LITERAL, tox_ini=tox)
    assert _init(root) == 0
    assert "подстановка '{envtmpdir}/coverage.json'" in capsys.readouterr().out
    assert veha_config.load(root).coverage_file == root.resolve() / "coverage.json"


# publish-ok: заведомо несуществующие пути для проверки отказа и скрытия значений
@pytest.mark.parametrize("value", ["../coverage.json", "/ci/out/secret-host/coverage.json",
                                   "C:/ci/secret-host/coverage.json",
                                   r"..\up\coverage.json", r"\\filer\share\coverage.json"])  # publish-ok: продолжение списка вымышленных путей
def test_report_outside_the_root_is_not_written_and_not_shown(tmp_path, capsys, value):
    root = _project(tmp_path, LITERAL,
                    pytest_ini=f"[pytest]\nenv =\n    PARTEST_COVERAGE_JSON={value}\n")
    assert _init(root) == 0
    text = _text(root)
    out = capsys.readouterr().out
    assert "reports.coverage: в pytest.ini путь вне корня" in out
    for shown in (text, out):
        assert "secret-host" not in shown and "filer" not in shown and "up/" not in shown
    # сервер возьмёт умолчание внутри корня; прогон — с переменной на него
    agent, server = _both(root)
    assert agent.raw["reports"]["coverage"] == "coverage.json"
    assert server.coverage_file == root.resolve() / "coverage.json"
    assert "PARTEST_COVERAGE_JSON=coverage.json" in out.split("Дальше:")[1]


@pytest.mark.parametrize("value, why", [
    ("out/a\x85b.json", "управляющие символы"),
    ("out/a\u202eb.json", "управляющие символы"),
    ("out/a&calc.json", "нестандартные символы"),
    (f"out/token={TOKEN}/c.json", "нестандартные символы"),
])
def test_odd_report_values_are_not_written(tmp_path, capsys, value, why):
    root = _project(tmp_path, LITERAL)
    (root / "pytest.ini").write_bytes(
        f"[pytest]\nenv =\n    PARTEST_COVERAGE_JSON={value}\n".encode("utf-8"))
    assert _init(root) == 0
    out = capsys.readouterr().out
    assert why in out and TOKEN not in out and TOKEN not in _text(root)
    yaml.safe_load(_text(root))
    assert veha_config.load(root).coverage_file == root.resolve() / "coverage.json"


def test_missing_confpartest_still_writes_a_template(tmp_path, capsys):
    root = _project(tmp_path)
    assert _init(root) == 0
    out = capsys.readouterr().out
    assert "нет confpartest.py ни в корне, ни в подкаталогах" in out
    assert "# ! нет confpartest.py" in _text(root)
    assert "collect openapi" not in out
    _both(root)


def test_existing_atlas_yaml_is_refused(tmp_path, capsys):
    root = _project(tmp_path, LITERAL)
    (root / "atlas.yaml").write_text("project: {name: hand-made}\n", encoding="utf-8")
    assert _init(root) == 2
    err = capsys.readouterr().err
    assert "уже есть — не перезаписываю" in err
    assert (root / "atlas.yaml").read_text(encoding="utf-8") == "project: {name: hand-made}\n"
    assert [p.name for p in root.iterdir() if p.name.startswith(".atlas-")] == []


def test_dangling_link_is_refused(tmp_path, capsys):
    root = _project(tmp_path, LITERAL)
    try:
        os.symlink(tmp_path / "nowhere.yaml", root / "atlas.yaml")
    except (OSError, NotImplementedError):
        pytest.skip("символические ссылки здесь не создать")
    assert _init(root) == 2
    assert not (tmp_path / "nowhere.yaml").exists()


def test_failed_write_leaves_no_atlas_yaml(tmp_path, monkeypatch):
    """Сбой посреди записи не оставляет пустого или полузаписанного файла — ни через
    ссылку, ни в запасном пути без жёстких ссылок."""
    root = _project(tmp_path, LITERAL)
    p = scaffold.plan(root)

    def no_link(*a, **kw):
        raise OSError("жёстких ссылок нет")

    class Broken:
        def __init__(self, f):
            self.f = f

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()
            return False

        def write(self, data):
            self.f.write(data[:10])
            raise OSError("диск кончился")

    real_open = open

    def broken_open(path, mode="r", *a, **kw):
        f = real_open(path, mode, *a, **kw)
        return Broken(f) if str(path).endswith("atlas.yaml") and "x" in mode else f

    monkeypatch.setattr(scaffold.os, "link", no_link)
    monkeypatch.setattr("builtins.open", broken_open)
    with pytest.raises(OSError):
        scaffold.write(p)
    monkeypatch.undo()
    assert sorted(x.name for x in root.iterdir()) == ["confpartest.py"]


def test_atlas_yaml_up_the_tree_is_named(tmp_path, capsys):
    (tmp_path / "atlas.yaml").write_text("project: {name: outer}\n", encoding="utf-8")
    root = _project(tmp_path, LITERAL)
    assert _init(root) == 0
    out = capsys.readouterr().out
    assert f"выше по дереву есть {tmp_path / 'atlas.yaml'}" in out


def test_bad_arguments(tmp_path, capsys):
    assert _init(tmp_path / "nope") == 2
    assert "нет каталога" in capsys.readouterr().err
    root = _project(tmp_path, LITERAL)
    assert _init(root, "--system", "../up") == 2
    assert not (root / "atlas.yaml").exists()


def test_exit_codes_are_in_the_help(capsys):
    with pytest.raises(SystemExit):
        main(["init", "--help"])
    out = capsys.readouterr().out
    assert "Коды возврата: 0" in out and "2 — ничего не записано" in out


def test_strings_from_the_project_cannot_break_out_of_comments(tmp_path, capsys):
    """Имя службы и адрес — чужой текст: перевод строки (и NEL, и разделитель строк, и
    bidi) в нём не должен стать ключом, а файл — нечитаемым."""
    root = _project(tmp_path, 'swagger_files = {"x\\nevil: 1": ["url", '
                              '"https://h.example.invalid/a\\nreports: 5"], '
                              '"y\\u2028z: 2": ["local", "p\\rq: 3"], '
                              '"w\\x85v: 4": ["local", "a\\u202eb"]}\n')
    assert _init(root) == 0
    agent, server = _both(root)
    assert set(agent.raw) == {"project", "artifacts_dir", "reports", "runner"}
    assert server.report_problems() == []
    out = capsys.readouterr().out
    for bad in ("\x85", "\u2028", "\u202e", "\r"):
        assert bad not in out and bad not in _text(root)


@pytest.mark.parametrize("name", ["a;curl${IFS}x|sh", 'b" ; id ; "', "c$(id)", "d`id`"])
def test_odd_service_names_are_not_put_into_commands(tmp_path, capsys, name):
    root = _project(tmp_path, f"swagger_files = {{{name!r}: ['local', 'a.json'], "
                              f"'other': ['local', 'b.json']}}\n")
    assert _init(root) == 0
    out = capsys.readouterr().out
    steps = out.split("Дальше:")[1]
    assert "--service <имя службы>" in steps
    assert name not in steps
    assert "имя нестандартное" in out


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
    assert "#   api: url http://192.0.2.1/openapi.json" in _text(root)


def test_init_from_the_project_directory_in_a_subprocess(tmp_path):
    """Как у человека: `python -m veha init` из корня проекта — текущий каталог первым в
    `sys.path`, рядом `confpartest.py`, который нельзя выполнять."""
    flag = tmp_path / "executed"
    root = _project(tmp_path,
                    "import pathlib\n"
                    f"pathlib.Path({str(flag)!r}).write_text('1')\n"
                    "raise SystemExit(3)\n"
                    'swagger_files = {"api": ["local", "openapi.json"]}\n')
    env = {k: v for k, v in os.environ.items() if k not in ("ATLAS_SYSTEM", "ATLAS_ENV")}
    out = subprocess.run([sys.executable, "-m", "veha", "init"], cwd=root,
                         env=env, capture_output=True, text=True,
                         encoding="utf-8", timeout=120)
    assert out.returncode == 0, out.stderr
    assert not flag.exists()
    assert (root / "atlas.yaml").is_file()


def test_deeply_nested_confpartest_does_not_kill_init(tmp_path, capsys):
    """На Python 3.10 `ast.parse` на такой цепочке роняет интерпретатор — разбор идёт в
    дочернем процессе, и init доживает до заготовки."""
    root = _project(tmp_path, "x = a" + "[0]" * 60000 + "\nswagger_files = {}\n")
    assert _init(root) == 0
    assert "не разбирается" in capsys.readouterr().out
    _both(root)


def test_oversized_files_are_not_read(tmp_path, capsys):
    big = "# " + "x" * (scaffold.READ_LIMIT + 10) + "\n"
    root = _project(tmp_path, big + 'swagger_files = {"a": ["local", "a.json"]}\n',
                    pytest_ini="[pytest]\n" + big)
    assert _init(root) == 0
    out = capsys.readouterr().out
    assert "confpartest.py: больше 256 КБ" in out and "pytest.ini: больше 256 КБ" in out
    _both(root)


def test_templates_are_working_forms_of_the_owner(tmp_path):
    """Шаблоны чужих разделов — минимально рабочая форма владельца: раскомментированный
    блок разбирается кодом partest-atlas."""
    from partest_atlas.collect import container_logs

    root = _project(tmp_path, LITERAL)
    assert _init(root) == 0
    text = _text(root)

    def block(head: str) -> str:
        lines = text.splitlines()
        start = lines.index(next(x for x in lines if x.startswith(f"# {head}:")))
        out = []
        for line in lines[start:]:
            if not line.startswith("#") or line.strip() == "#":
                break
            out.append(line[2:])
        return "\n".join(out)

    (root / "atlas.yaml").write_text(block("container_logs") + "\n" + block("database"),
                                     encoding="utf-8")
    cfg = atlas_config.load(root)
    logs = cfg.section("container_logs")
    assert ":" in logs["source"]
    rules = container_logs.rules_from(logs)
    assert rules and rules[0].compiled().search('table "orders"').group(1) == "orders"
    db = cfg.section("database")
    assert db == {"dsn_env": "DATABASE_URL", "schemas": "auto"}
    try:
        from partest_atlas.adapters.postgres import schemas_from
    except ImportError:          # partest-atlas до 0.3: `schemas: auto` ещё не знает
        return
    assert schemas_from(db) is None


# ------------------------------------------------------------------ части


@pytest.mark.parametrize("raw, shown", [
    ("https://u:p@h.example.invalid/x?token=1#f", "https://h.example.invalid/x"),
    ("http://h.example.invalid:81/a/b.json", "http://h.example.invalid:81/a/b.json"),
    ("https://:pw@h.example.invalid", "https://h.example.invalid"),
    ("not a url", "<адрес не разобран>"),
    ("http://[::1", "http://<скрыто>"),
    # сырые / ? # в пароле: разбор режет учётную запись в путь, запрос или фрагмент
    ("https://user:pa/ss@h.example.invalid/x", "https://<скрыто>"),
    ("https://user:pa?ss@h.example.invalid/x", "https://<скрыто>"),
    ("https://user:pa#ss@h.example.invalid/x", "https://<скрыто>"),
    ("https://h.example.invalid:notaport/x", "https://<скрыто>"),
])
def test_safe_url(raw, shown):
    assert scaffold.safe_url(raw) == shown


def test_safe_url_masks_a_secret_in_the_path():
    assert "abcdef123456" not in scaffold.safe_url(
        "https://h.example.invalid/spec;password=abcdef123456/x")


def test_quoted_is_readable_by_yaml_for_any_text():
    for value in ("путь/отчёт.json", "a\x7fb", "a\x85b", "a\u2028b", "a\u202eb", "a\ud800b"):
        q = scaffold.quoted(value)
        assert q.isascii() and yaml.safe_load(f"k: {q}")["k"] == value


@pytest.mark.parametrize("name, seg", [("my shop", "my-shop"), ("..", "project"),
                                       ("Тесты", "project"), ("a..b", "a.b")])
def test_segment(name, seg):
    assert scaffold.segment(name) == seg


# ------------------------------------------- перепроверка security по 4711f75


@pytest.mark.parametrize("raw", [
    "postgresql://user:p@ss/word@host.example.invalid:5432/db",
    "postgresql://user:p@ss?word@host.example.invalid:5432/db",
    "postgresql://user:p@ss#word@host.example.invalid:5432/db",
])
def test_safe_url_hides_an_address_with_at_after_the_host(raw):
    """`@` в пути, запросе или фрагменте — разбор унёс туда хвост пароля: адрес скрыт
    целиком, ни `ss`, ни `word` не показываются."""
    shown = scaffold.safe_url(raw)
    assert shown == "postgresql://<скрыто>"


def _junction(link: Path, target: Path) -> None:
    """Ссылка на каталог: junction на Windows (прав администратора не нужно), символическая
    ссылка на POSIX."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def test_linked_directories_and_files_are_not_followed(tmp_path, capsys):
    """Каталог-ссылка (junction, симлинк — в том числе на сетевой ресурс) за пределы
    корня: confpartest.py и файлы зависимостей в нём не ищутся и не читаются."""
    outside = tmp_path / "outside"
    (outside / "req").mkdir(parents=True)
    (outside / "confpartest.py").write_text('swagger_files = {"leak": ["local", "x"]}\n',
                                            encoding="utf-8")
    (outside / "req" / "base.txt").write_text("psycopg2\n", encoding="utf-8")
    root = _project(tmp_path)
    try:
        _junction(root / "tests", outside)
        _junction(root / "requirements", outside / "req")
    except (OSError, NotImplementedError, ImportError, AttributeError):
        pytest.skip("ссылку на каталог здесь не создать")
    assert scaffold.find_confpartest(root) == (None, [])
    assert scaffold._dependency_files(root) == []
    assert _init(root) == 0
    out = capsys.readouterr().out
    assert "leak" not in out and "psycopg" not in out
    assert not scaffold.local_to(root, root / "tests" / "confpartest.py")


def test_link_instead_of_a_settings_file_is_not_read(tmp_path):
    outside = tmp_path / "outside.ini"
    outside.write_text("[pytest]\naddopts = --alluredir=leak\n", encoding="utf-8")
    root = _project(tmp_path)
    try:
        os.symlink(outside, root / "pytest.ini")
    except (OSError, NotImplementedError):
        pytest.skip("символические ссылки здесь не создать")
    assert scaffold.find_reports(root)[1].path == ""


def test_odd_directory_names_are_not_put_into_commands(tmp_path, capsys):
    """`$(…)` и обратные кавычки в имени каталога развернули бы bash и PowerShell внутри
    двойных кавычек — путь заменяется заглушкой."""
    root = tmp_path / "t$(calc)x`id`"
    try:
        root.mkdir()
    except OSError:
        pytest.skip("такое имя каталога здесь не создать")
    (root / "confpartest.py").write_text(LITERAL, encoding="utf-8")
    assert _init(root) == 0
    out = capsys.readouterr().out
    steps = out.split("Дальше:")[1]
    assert "$(calc)" not in steps and "`id`" not in steps
    assert "--root <путь к каталогу>" in steps
    assert "имя каталога нестандартное" in out
    assert scaffold.safe_dir("C:/Users/John Doe/proj") == '"C:/Users/John Doe/proj"'  # publish-ok: вымышленный пользователь для проверки экранирования
    # publish-ok: вымышленный домашний путь для проверки безопасной подстановки
    assert scaffold.safe_dir("/home/u/%PATH%") == scaffold.DIR_PLACEHOLDER


def test_relative_confpartest_path_is_printed_safely(tmp_path, capsys):
    root = _project(tmp_path)
    sub = root / "te\u202est"
    try:
        sub.mkdir()
    except OSError:
        pytest.skip("такое имя каталога здесь не создать")
    (sub / "confpartest.py").write_text('swagger_files = {"a": ["local", "a.json"]}\n',
                                        encoding="utf-8")
    assert _init(root) == 0
    out = capsys.readouterr().out
    assert "\u202e" not in out and "\u202e" not in _text(root)
    assert "спецификации из te?st/confpartest.py" in out


def test_main_process_never_parses_confpartest(tmp_path, monkeypatch):
    """Разбор один — в дочернем процессе. Основной `ast.parse` не зовёт вовсе: на 3.10
    повторный разбор упал бы переполнением стека там, где дочерний ответил «ok»."""
    import ast

    def forbidden(*a, **kw):
        raise AssertionError("ast.parse в основном процессе")

    monkeypatch.setattr(ast, "parse", forbidden)
    root = _project(tmp_path, LITERAL)
    assert _init(root) == 0
    assert "#   orders: local specs/orders.json" in _text(root)


def test_child_answer_shape():
    from veha import _spec_child

    answer = _spec_child.analyse(b'swagger_files = {"a": ["url", "u"], 1: ["local", "p"], '
                                 b'"b": f(), **x}\nswagger_files["c"] = 1\n')
    assert answer["assignments"] == 1 and answer["touched"] and answer["unpack"]
    assert [e["key"] for e in answer["entries"]] == ["a", None, "b"]
    assert answer["entries"][0]["shape"] == "ok" and answer["entries"][2]["shape"] == "nonlit"
    assert _spec_child.analyse(b"def (")["error"] == "SyntaxError"


def test_no_interpreter_path_fails_closed(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(scaffold.sys, "executable", "")
    root = _project(tmp_path, LITERAL)
    assert _init(root) == 0
    out = capsys.readouterr().out
    assert "разбор не выполнен: неизвестен путь к интерпретатору Python" in out
    assert "Traceback" not in out
    _both(root)


def test_written_file_has_ordinary_permissions(tmp_path):
    """mkstemp создаёт 0600; atlas.yaml должен читаться и контейнером Вехи под другим
    пользователем — права как у обычного open(): 0o666 & ~umask."""
    root = _project(tmp_path, LITERAL)
    target = scaffold.write(scaffold.plan(root))
    assert os.access(target, os.R_OK | os.W_OK)
    if os.name == "nt":
        return                      # на Windows права — ACL, режим не проверить
    mask = os.umask(0o022)
    os.umask(mask)
    assert target.stat().st_mode & 0o777 == 0o666 & ~mask


def test_format_characters_are_masked():
    text = "a\u200bb\u2060c\U000E0041d\ufeffe\u00adf"
    assert scaffold.one_line(text) == "a?b?c?d?e?f"


def test_scope_segment_rejects_a_trailing_newline():
    from veha.core.sources import valid_segment

    assert valid_segment("shop") and not valid_segment("shop\n")


def test_deep_sum_that_the_child_parses_is_read_once(tmp_path, capsys):
    """Длинная сумма, которую дочерний разбор переживает, а повторный в основном процессе
    на 3.10 — нет (переполнение стека): литерал читается, init доживает до конца."""
    root = _project(tmp_path, "x = " + "1+" * 31400 + "1\n"
                              'swagger_files = {"api": ["local", "openapi.json"]}\n')
    assert _init(root) == 0
    out = capsys.readouterr().out
    assert "спецификации из confpartest.py: api (local)" in out or "не разбирается" in out
    _both(root)
