"""Маскирование секретов: каждая форма, которую маскирование когда-то пропускало, —
отдельная проверка. Производительность на патологических входах проверена руками, тестом
с замером времени не закреплена: такой тест мигает на загруженной машине."""

from __future__ import annotations

import json

import pytest
from partest.redact import DEFAULT_SENSITIVE_BODY_KEYS, DEFAULT_SENSITIVE_HEADERS

from tests.conftest import artifact_path, make_client
from veha import redact

V = "s3cr3t-Value-42"


@pytest.mark.parametrize("line", [
    f"DB_PASSWORD={V}", f"POSTGRES_PASSWORD: {V}", f"user_password={V}",
    f"SECRET_KEY={V}", f"JWT_SECRET={V}", f"AWS_SECRET_ACCESS_KEY={V}",
    f"x-api-key: {V}", f"apiKey={V}", f"session_id={V}", f"client_credential={V}",
    f"password={V}", f"passwd={V}", f"pwd={V}", f"passphrase: {V}",
])
def test_compound_keys(line):
    assert V not in redact.text(line)


@pytest.mark.parametrize("line,kept", [
    (f'{{"password": "{V} with spaces"}}', '"password": "***"'),
    (f"password='{V} with spaces'", "password='***'"),  # publish-ok: вымышленный пароль-образец для проверки маскирования
    (f'DB_PASSWORD="{V} x y"', 'DB_PASSWORD="***"'),
])
def test_quoted_value_masked_to_the_closing_quote(line, kept):
    out = redact.text(line)
    assert V not in out and "with spaces" not in out and "x y" not in out
    assert kept in out


def test_bare_jwt():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"  # publish-ok: образец JWT из документации jwt.io, ключей не содержит
    out = redact.text(f"got {jwt} back")
    assert "eyJ" not in out and out == "got *** back"


@pytest.mark.parametrize("line", [f"token {V}", f"Bearer {V}", f"bearer {'a' * 24}"])
def test_word_then_value(line):
    out = redact.text(line)
    assert V not in out and "a" * 24 not in out


@pytest.mark.parametrize("line", ["token expired", "the password is weak",
                                  "session closed by peer", "order_header updated"])
def test_prose_is_left_alone(line):
    assert redact.text(line) == line


def test_authorization_keeps_the_scheme():
    assert redact.text(f"Authorization: Bearer {V}") == "Authorization: Bearer ***"
    assert redact.text(f"Cookie: sid={V}; a=b") == "Cookie: ***"
    assert redact.text(f"https://user:{V}@host/x") == "https://user:***@host/x"


@pytest.mark.parametrize("key", ["accessToken", "newPassword", "DB_PASSWORD", "X-Api-Key",
                                 "client_secret", "refresh_token", "sessionId", "Credentials"])
def test_structure_keys_any_case_and_inside_words(key):
    assert V not in json.dumps(redact.data({key: V}))


def test_value_under_sensitive_key_is_masked_whole():
    out = redact.data({"secret": {"value": V}, "tokens": [V, V]})
    assert V not in json.dumps(out)


def test_too_deep_is_masked_not_passed_through():
    deep = cur = {}
    for _ in range(redact.DEPTH + 5):
        cur["x"] = {}
        cur = cur["x"]
    cur["note"] = f"password={V}"
    cur["plain"] = "anything"
    out = json.dumps(redact.data(deep))
    assert V not in out and "anything" not in out


def test_one_rule_for_structures_and_text_covers_the_family_keys():
    """Наборы ключей partest покрыты признаком целиком: иначе ключ, который семейство
    считает секретным, прошёл бы у нас открытым."""
    for key in (*DEFAULT_SENSITIVE_HEADERS, *DEFAULT_SENSITIVE_BODY_KEYS):
        assert redact.sensitive_key(key), key
        if key not in ("authorization", "proxy-authorization", "cookie", "set-cookie"):
            assert V not in redact.text(f"{key}={V}"), key


def test_strings_keep_identifiers_and_mask_text():
    doc = {"table": "session", "found": {"table": {"session": 3}},
           "note": f"DB_PASSWORD={V}"}
    out = redact.strings(doc, skip=frozenset({"table"}))
    assert out["table"] == "session" and out["found"]["table"] == {"session": 3}
    assert V not in out["note"]


# ------------------------------------------------- артефакты и снимки


def test_artifact_free_text_and_live_snapshots_are_masked(small_root):
    spec = artifact_path(small_root, "specs.requirements")
    doc = json.loads(spec.read_text(encoding="utf-8"))
    doc["requirements"][0]["lead"] = f"Подключение: DB_PASSWORD={V}"
    spec.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    cont = small_root / "live" / "containers.json"
    snap = json.loads(cont.read_text(encoding="utf-8"))
    snap["containers"][0]["status"] = f"Exited: token={V}"
    snap["containers"][0]["image"] = f"https://robot:{V}@registry/x:1"
    cont.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")

    mrs = small_root / "live" / "mrs.json"
    m = json.loads(mrs.read_text(encoding="utf-8"))
    m["mrs"][0]["title"] = f"fix: rotate api_key={V}"
    mrs.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")

    c = make_client(small_root)
    for url in ("/api/graph", "/api/live/containers", "/api/live/mrs"):
        assert V not in c.get(url).text, url
    # идентификаторы не тронуты: сцена та же
    assert c.get("/api/graph").json()["meta"]["endpoints"] == 177


# ------------------------------------------------- закрытые ключи, JWT, токены сервисов

PEM_BODY = "MIIEowIBAAKCAQEAsecretbody"


@pytest.mark.parametrize("raw", [
    f"-----BEGIN RSA PRIVATE KEY-----\n{PEM_BODY}\nAAAA\n-----END RSA PRIVATE KEY-----",  # publish-ok: заголовок PEM вокруг вымышленного тела, ключа нет
    f"-----BEGIN PRIVATE KEY----- {PEM_BODY} -----END PRIVATE KEY-----",  # publish-ok: заголовок PEM вокруг вымышленного тела, ключа нет
    f"-----BEGIN EC PRIVATE KEY-----\n{PEM_BODY}",                       # без END  # publish-ok: заголовок PEM вокруг вымышленного тела, ключа нет
    f"-----BEGIN OPENSSH PRIVATE KEY-----\n{PEM_BODY}\n\nnext block",     # до пустой строки; publish-ok: заголовок PEM вокруг вымышленного тела, ключа нет
])
def test_pem_private_key(raw):
    out = redact.text(f"key:\n{raw}\n")
    assert PEM_BODY not in out and "PRIVATE KEY" not in out.split("***")[-1]


def test_pem_without_end_stops_at_the_block():
    out = redact.text(f"-----BEGIN EC PRIVATE KEY-----\n{PEM_BODY}\n\nnext block stays")  # publish-ok: заголовок PEM вокруг вымышленного тела, ключа нет
    assert out.endswith("next block stays") and PEM_BODY not in out


@pytest.mark.parametrize("line", [
    "redis://:pw0rd-42@cache:6379/0",
    "id AKIAIOSFODNN7EXAMPLE used",
    "ghp_" + "A1b2" * 9, "gho_" + "A1b2" * 9, "ghs_" + "A1b2" * 9,
    "glpat-" + "x1Y2" * 5, "xoxb-1234567890-abcdef", "xoxp-1234567890-abcdef",
    "DB_PASS=pw0rd-42", "pw: pw0rd-42", "pswd=pw0rd-42", "creds=pw0rd-42",
    "sid=pw0rd-42", "PHPSESSID=pw0rd-42",
    "GET /x?sig=pw0rd-42", "GET /cb?state=1&code=pw0rd-42", "GET /x?key=pw0rd-42&page=2",
    '{"password": "pw0rd-42 \\" with quote"}',
])
def test_more_secret_forms(line):
    out = redact.text(line)
    for leak in ("pw0rd-42", "IOSFODNN7", "A1b2A1b2", "x1Y2x1Y2", "1234567890-abcdef",
                 "with quote"):
        assert leak not in out, (line, out)


@pytest.mark.parametrize("line", [
    "token_table", "column session_id", "topic auth.token.issued", "GET /api/v1/sessions/42",
    "passenger list", "side effect", "the key is code", "pass the test",
    "GET /api/v1/codes/42", "SELECT key FROM settings",
])
def test_identifiers_stay(line):
    assert redact.text(line) == line


@pytest.mark.parametrize("key", ["pa\u200bssword", "\uff50\uff41\uff53\uff53\uff57\uff4f\uff52\uff44",
                                 "db_pass", "userPw", "PHPSESSID", "api\u00adKey"])
def test_keys_are_normalised_before_comparison(key):
    assert redact.sensitive_key(key) and V not in json.dumps(redact.data({key: V}))


@pytest.mark.parametrize("key", ["passenger", "side", "status", "description", "keyboard"])
def test_ordinary_keys_stay(key):
    assert not redact.sensitive_key(key)


def test_one_source_for_text_and_structures():
    """`authorization_url` в тексте и `authorizationUrl` в структуре решаются одинаково —
    маскируются. Правило одно: ключ, содержащий слово-признак, чувствителен. Исключение
    для authorization породило бы второй список, а цена — скрытый в интерфейсе адрес
    OAuth — меньше цены пропущенного `authorization_code`."""
    assert redact.text("authorization_url=https://idp/x") == "authorization_url=***"
    assert redact.data({"authorizationUrl": "https://idp/x"})["authorizationUrl"] != \
        "https://idp/x"
    # и наоборот: слово, не входящее в источник, не маскируется ни там, ни там
    assert redact.text("author=Ivanov") == "author=Ivanov"
    assert redact.data({"author": "Ivanov"}) == {"author": "Ivanov"}
    for word in redact.SHORT_WORDS:
        assert redact.sensitive_key(f"db_{word}") and V not in redact.text(f"db_{word}={V}")


# ------------------------------------------------- PEM и PGP с пустой строкой внутри

ENCRYPTED_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"  # publish-ok: заголовок PEM вокруг вымышленного тела, ключа нет
    "Proc-Type: 4,ENCRYPTED\n"
    "DEK-Info: AES-128-CBC,3F17F5316E2BAC89\n"
    "\n"
    f"{PEM_BODY}line-after-blank\n"
    "-----END RSA PRIVATE KEY-----")
PGP_BLOCK = (
    "-----BEGIN PGP PRIVATE KEY BLOCK-----\n"  # publish-ok: заголовок PEM вокруг вымышленного тела, ключа нет
    "Version: GnuPG v2\n"
    "\n"
    f"{PEM_BODY}pgp\n"
    "=abcd\n"
    "-----END PGP PRIVATE KEY BLOCK-----")


@pytest.mark.parametrize("block", [
    ENCRYPTED_PEM,
    PGP_BLOCK,
    f"-----BEGIN PRIVATE KEY-----\nAAAA\n\n{PEM_BODY}after-inner-blank\n-----END PRIVATE KEY-----",  # publish-ok: заголовок PEM вокруг вымышленного тела, ключа нет
])
def test_pem_with_a_blank_line_inside_is_masked_to_end(block):
    """END ищется первым: пустая строка внутри тела (заголовки Proc-Type/DEK-Info,
    PGP) не обрывает маскирование на середине ключа."""
    out = redact.text(f"before\n{block}\nafter")
    assert PEM_BODY not in out and out == "before\n***\nafter"


@pytest.mark.parametrize("line", [
    f"\uff50\uff41\uff53\uff53\uff57\uff4f\uff52\uff44={V}",      # полноширинные
    f"pass\u200bword={V}",                                       # zero-width внутри
    f"DB_\u00adPASSWORD={V}",                                    # мягкий перенос
    f"to\u200bken {V}",
])
def test_text_rules_see_normalised_keys(line):
    assert V not in redact.text(line)


def test_normalised_output_is_acceptable():
    """Нормализуется весь текст: невидимые символы пропадают и там, где секрета нет."""
    assert redact.text("hello\u200b world") == "hello world"


@pytest.mark.parametrize("unit", ["\ufdfa", "a\u0301\u0316", "\ufb01", "\u3392"])
def test_fold_never_grows_the_text(unit):
    """Полный NFKC раскрывал `U+FDFA` в 18 символов и переставлял длинные цепочки
    комбинирующих знаков за квадратичное время. Свёртка — по одному символу и только в
    один символ: длина не растёт, многосимвольные раскрытия остаются как есть."""
    s = unit * 1000
    assert redact.text(s) == s


def test_fold_still_catches_lookalike_keys():
    bold_password = ("\U0001d429\U0001d41a\U0001d42c\U0001d42c\U0001d430"
                     "\U0001d428\U0001d42b\U0001d41d")
    assert V not in redact.text(f"{bold_password}={V}")
    assert redact.text("plain ascii stays") == "plain ascii stays"
