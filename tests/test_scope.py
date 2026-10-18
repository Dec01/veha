"""Как сервер выбирает область (система, контур) из раскладки агента."""

from __future__ import annotations

import shutil

import pytest
from partest_atlas.artifacts import Scope

from tests.conftest import artifact_path
from veha import cli, config
from veha.core import sources


def _move_scope(root, system, env):
    src = root / "atlas" / "default" / "default"
    dst = root / "atlas" / system / env
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    shutil.rmtree(root / "atlas" / "default", ignore_errors=True)


def test_no_artifacts_at_all(tmp_path):
    choice = sources.choose_scope(tmp_path / "atlas")
    assert choice.scope == Scope() and "нет" in choice.how
    assert choice.chosen and choice.notable


def test_single_scope_on_disk_is_taken(small_root):
    _move_scope(small_root, "shop", "test")
    choice = sources.choose_scope(small_root / "atlas")
    assert choice.scope == Scope("shop", "test") and "единственная" in choice.how
    assert not choice.notable
    arts = sources.load_artifacts(config.load(small_root))
    assert arts.statuses["db.schema"].state == "ok"


def test_several_scopes_prefer_default(small_root):
    shutil.copytree(small_root / "atlas" / "default" / "default",
                    small_root / "atlas" / "shop" / "preprod")
    choice = sources.choose_scope(small_root / "atlas")
    assert choice.scope == Scope() and "shop/preprod" in choice.how
    assert choice.chosen and choice.notable


def test_several_scopes_without_default_are_not_guessed(small_root):
    _move_scope(small_root, "shop", "test")
    shutil.copytree(small_root / "atlas" / "shop" / "test",
                    small_root / "atlas" / "shop" / "preprod")
    choice = sources.choose_scope(small_root / "atlas")
    assert not choice.chosen and "укажите" in choice.how
    arts = sources.load_artifacts(config.load(small_root))
    assert all(s.state == "missing" for s in arts.statuses.values())
    assert len(arts.scopes) == 2 and arts.as_dict()["scopeChosen"] is False


def test_explicit_scope_wins_even_if_absent(small_root):
    choice = sources.choose_scope(small_root / "atlas", Scope("crm", "prod"))
    assert choice.scope == Scope("crm", "prod") and choice.how == "задана явно"
    assert not choice.notable
    arts = sources.load_artifacts(config.load(small_root), Scope("crm", "prod"))
    assert arts.statuses["db.schema"].state == "missing"
    assert artifact_path(small_root, "db.schema").is_file()


def test_cli_scope_from_flags_and_agent_env(monkeypatch):
    parser = cli.build_parser()
    monkeypatch.delenv("ATLAS_SYSTEM", raising=False)
    monkeypatch.delenv("ATLAS_ENV", raising=False)
    assert cli._requested_scope(parser.parse_args(["serve", "--root", "."])) is None
    args = parser.parse_args(["serve", "--root", ".", "--system", "shop"])
    assert cli._requested_scope(args) == Scope("shop", "default")
    monkeypatch.setenv("ATLAS_ENV", "preprod")
    assert cli._requested_scope(args) == Scope("shop", "preprod")


@pytest.mark.parametrize("bad", ["..", "../x", "a/b", "a\\b", "C:x", "a..b", ".", "сеть"])
def test_scope_name_is_one_safe_segment(monkeypatch, bad):
    """Имя области становится каталогом под atlas/ — выйти через него нельзя."""
    monkeypatch.delenv("ATLAS_SYSTEM", raising=False)
    monkeypatch.setenv("ATLAS_ENV", bad)
    args = cli.build_parser().parse_args(["serve", "--root", "."])
    with pytest.raises(cli.ScopeError):
        cli._requested_scope(args)


def test_cli_refuses_bad_scope_and_missing_root(tmp_path, monkeypatch):
    monkeypatch.delenv("ATLAS_SYSTEM", raising=False)
    monkeypatch.delenv("ATLAS_ENV", raising=False)
    assert cli.main(["serve", "--root", str(tmp_path / "nope"), "--no-browser"]) == 2
    assert cli.main(["serve", "--root", str(tmp_path), "--system", "../etc",
                     "--no-browser"]) == 2


def test_startup_says_when_atlas_yaml_is_missing(tmp_path, monkeypatch, capsys):
    import uvicorn

    monkeypatch.setattr(uvicorn.Server, "run", lambda self, *a, **kw: None)
    monkeypatch.delenv("ATLAS_SYSTEM", raising=False)
    monkeypatch.delenv("ATLAS_ENV", raising=False)
    assert cli.main(["serve", "--root", str(tmp_path), "--no-browser"]) == 0
    out = capsys.readouterr().out
    assert f"нет atlas.yaml в {tmp_path.resolve()}" in out
    assert "артефактов нет ни в одной области" in out
