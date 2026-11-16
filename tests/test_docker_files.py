"""Образ и пример compose — без Docker: то, что в них обещано, держится текстом.

Собрать образ в проверках нельзя (движка может не быть), поэтому проверяется то, на чём
держатся красные линии: порт наружу только на loopback хоста, корень — том только на
чтение, пользователь не root, в команде нет `--allow-run`, пакет — из этого дерева, а не
с PyPI, и в контекст сборки не уезжает рабочая копия.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]


def _dockerfile() -> str:
    return (REPO / "Dockerfile").read_text(encoding="utf-8")


def _instructions(text: str):
    """Инструкции Dockerfile без комментариев, с продолжениями строк."""
    joined = re.sub(r"\\\n", " ", text)
    return [ln.strip() for ln in joined.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def test_dockerfile_base_is_slim_python_with_a_pinned_patch():
    lines = _instructions(_dockerfile())
    arg = next(ln for ln in lines if ln.startswith("ARG PYTHON_VERSION="))
    assert arg == "ARG PYTHON_VERSION=3.14.4"
    assert "FROM python:${PYTHON_VERSION}-slim" in lines


def test_dockerfile_installs_the_tree_and_runs_without_root():
    lines = _instructions(_dockerfile())
    assert any(ln.startswith("RUN pip install .") for ln in lines)
    assert not any(re.search(r"pip install\s+veha\b", ln) for ln in lines)
    user = [ln for ln in lines if ln.startswith("USER ")]
    assert user and not re.search(r"\b(root|0)(:|$)", user[-1].split(maxsplit=1)[1])


def test_documented_run_commands_publish_only_on_loopback():
    """Каждая команда `docker run` в Dockerfile, README и howto — с портом только на
    127.0.0.1 хоста и в форме compose: --read-only, без привилегий."""
    docs = [REPO / "Dockerfile", REPO / "README.md", REPO / "docs" / "wiki" / "howto" / "docker.md"]
    runs = []
    for f in docs:
        # продолжения строк `\` склеиваются (и в комментарии Dockerfile — с `#` в начале)
        text = re.sub(r"\\\n\s*(#\s*)?", " ", f.read_text(encoding="utf-8"))
        runs += [(f.name, ln) for ln in text.splitlines() if "docker run " in ln]
    assert runs
    for name, ln in runs:
        assert "-p 127.0.0.1:8770:8770" in ln, (name, ln)
        for flag in ("--read-only", "--cap-drop ALL", "--security-opt no-new-privileges"):
            assert flag in ln, (name, flag, ln)


def test_dockerfile_command_is_read_only_serve():
    lines = _instructions(_dockerfile())
    cmd = next(ln for ln in lines if ln.startswith("CMD "))
    args = yaml.safe_load(cmd[4:])                # JSON-массив — тот же YAML
    assert args[:3] == ["serve", "--root", "/data"]
    assert "--allow-run" not in args and "--no-browser" in args
    assert args[args.index("--host") + 1] == "0.0.0.0"
    assert next(ln for ln in lines if ln.startswith("ENTRYPOINT ")) == 'ENTRYPOINT ["veha"]'
    assert not any("pytest" in ln for ln in lines if ln.startswith("RUN "))


def test_compose_example_publishes_only_on_loopback_and_mounts_read_only():
    doc = yaml.safe_load((REPO / "compose.example.yaml").read_text(encoding="utf-8"))
    svc = doc["services"]["veha"]
    assert svc["ports"] == ["127.0.0.1:8770:8770"]
    assert all(v.endswith(":/data:ro") for v in svc["volumes"])
    assert svc["read_only"] is True and svc["cap_drop"] == ["ALL"]
    assert svc["security_opt"] == ["no-new-privileges:true"]
    assert 0 < svc["pids_limit"] <= 512 and svc["mem_limit"]
    assert "network_mode" not in svc
    assert "command" not in svc                    # без --allow-run и прочих переопределений


def test_dockerignore_lets_in_only_the_package():
    text = (REPO / ".dockerignore").read_text(encoding="utf-8")
    rules = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    assert rules[0] == "*"
    allowed = {r[1:] for r in rules if r.startswith("!")}
    assert allowed == {"pyproject.toml", "README.md", "LICENSE", "veha/"}
    for junk in ("**/__pycache__", "**/*.stackdump", "**/.venv*", "**/.git", "**/.env*",
                 "**/.private-names", "**/_tmp_*", "**/*.local.*"):
        assert junk in rules
    # всё, что Dockerfile копирует, контекст пропускает
    copied = [w for ln in _instructions(_dockerfile()) if ln.startswith("COPY ")
              for w in ln.split()[1:-1]]
    assert set(copied) <= allowed | {"veha"}
