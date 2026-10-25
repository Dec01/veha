"""Статический снимок вебморды (`veha export-static`): то, что кладут на сайт.

Снимок обязан отвечать то же, что сервер: файлы пишутся теми же маршрутами, и
расхождение здесь — это демо, которое показывает не то, что увидит пользователь
сервера. И обязан жить под CSP сайта (`style-src 'self'`, без inline) из подкаталога.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from tests.conftest import make_client

STATIC = Path(__file__).resolve().parents[1] / "veha" / "viz" / "static"
#: Сайт не хочет десятков мегабайт; демо сейчас ~4 МБ. Рост вдвое — повод разобраться.
SIZE_BUDGET = 8_000_000


@pytest.fixture(scope="module")
def snapshot(demo_root, tmp_path_factory):
    from veha.viz.export import export_static

    out = tmp_path_factory.mktemp("snap") / "demo"
    report = export_static(demo_root, out)
    return out, report


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def relative_screens(obj):
    """Сервер отдаёт `/screens/x.png` от корня; снимок — относительно страницы."""
    return json.loads(json.dumps(obj, ensure_ascii=False).replace('"/screens/', '"screens/'))


def test_page_is_marked_static_and_loads_assets_relatively(snapshot):
    out, _ = snapshot
    html = (out / "index.html").read_text(encoding="utf-8")
    assert '<meta name="veha-mode" content="static">' in html
    assert not re.search(r'\b(?:src|href)="/', html), "путь от корня домена мимо подкаталога"
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    for f in STATIC.iterdir():
        if f.name != "index.html":
            # переводы строк — как в репозитории (LF), а не как в рабочей копии
            assert (out / "static" / f.name).read_bytes() == \
                f.read_bytes().replace(b"\r\n", b"\n")


def test_graph_and_lists_match_the_server(snapshot, client):
    out, _ = snapshot
    assert load(out / "api" / "graph.json") == relative_screens(client.get("/api/graph").json())
    assert load(out / "api" / "tests.json") == client.get("/api/tests?limit=2000").json()
    runner = load(out / "api" / "runner.json")
    live = client.get("/api/runner/suites").json()
    assert runner["history"] == live["history"] and runner["trendSeries"] == live["trendSeries"]
    assert runner["readonly"] is True and runner["busy"] is False and runner["run"] is None


def test_live_is_exported_read_only(snapshot, client):
    """«Live» в снимке — состояние стенда и очередь МР на момент снимка. Время чтения
    очереди сервер берёт текущее; у снимка это время снимка контейнеров."""
    out, _ = snapshot
    containers = load(out / "api" / "live" / "containers.json")
    assert containers == client.get("/api/live/containers").json()
    assert containers["enabled"] and containers["containers"]
    mrs = load(out / "api" / "live" / "mrs.json")
    served = client.get("/api/live/mrs").json()
    assert mrs["at"] == containers["at"]
    assert {k: v for k, v in mrs.items() if k != "at"} ==         {k: v for k, v in served.items() if k != "at"}
    assert not (out / "api" / "live" / "container").exists(), "лог «в реальном времени»"


def test_every_node_card_is_there_and_matches(snapshot, client, demo_graph):
    out, report = snapshot
    index = load(out / "api" / "node" / "index.json")
    assert set(index) == {n["id"] for n in demo_graph["nodes"]}
    assert report.counts["nodes"] == len(index)
    for node_id in list(index)[::25]:   # каждый 25-й: сверка всех — минута без пользы
        from urllib.parse import quote

        served = client.get("/api/node/" + quote(node_id, safe="")).json()
        assert load(out / "api" / "node" / index[node_id]) == relative_screens(served), node_id


def test_tests_attachments_and_logs_are_exported(snapshot, client, demo_graph):
    out, _ = snapshot
    files = [t["file"] for t in demo_graph["tests"] if t.get("file")]
    assert files and all((out / "api" / "test" / f).is_file() for f in files)
    sample = files[0]
    assert load(out / "api" / "test" / sample) == client.get(f"/api/test?file={sample}").json()

    sources = set()
    for f in files:
        d = load(out / "api" / "test" / f)
        stack = [d]
        while stack:
            cur = stack.pop()
            sources.update(a["source"] for a in cur.get("attachments") or [])
            stack.extend(cur.get("steps") or [])
    assert sources
    for s in sources:
        name = s if s.endswith(".png") else s + ".json"
        assert (out / "api" / "attachment" / name).is_file(), s
    text = next(s for s in sorted(sources) if not s.endswith(".png"))
    assert load(out / "api" / "attachment" / (text + ".json")) == \
        client.get(f"/api/attachment/{text}").json()

    logs = load(out / "api" / "log.json")
    assert logs["names"] and logs["first"] == logs["names"][0]
    for name in logs["names"]:
        assert logs["logs"][name] == client.get("/api/log", params={"name": name}).json()


def test_screens_are_copied_and_referenced_relatively(snapshot):
    out, report = snapshot
    graph = load(out / "api" / "graph.json")
    images = [s["image"] for s in graph.get("screens") or [] if s.get("image")]
    assert images and report.counts["screens"] == len(images)
    for img in images:
        assert img.startswith("screens/") and (out / img).is_file()


def test_snapshot_has_no_machine_paths(snapshot, demo_root):
    out, _ = snapshot
    root = str(demo_root.resolve())
    assert load(out / "api" / "artifacts.json")["config"]["path"] == "atlas.yaml"
    for f in out.rglob("*"):
        if f.suffix in (".json", ".html", ".js", ".css"):
            text = f.read_text(encoding="utf-8")
            assert root not in text and root.replace("\\", "/") not in text, f
            assert '"/screens/' not in text, f


def test_snapshot_is_deterministic_and_small(snapshot, demo_root, tmp_path):
    from veha.viz.export import export_static

    out, report = snapshot
    again = tmp_path / "again"
    export_static(demo_root, again)
    first = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    second = {p.relative_to(again): p.read_bytes() for p in again.rglob("*") if p.is_file()}
    assert first == second
    assert report.bytes == sum(len(b) for b in first.values())
    assert report.bytes < SIZE_BUDGET, f"снимок {report.bytes} байт"
    assert not report.skipped


def test_reexport_swaps_the_whole_snapshot(demo_root, tmp_path):
    """Повторный экспорт подменяет снимок целиком: устаревший файл не доживает, а
    временный и старый каталоги рядом не остаются."""
    from veha.viz.export import export_static

    out = tmp_path / "site"
    export_static(demo_root, out)
    (out / "api" / "stale.json").write_text("{}", encoding="utf-8")
    export_static(demo_root, out)
    assert not (out / "api" / "stale.json").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["site"]


def test_snapshot_with_foreign_files_is_not_overwritten(demo_root, tmp_path):
    """Рядом со снимком лежит чужое — это уже не снимок, и экспорт его не трогает."""
    from veha.viz.export import ExportError, export_static

    out = tmp_path / "site"
    export_static(demo_root, out)
    (out / "README.txt").write_text("сайт", encoding="utf-8")
    with pytest.raises(ExportError):
        export_static(demo_root, out)
    assert (out / "README.txt").read_text(encoding="utf-8") == "сайт"
    assert (out / "api" / "export.json").is_file()


@pytest.mark.parametrize("marker", ['{"mode": "server"}', "не json",
                                    '{"mode": "static", "generator": "чужой"}'])
def test_marker_is_checked_by_content(demo_root, tmp_path, marker):
    from veha.viz.export import ExportError, export_static

    out = tmp_path / "site"
    (out / "api").mkdir(parents=True)
    (out / "api" / "export.json").write_text(marker, encoding="utf-8")
    with pytest.raises(ExportError):
        export_static(demo_root, out)
    assert (out / "api" / "export.json").read_text(encoding="utf-8") == marker


@pytest.fixture
def demo_copy(demo_root, tmp_path):
    import shutil

    root = tmp_path / "data"
    shutil.copytree(demo_root, root)
    return root


def test_broken_config_does_not_leak_the_machine_path(demo_copy, tmp_path):
    """Ошибка разбора `atlas.yaml` несёт абсолютный путь; в снимок он не попадает."""
    from veha.viz.export import export_static

    (demo_copy / "atlas.yaml").write_text("project: [unclosed\n", encoding="utf-8")
    out = tmp_path / "site"
    export_static(demo_copy, out)
    artifacts = load(out / "api" / "artifacts.json")
    assert "YAML" in artifacts["config"]["error"]
    needles = {str(demo_copy), demo_copy.as_posix(), str(Path.home()), Path.home().as_posix()}
    for f in out.rglob("*.json"):
        text = f.read_text(encoding="utf-8").lower()
        for n in needles:
            assert n.lower() not in text and n.replace("\\", "\\\\").lower() not in text, f


def test_unscrubbed_path_stops_the_export(demo_copy, tmp_path, monkeypatch):
    """Итоговая проверка — второй замок: не вычистилось — снимок не записывается."""
    from veha.viz import export

    (demo_copy / "atlas.yaml").write_text("project: [unclosed\n", encoding="utf-8")
    monkeypatch.setattr(export, "_scrubber", lambda root: (lambda obj: obj))
    out = tmp_path / "site"
    with pytest.raises(export.ExportError, match="путь машины"):
        export.export_static(demo_copy, out)
    assert not out.exists()
    assert not any(p.name.startswith(".site.") for p in tmp_path.iterdir())


def test_not_a_png_is_skipped(demo_copy, tmp_path):
    from veha.viz.export import export_static

    shots = sorted((demo_copy / "screens").glob("*.png"))
    shots[0].write_bytes(b"<svg onload=alert(1)>")
    rep = export_static(demo_copy, tmp_path / "site")
    assert not (tmp_path / "site" / "screens" / shots[0].name).exists()
    assert any(shots[0].name in s and "не PNG" in s for s in rep.skipped)


def test_symlinked_attachment_is_skipped(demo_copy, tmp_path):
    import os

    from veha.viz.export import export_static

    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET-OUTSIDE", encoding="utf-8")
    allure = demo_copy / "allure-results"
    victim = next(p for p in sorted(allure.glob("*-attachment.txt")))
    victim.unlink()
    try:
        os.symlink(secret, victim)
    except (OSError, NotImplementedError):
        pytest.skip("символические ссылки недоступны этой учётной записи")
    rep = export_static(demo_copy, tmp_path / "site")
    assert any(victim.name in s and "символическая ссылка" in s for s in rep.skipped)
    for f in (tmp_path / "site").rglob("*"):
        if f.is_file():
            assert b"SECRET-OUTSIDE" not in f.read_bytes(), f


def test_safe_names_and_junk_attachments():
    from veha.viz.export import _SAFE_NAME, _attachment_sources

    assert _SAFE_NAME.match("a-attachment.txt") and _SAFE_NAME.match(".hidden")
    for bad in (".", "..", "a/b", "a\\b", "", "c:x"):
        assert not _SAFE_NAME.match(bad), bad
    junk = {"attachments": [None, 1, {"source": 7}, {"source": "ok.txt"}],
            "steps": [None, "x", {"attachments": "nope", "steps": [{"attachments":
                                                                     [{"source": "deep.txt"}]}]}]}
    assert _attachment_sources(junk) == ["ok.txt", "deep.txt"]
    assert _attachment_sources(["not", "a", "dict"]) == []


def test_only_whitelisted_static_files_are_copied(snapshot):
    """В снимок едет белый список; новый файл статики обязан попасть в него осознанно."""
    from veha.viz.export import STATIC_FILES

    out, _ = snapshot
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    referenced = {m.split("/")[-1] for m in re.findall(r'(?:src|href)="static/([^"]+)"', html)}
    assert referenced == set(STATIC_FILES)
    assert {f.name for f in STATIC.iterdir() if f.is_file()} == set(STATIC_FILES) | {"index.html"}
    assert {f.name for f in (out / "static").iterdir()} == set(STATIC_FILES)


def test_static_mode_blocks_writes_in_the_browser():
    """Второй замок в странице: в снимке fetch/XHR/sendBeacon с изменяющим методом
    бросают ошибку до сети."""
    api = (STATIC / "api.js").read_text(encoding="utf-8")
    guard = api.split("if (STATIC) {")[1].split("\n  }\n")[0]
    assert "window.fetch = " in guard and "throw new Error" in guard
    assert "XMLHttpRequest.prototype.open" in guard and "sendBeacon" in guard
    assert "(?!\\.{1,2}$)" in api       # `.` и `..` — не имена файлов


def test_refuses_foreign_or_overlapping_directories(demo_root, tmp_path):
    from veha.viz.export import ExportError, export_static

    foreign = tmp_path / "project"
    foreign.mkdir()
    (foreign / "main.py").write_text("print(1)", encoding="utf-8")
    with pytest.raises(ExportError):
        export_static(demo_root, foreign)
    assert (foreign / "main.py").is_file()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["project"]
    with pytest.raises(ExportError):
        export_static(demo_root, demo_root / "site")
    with pytest.raises(ExportError):
        export_static(demo_root, demo_root)


def test_cli_export_static(demo_root, tmp_path, capsys):
    from veha.cli import main

    assert main(["export-static", "--root", str(demo_root), "--out", str(tmp_path / "o")]) == 0
    assert "снимок" in capsys.readouterr().out
    assert (tmp_path / "o" / "api" / "export.json").is_file()
    assert main(["export-static", "--root", str(tmp_path / "nope"),
                 "--out", str(tmp_path / "p")]) == 2


def test_interface_reads_view_data_through_one_place():
    """Чтение для просмотра — только через `vehaApi`: прямой `fetch('/api/…')` в обход
    него в снимке ушёл бы от корня домена, мимо подкаталога, и вернул бы 404."""
    viewing = ("/api/graph", "/api/node/", "/api/tests", "/api/test?", "/api/log",
               "/api/attachment/", "/api/runner/suites", "/api/live/containers",
               "/api/live/mrs")
    for name in ("atlas.js", "dock.js"):
        text = (STATIC / name).read_text(encoding="utf-8")
        for call in re.findall(r"fetch\(\s*[`'\"]([^`'\"]+)", text):
            assert not call.startswith(viewing), f"{name}: fetch('{call}')"


def test_server_actions_are_disabled_in_the_snapshot_markup():
    """Каждая кнопка, которой нужен сервер, в снимке заменена выключенной с подписью."""
    dock = (STATIC / "dock.js").read_text(encoding="utf-8")
    atlas = (STATIC / "atlas.js").read_text(encoding="utf-8")
    assert dock.count("data-rerun=") == 1 and "function rerunButton" in dock
    assert "vehaApi.isStatic ? window.vehaApi.offline('Перегнать кейс'" in atlas
    assert "OFF ? window.vehaApi.offline('Запустить')" in dock
    live = dock.split("function viewLive")[1].split("function when")[0]
    assert "OFF ? window.vehaApi.offline('прогнать · отметить')" in live
    assert "data-live-reload" in live.split("OFF ?")[-1]   # «Обновить» — только у сервера
    assert "window.vehaApi.isStatic" in dock.split("function loadContainerLog")[1][:120]
    assert "window.vehaApi.isStatic" in dock.split("function scheduleLive")[1][:120]


def test_readonly_server_still_serves_relative_page(demo_root):
    """Страница с относительными путями у сервера раздаётся с корня — `static/…`
    разрешается в `/static/…`, и сервер его отдаёт."""
    c = make_client(demo_root)
    html = c.get("/").text
    for ref in re.findall(r'(?:src|href)="([^"]+)"', html):
        assert c.get("/" + ref).status_code == 200, ref


def test_read_inside_refuses_links_and_escapes(tmp_path, monkeypatch):
    """Замок копирования без привилегий на символические ссылки: ссылка, файл вне
    каталога после разрешения, не-PNG под именем PNG — всё мимо снимка."""
    from veha.viz import export

    base = tmp_path / "base"
    base.mkdir()
    (base / "ok.png").write_bytes(export.PNG_SIGNATURE + b"rest")
    (base / "fake.png").write_bytes(b"GIF89a")
    (base / "note.txt").write_text("x", encoding="utf-8")
    assert export._read_inside(base, "ok.png").startswith(export.PNG_SIGNATURE)
    for name, why in (("fake.png", "не PNG"), ("..", "недопустимое имя"),
                      ("missing.txt", "файла нет")):
        with pytest.raises(export._Skip, match=why):
            export._read_inside(base, name)
    real = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink",
                        lambda self: self.name == "note.txt" or real(self))
    with pytest.raises(export._Skip, match="символическая ссылка"):
        export._read_inside(base, "note.txt")
    monkeypatch.setattr(Path, "is_symlink", real)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(Path, "resolve",
                        lambda self, strict=False: outside if self.name == "note.txt"
                        else Path(os.path.realpath(self)))
    with pytest.raises(export._Skip, match="вне своего каталога"):
        export._read_inside(base, "note.txt")


def test_numbers_from_reports_reach_markup_only_as_numbers():
    """Числа из `coverage.json` и отчётов — чужие данные: строка с разметкой на месте
    числа ушла бы в innerHTML или в data-css. Прежние сырые подстановки не возвращаются."""
    dock = (STATIC / "dock.js").read_text(encoding="utf-8")
    atlas = (STATIC / "atlas.js").read_text(encoding="utf-8")
    raw = {
        "dock.js": ["${x.count}", "${cnt}</td>", "${s.avgCoverage}%", "${s.totalEndpoints}",
                    "${s.full}", "${m.calls ?? ''}", "${m.runFailed}</span>", "${r.passed}",
                    "${r.runsFound}", "${v.holes}", "${c.operations}", "`${v}%`",
                    "${e.w || ''}", "${r.testCount}", "${eff[t.name]}</span>"],
        "atlas.js": ["${t.msAvg}", "${t.msP95}", "${m.calls ?? '—'}", "${m.checkCount || 0}",
                     "    const pct = m.coverage;", "${m.nodes}`", "${h.count}"],
    }
    for name, text in (("dock.js", dock), ("atlas.js", atlas)):
        for pattern in raw[name]:
            assert pattern not in text, f"{name}: {pattern}"


def test_snapshot_does_not_depend_on_checkout_line_endings(demo_root, tmp_path, monkeypatch):
    """Рабочая копия с CRLF и `git archive` с LF дают побайтно один снимок."""
    from veha.viz import export

    def static_copy(name, eol):
        d = tmp_path / name
        d.mkdir()
        for f in STATIC.iterdir():
            text = f.read_bytes().replace(b"\r\n", b"\n")
            (d / f.name).write_bytes(text.replace(b"\n", eol))
        return d

    snaps = {}
    for eol, label in ((b"\n", "lf"), (b"\r\n", "crlf")):
        monkeypatch.setattr(export, "STATIC", static_copy(f"static-{label}", eol))
        out = tmp_path / f"snap-{label}"
        export.export_static(demo_root, out)
        snaps[label] = {p.relative_to(out).as_posix(): p.read_bytes()
                        for p in out.rglob("*") if p.is_file()}
    assert snaps["crlf"] == snaps["lf"]
    assert all(b"\r\n" not in b for k, b in snaps["crlf"].items()
               if k.startswith("static/") or k == "index.html")


# ------------------------------------------------- версия статики в адресах

_ASSET_REF = re.compile(r'"(static/[^"]*)"')


def _refs(html: str) -> dict:
    """`static/<имя>` → версия из `?v=`; адрес без версии — `None`."""
    found = {}
    for ref in _ASSET_REF.findall(html):
        path, _, query = ref.partition("?")
        found[path.split("/", 1)[1]] = query[2:] if query.startswith("v=") else None
    return found


def _outside_comments(html: str) -> str:
    """Разметка без содержимого HTML-комментариев — линейным поиском, без регулярок.
    Незакрытый комментарий — сломанная страница, а не повод что-то пропустить."""
    parts, pos = [], 0
    while True:
        start = html.find("<!--", pos)
        if start < 0:
            parts.append(html[pos:])
            return "".join(parts)
        end = html.find("-->", start + 4)
        assert end >= 0, f"незакрытый комментарий в странице с позиции {start}"
        parts.append(html[pos:start])
        pos = end + 3


def _stray_static(html: str, versioned: bool) -> list:
    """Вхождения `static/` (без учёта регистра) вне комментариев, которые не являются
    одним из шести адресов страницы: `"static/<имя>"` или, после подстановки,
    `"static/<имя>?v=<10 hex>"`. Каждый из шести — ровно один раз."""
    from veha.viz.assets import STATIC_FILES

    bare = _outside_comments(html)
    names = "|".join(re.escape(n) for n in STATIC_FILES)
    tail = r"\?v=[0-9a-f]{10}" if versioned else ""
    good = re.compile(rf'"static/({names}){tail}"')
    stray, seen = [], []
    for m in re.finditer("static/", bare, re.I):
        g = good.match(bare, m.start() - 1) if m.start() > 0 else None
        if g:
            seen.append(g.group(1))
        else:
            stray.append(bare[max(0, m.start() - 20):m.end() + 20])
    if sorted(seen) != sorted(STATIC_FILES):
        stray.append(f"адреса страницы: {sorted(seen)}")
    return stray


def test_packaged_page_links_static_only_by_the_six_addresses():
    """Гарантия «других ссылок на статику нет» — здесь, над упакованной страницей и над
    тем, что из неё делает `assets.page`: подстановка во время работы ищет только точные
    строки `"static/<имя>"` и разметку не разбирает."""
    from veha.viz import assets

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert _stray_static(html, versioned=False) == []
    assert _stray_static(assets.page(STATIC, assets.read_static(STATIC)), versioned=True) == []


def test_snapshot_page_versions_every_static_file_by_written_bytes(snapshot):
    """Если JS и CSS снимка на хостинге кешируются надолго, без версии в адресе новая
    страница встречалась со старыми скриптами из кеша и падала до Ctrl+F5. Версия —
    хеш тех байтов, что лежат в снимке рядом со страницей."""
    import hashlib

    from veha.viz.export import STATIC_FILES

    out, _ = snapshot
    html = (out / "index.html").read_text(encoding="utf-8")
    refs = _refs(html)
    assert set(refs) == set(STATIC_FILES)
    for name in STATIC_FILES:
        written = (out / "static" / name).read_bytes()
        assert refs[name] == hashlib.sha256(written).hexdigest()[:10], name
        assert f'"static/{name}?v={refs[name]}"' in html
    assert _stray_static(html, versioned=True) == []


def _static_copy(tmp_path) -> Path:
    d = tmp_path / "static"
    d.mkdir()
    for f in STATIC.iterdir():
        (d / f.name).write_bytes(f.read_bytes())
    return d


@pytest.mark.parametrize("ref", [
    "<script src='static/api.js'></script>",
    '<script src = "./static/api.js"></script>',
    '<SCRIPT SRC="static/api.js"></SCRIPT>',
    '<script data-src="static/api.js"></script>',
    '<script src="/static/api.js"></script>',
    '<script src="static/api.js?x=1"></script>',
    '<link rel="stylesheet" href="static/style.css#a">',
    '<script src="static/extra.js"></script>',
    "<script src=static/api.js></script>",
    '<img srcset="static/api.js 1x">',
    '<p>файлы лежат в Static/ рядом</p>',
])
def test_any_other_static_link_is_refused_or_caught(tmp_path, ref):
    """Любая иная форма ссылки на статику: либо `assets.page` отказывает (адрес
    встретился не ровно один раз), либо её ловит гарантия над страницей. Без версии и
    незамеченной она не проходит."""
    from veha.viz import assets

    d = _static_copy(tmp_path)
    html = (d / "index.html").read_text(encoding="utf-8")
    (d / "index.html").write_text(html.replace("</body>", ref + "\n</body>"),
                                  encoding="utf-8")
    try:
        page = assets.page(d, assets.read_static(d))
    except ValueError as exc:
        assert "ровно" in str(exc) and "один" in str(exc)
        return
    assert _stray_static(page, versioned=True), ref


def test_static_word_in_page_text_does_not_break_the_server(demo_root, tmp_path,
                                                            monkeypatch):
    """Слово `static/` в тексте или комментарии страницы — не адрес: сервер отдаёт
    страницу (200, не 500), шесть адресов — с версией."""
    from veha.viz import app as app_module

    d = _static_copy(tmp_path)
    html = (d / "index.html").read_text(encoding="utf-8")
    (d / "index.html").write_text(html.replace(
        "</body>", "<!-- <script src=static/x.js> --><p>каталог static/ и Static/</p>\n"
                   "</body>"), encoding="utf-8")
    monkeypatch.setattr(app_module, "STATIC", d)
    r = make_client(demo_root).get("/")
    assert r.status_code == 200
    assert all(_refs(r.text).values()) and len(_refs(r.text)) == 6


def test_static_version_changes_only_with_the_file(tmp_path):
    """Изменённый файл — новый адрес, неизменный — прежний; перевод строк рабочей копии
    адреса не меняет."""
    from veha.viz import assets

    d = _static_copy(tmp_path)
    before = _refs(assets.page(d, assets.read_static(d)))
    assert before == _refs(assets.page(d, assets.read_static(d)))
    assert all(before.values())

    dock = d / "dock.js"
    dock.write_bytes(dock.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert _refs(assets.page(d, assets.read_static(d))) == before

    dock.write_bytes(dock.read_bytes() + b"\n// changed\n")
    after = _refs(assets.page(d, assets.read_static(d)))
    assert after["dock.js"] != before["dock.js"]
    assert {k: v for k, v in after.items() if k != "dock.js"} == \
        {k: v for k, v in before.items() if k != "dock.js"}


def test_changed_static_file_gets_a_new_address_in_the_snapshot(demo_root, tmp_path,
                                                                monkeypatch):
    """Два экспорта одного дерева — одни байты; правка одного файла меняет в снимке
    адрес только его."""
    from veha.viz import export

    d = _static_copy(tmp_path)
    monkeypatch.setattr(export, "STATIC", d)

    def page_of(label):
        out = tmp_path / f"snap-{label}"
        export.export_static(demo_root, out)
        return (out / "index.html").read_bytes()

    first, second = page_of("a"), page_of("b")
    assert first == second
    (d / "style.css").write_bytes((d / "style.css").read_bytes() + b"\n/* changed */\n")
    third = page_of("c")
    old, new = _refs(first.decode("utf-8")), _refs(third.decode("utf-8"))
    assert new["style.css"] != old["style.css"]
    assert {k: v for k, v in new.items() if k != "style.css"} == \
        {k: v for k, v in old.items() if k != "style.css"}


def test_page_without_an_exact_address_stops_the_export(demo_root, tmp_path, monkeypatch):
    """Адреса файла нет в странице или он встретился дважды — версию ставить некуда или
    неоднозначно: отказ экспорта, а не страница без версии."""
    from veha.viz import export

    d = _static_copy(tmp_path)
    html = (d / "index.html").read_text(encoding="utf-8")
    (d / "index.html").write_text(html.replace('"static/dock.js"', '"static/dock2.js"'),
                                  encoding="utf-8")
    monkeypatch.setattr(export, "STATIC", d)
    with pytest.raises(export.ExportError, match=r'"static/dock\.js" должен встретиться в '
                                                 r"index\.html ровно один раз, а встретился: 0"):
        export.export_static(demo_root, tmp_path / "snap")
    assert not (tmp_path / "snap").exists()


def test_server_page_carries_the_same_versions_as_the_snapshot(snapshot, client):
    """`veha serve` отдаёт ту же страницу тем же кодом: адреса с теми же версиями, и по
    ним отвечает статика сервера (запрос с `?v=` — тот же файл)."""
    out, _ = snapshot
    served = client.get("/")
    assert served.headers["content-type"].startswith("text/html")
    assert served.headers["cache-control"] == "no-store, must-revalidate"
    snap = (out / "index.html").read_text(encoding="utf-8")
    assert _refs(served.text) == _refs(snap)
    assert served.text == snap.replace('<meta name="veha-mode" content="static">\n', "")
    for name, v in _refs(served.text).items():
        r = client.get(f"/static/{name}?v={v}")
        assert r.status_code == 200
        assert r.content.replace(b"\r\n", b"\n") == (out / "static" / name).read_bytes()


def test_nothing_else_loads_static_files_by_address(snapshot):
    """Страница — единственное место, где адреса статики: стили не грузят файлов
    (`url()`, `@import`), скрипты не подгружают `static/…` сами, ответы API на статику
    не ссылаются. Появится такая ссылка — её тоже нужно версионировать."""
    out, _ = snapshot
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "url(" not in css and "@import" not in css
    for f in STATIC.glob("*.js"):
        text = f.read_text(encoding="utf-8")
        assert "static/" not in text, f.name
        assert not re.search(r"importScripts|new\s+Worker|import\s*\(|"
                             r"createElement\(\s*['\"](?:script|link)", text), f.name
    for f in (out / "api").rglob("*.json"):
        assert "static/" not in f.read_text(encoding="utf-8"), f.relative_to(out)
