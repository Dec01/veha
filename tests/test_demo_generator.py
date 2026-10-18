"""Генератор демонстрации: перезапуск на другом интерпретаторе не пачкает git."""

from __future__ import annotations

import importlib.util
import struct
import zlib

import pytest

from tests.conftest import DEMO, GENERATOR


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("veha_demo_generate", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recompress(path, level):
    """Тот же PNG, сжатый иначе — как его сожмёт другая сборка zlib."""
    data = path.read_bytes()
    head, idat, pos = b"", b"", 8
    while pos + 8 <= len(data):
        size, tag = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + size]
        head = body if tag == b"IHDR" else head
        idat += body if tag == b"IDAT" else b""
        pos += 12 + size

    def chunk(tag, body):
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))
    blob = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head)
            + chunk(b"IDAT", zlib.compress(zlib.decompress(idat), level)) + chunk(b"IEND", b""))
    path.write_bytes(blob)
    return blob


def test_png_with_same_pixels_is_not_rewritten(gen, tmp_path):
    path = tmp_path / "s.png"
    c = gen.Canvas(40, 20, gen.BG)
    c.rect(2, 2, 10, 10, gen.ACCENT)
    c.png(path)
    other = _recompress(path, 1)
    c.png(path)
    assert path.read_bytes() == other          # пиксели те же — файл не тронут

    c.rect(20, 2, 10, 10, gen.INK)
    c.png(path)
    assert path.read_bytes() != other          # пиксели другие — перезаписан


def test_demo_screens_decode(gen):
    shots = sorted((DEMO / "screens").glob("*.png"))
    assert len(shots) == 16 and all(gen.png_pixels(p) for p in shots)


def test_every_text_write_is_lf():
    """Набор пишется с LF на любой ОС: иначе на Windows после генерации git видит
    изменения во всех файлах, хотя содержимое то же."""
    import ast

    tree = ast.parse(GENERATOR.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr in ("write_text", "open")]
    assert calls
    bad = [n.lineno for n in calls
           if n.func.attr == "write_text"
           and not any(k.arg == "newline" and isinstance(k.value, ast.Constant)
                       and k.value.value == "\n" for k in n.keywords)]
    assert not bad, f"write_text без newline=LF: строки {bad}"
    assert not [n.lineno for n in calls if n.func.attr == "open"], "open() — писать через write_text"
