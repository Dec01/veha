"""Focused offline-контракт generation и legacy snapshots вкладки Live."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from tests.conftest import make_client

CANARY = "canary-live-generation"


def _containers(marker: str) -> dict:
    return {
        "scannedAt": marker,
        "containers": [{
            "container": "storefront-api", "name": marker, "state": "running",
            "status": "Up", "image": "shop:dev", "uptime": "1h",
        }],
    }


def _mrs(marker: str, state: str = "opened") -> dict:
    return {
        "project": "group/shop",
        "mrs": [{
            "iid": 7, "title": marker, "state": state, "author": "A",
            "url": "https://git.example/mr/7", "branch": "feature",
            "updated": marker, "merged": marker if state == "merged" else None,
        }],
    }


def _write_generation(root: Path, generation: str, marker: str, *, pointer: bool = True,
                      state: str = "opened") -> Path:
    directory = root / "live" / "generations" / generation
    (directory / "logs").mkdir(parents=True)
    (directory / "containers.json").write_text(
        json.dumps(_containers(marker)), encoding="utf-8")
    (directory / "mrs.json").write_text(
        json.dumps(_mrs(marker, state)), encoding="utf-8")
    (directory / "logs" / "storefront-api.log").write_text(
        f"log {marker}\n", encoding="utf-8")
    if pointer:
        (root / "live" / "current.json").write_text(json.dumps({
            "schemaVersion": 1,
            "generation": generation,
        }), encoding="utf-8")
    return directory


def _link_dir(link: Path, target: Path) -> None:
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def test_legacy_layout_still_reads_containers_mrs_and_logs(small_root):
    logs = small_root / "logs" / "containers"
    logs.mkdir(parents=True)
    (logs / "storefront-api.log").write_text("legacy log\n", encoding="utf-8")
    client = make_client(small_root)

    containers = client.get("/api/live/containers", params={"ttl": 0}).json()
    mrs = client.get("/api/live/mrs", params={"ttl": 0}).json()
    log = client.get("/api/live/container/storefront-api/logs").json()
    assert (containers["enabled"], containers["total"]) == (True, 11)
    assert (mrs["enabled"], mrs["opened"]) == (True, 3)
    assert log["lines"] == ["legacy log"]


def test_reader_sees_whole_old_or_new_generation_never_staging_or_legacy_mix(small_root):
    _write_generation(small_root, "gold", "old")
    client = make_client(small_root)
    for endpoint in ("/api/live/containers", "/api/live/mrs"):
        assert client.get(endpoint, params={"ttl": 0}).status_code == 200

    _write_generation(small_root, "gnew", "new", pointer=False)
    # Изменение legacy не должно влиять уже при существующем manifest.
    (small_root / "live" / "containers.json").write_text(
        json.dumps(_containers("legacy")), encoding="utf-8")
    (small_root / "live" / "mrs.json").write_text(
        json.dumps(_mrs("legacy")), encoding="utf-8")

    old_containers = client.get("/api/live/containers", params={"ttl": 0}).json()
    old_mrs = client.get("/api/live/mrs", params={"ttl": 0}).json()
    old_log = client.get("/api/live/container/storefront-api/logs").json()
    assert old_containers["containers"][0]["name"] == "old"
    assert old_mrs["mrs"][0]["title"] == "old"
    assert old_log["lines"] == ["log old"]

    (small_root / "live" / "current.json").write_text(json.dumps({
        "schemaVersion": 1, "generation": "gnew",
    }), encoding="utf-8")
    # TTL — штатное короткое кэширование ответа; новый reader обязан увидеть pointer.
    client = make_client(small_root)
    new_containers = client.get("/api/live/containers").json()
    new_mrs = client.get("/api/live/mrs").json()
    new_log = client.get("/api/live/container/storefront-api/logs").json()
    assert new_containers["containers"][0]["name"] == "new"
    assert new_mrs["mrs"][0]["title"] == "new"
    assert new_log["lines"] == ["log new"]


@pytest.mark.parametrize("generation", ["../../outside", "/absolute", "bad/generation"])
def test_invalid_or_escaping_manifest_never_falls_back_to_legacy(small_root, generation):
    (small_root / "live" / "current.json").write_text(json.dumps({
        "schemaVersion": 1, "generation": generation,
    }), encoding="utf-8")
    client = make_client(small_root)

    containers = client.get("/api/live/containers", params={"ttl": 0}).json()
    mrs = client.get("/api/live/mrs", params={"ttl": 0}).json()
    log = client.get("/api/live/container/storefront-api/logs").json()
    assert containers["enabled"] is False and containers["total"] == 0
    assert mrs["enabled"] is False and mrs["mrs"] == []
    assert log["lines"] == [] and "manifest" in log["note"]


def test_incomplete_generation_does_not_borrow_legacy_files(small_root):
    directory = small_root / "live" / "generations" / "gpartial"
    directory.mkdir(parents=True)
    (directory / "containers.json").write_text(
        json.dumps(_containers("partial")), encoding="utf-8")
    (small_root / "live" / "current.json").write_text(json.dumps({
        "schemaVersion": 1, "generation": "gpartial",
    }), encoding="utf-8")
    client = make_client(small_root)

    containers = client.get("/api/live/containers", params={"ttl": 0}).json()
    mrs = client.get("/api/live/mrs", params={"ttl": 0}).json()
    assert containers["containers"][0]["name"] == "partial"
    assert mrs["enabled"] is False and mrs["mrs"] == []


def test_linked_generations_parent_is_refused_without_legacy_fallback(small_root, tmp_path):
    outside = tmp_path / "outside"
    generation = _write_generation(tmp_path, "glinked", CANARY, pointer=False)
    outside.mkdir()
    for child in generation.iterdir():
        if child.is_file():
            child.replace(outside / child.name)
        else:
            child.replace(outside / child.name)
    generation.rmdir()
    try:
        _link_dir(small_root / "live" / "generations", outside)
    except (OSError, NotImplementedError, ImportError, AttributeError):
        pytest.skip("ссылку на каталог здесь не создать")
    (small_root / "live" / "current.json").write_text(json.dumps({
        "schemaVersion": 1, "generation": "glinked",
    }), encoding="utf-8")
    client = make_client(small_root)

    response = client.get("/api/live/containers", params={"ttl": 0})
    assert response.json()["enabled"] is False
    assert CANARY not in response.text


def test_linked_legacy_logs_parent_is_not_read(small_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "storefront-api.log").write_text(CANARY, encoding="utf-8")
    try:
        _link_dir(small_root / "logs", outside)
    except (OSError, NotImplementedError, ImportError, AttributeError):
        pytest.skip("ссылку на каталог здесь не создать")
    client = make_client(small_root)

    response = client.get("/api/live/container/storefront-api/logs")
    assert response.status_code == 200
    assert response.json()["lines"] == []
    assert CANARY not in response.text


def test_mr_checks_stay_outside_generation_and_do_not_rewrite_snapshot(small_root):
    generation = _write_generation(small_root, "gchecks", "new", state="merged")
    snapshot = (generation / "mrs.json").read_bytes()
    client = make_client(small_root, readonly=False)

    response = client.post("/api/live/mr", json={"iid": 7, "status": "checked"})
    assert response.status_code == 200
    assert (small_root / "live" / "mr_checks.json").is_file()
    assert not (generation / "mr_checks.json").exists()
    assert (generation / "mrs.json").read_bytes() == snapshot


def test_deleted_pinned_generation_is_revalidated_before_cached_reads(small_root):
    directory = _write_generation(small_root, "gold", "cached")
    client = make_client(small_root)
    for endpoint in ("/api/live/containers", "/api/live/mrs"):
        response = client.get(endpoint, params={"generation": "gold"})
        assert response.status_code == 200
        assert response.json()["generation"] == "gold"

    shutil.rmtree(directory)
    for endpoint in ("/api/live/containers", "/api/live/mrs"):
        response = client.get(endpoint, params={"generation": "gold"})
        assert response.status_code == 404
        assert "cached" not in response.text


def test_deleted_pinned_generation_does_not_write_mr_check(small_root):
    _write_generation(small_root, "gold", "old")
    client = make_client(small_root, readonly=False)
    created = client.post("/api/live/mr", json={
        "iid": 7, "status": "checked", "generation": "gold"})
    assert created.status_code == 200
    checks = small_root / "live" / "mr_checks.json"
    before = checks.read_bytes()

    response = client.post("/api/live/mr", json={
        "iid": 7, "status": "todo", "note": "must not be written",
        "generation": "missing"})
    assert response.status_code == 404
    assert checks.read_bytes() == before
    assert b"must not be written" not in checks.read_bytes()


def test_pinned_generation_keeps_containers_mrs_and_logs_together(small_root):
    gold = _write_generation(small_root, "gold", "old")
    client = make_client(small_root)
    containers = client.get("/api/live/containers", params={"ttl": 0}).json()
    assert containers["generation"] == "gold"
    assert containers["containers"][0]["name"] == "old"

    # Pointer переключился после ответа containers: связанные запросы остаются на gold.
    _write_generation(small_root, "gnew", "new")
    mrs = client.get("/api/live/mrs", params={"ttl": 0, "generation": "gold"}).json()
    log = client.get("/api/live/container/storefront-api/logs",
                     params={"generation": "gold"}).json()
    assert mrs["generation"] == "gold" and mrs["mrs"][0]["title"] == "old"
    assert log["generation"] == "gold" and log["lines"] == ["log old"]

    # Новый reader видит current без TTL-кэша предыдущего независимого клиента.
    current_client = make_client(small_root)
    current_containers = current_client.get("/api/live/containers").json()
    current_mrs = current_client.get("/api/live/mrs").json()
    current_log = current_client.get("/api/live/container/storefront-api/logs").json()
    assert current_containers["generation"] == "gnew"
    assert current_containers["containers"][0]["name"] == "new"
    assert current_mrs["generation"] == "gnew" and current_mrs["mrs"][0]["title"] == "new"
    assert current_log["generation"] == "gnew" and current_log["lines"] == ["log new"]

    for url in ("/api/live/containers", "/api/live/mrs",
                "/api/live/container/storefront-api/logs"):
        assert client.get(url, params={"generation": "missing"}).status_code == 404
        assert client.get(url, params={"generation": "bad/generation"}).status_code == 404
    shutil.rmtree(gold)
    assert client.get("/api/live/container/storefront-api/logs",
                      params={"generation": "gold"}).status_code == 404


def test_legacy_pin_does_not_switch_when_manifest_appears(small_root):
    logs = small_root / "logs" / "containers"
    logs.mkdir(parents=True)
    (logs / "storefront-api.log").write_text("legacy log\n", encoding="utf-8")
    client = make_client(small_root)
    containers = client.get("/api/live/containers", params={"ttl": 0}).json()
    generation = containers["generation"]
    assert generation == "@legacy"

    _write_generation(small_root, "gnew", "new")
    mrs = client.get("/api/live/mrs", params={"ttl": 0, "generation": generation}).json()
    log = client.get("/api/live/container/storefront-api/logs",
                     params={"generation": generation}).json()
    assert mrs["generation"] == generation and mrs["mrs"][0]["title"] != "new"
    assert log["generation"] == generation and log["lines"] == ["legacy log"]
