"""Маскирование секретов во всём, что сервер отдаёт в вебморду.

Вложения Allure, логи прогона и контейнеров, разбор падений, вывод перезапуска, свободный
текст артефактов — всё это пишет чужая программа, и в нём бывают заголовки авторизации,
пароли и токены. Красная линия: в интерфейс секреты не уходят.

**Один набор признаков на структуры и текст.** Ключ чувствителен, если в нём, без учёта
регистра и разделителей, есть одно из слов :data:`LONG_WORDS` — `DB_PASSWORD`, `newPassword`,
`accessToken`, `AWS_SECRET_ACCESS_KEY`, `x-api-key`. Наборы ключей `partest.redact`
(`DEFAULT_SENSITIVE_HEADERS`, `DEFAULT_SENSITIVE_BODY_KEYS`) этим признаком покрыты
целиком; тест держит это свойство, чтобы набор семейства и наш не разошлись молча.

* **Структуры** — значение под чувствительным ключом маскируется целиком, даже если это
  объект или список: `{"secret": {"value": "…"}}` иначе утёк бы через вложенный ключ.
  Всё глубже предела вложенности маскируется, а не отдаётся как есть.
* **Свободный текст** — :func:`text`. В partest такой функции нет; написанное здесь
  полезно и без карты, и это кандидат в `partest.redact`; пока оно живёт
  здесь.

Значение маскируется `partest.redact.mask_secret` **без префикса**: у partest по
умолчанию остаются первые восемь символов «для отладки», а восемь символов пароля — это
половина пароля.

Маскирование — эвристика, а не гарантия: секрет без узнаваемого признака рядом не
найдётся. Все квантификаторы ограничены по длине: текст приходит из чужих файлов, и
регулярка с вложенным перебором на нём превращается в зависание сервера.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, List

from partest.redact import mask_secret

MASK = "***"
DEPTH = 12

#: Единственный источник слов-признаков — из него строятся и проверка ключа структуры,
#: и оба текстовых правила. Длинные слова ищутся **подстрокой** ключа без регистра и
#: разделителей: `DB_PASSWORD`, `accessToken`, `x-api-key`. Короткие — только **целым
#: сегментом** ключа: `pass` в `db_pass`, но не в `passenger`; `sid` в `PHPSESSID` и
#: `sid`, но не в `side`.
LONG_WORDS = ("password", "passwd", "passphrase", "pwd", "secret", "token", "api[_-]?key",
              "private[_-]?key", "session(?:id)?", "cookie", "credential", "authorization")
SHORT_WORDS = ("pass", "pw", "pswd", "creds", "sid", "phpsessid")
#: Параметры запроса, которые несут секрет, хотя слово само по себе не секретное.
#: Только после `?`/`&`: как слово `code` и `key` встречаются в идентификаторах сплошь.
QUERY_PARAMS = ("sig", "signature", "code", "key", "x-amz-signature", "x-amz-credential")

_LONG_RE = re.compile("|".join(LONG_WORDS), re.I)
_ZERO_WIDTH = re.compile("[\u200b-\u200f\u2060\ufeff\u00ad]")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


#: Сколько разных символов помнит таблица свёртки. Дальше считается без запоминания:
#: текст с сотнями тысяч разных кодовых точек не должен раздувать память сервера.
_FOLD_CACHE_LIMIT = 65536


class _Fold(dict):
    """Посимвольная свёртка для `str.translate`.

    Символ заменяется своим NFKC, **только если тот — ровно один символ**
    (`ｐ` → `p`, `𝐩` → `p`); невидимые символы удаляются; остальное остаётся как есть.
    Это не NFKC целиком, и нарочно: полная нормализация раскрывает `U+FDFA` в 18
    символов и переставляет длинные цепочки комбинирующих знаков за квадратичное время —
    миллион символов шёл почти две минуты, держа GIL всего сервера. Свёртка по одному
    символу линейна и ничего не раздувает: длина текста не растёт никогда.
    """

    def __missing__(self, code: int):
        ch = chr(code)
        if _ZERO_WIDTH.match(ch):
            value = None
        else:
            folded = unicodedata.normalize("NFKC", ch)
            value = folded if len(folded) == 1 and folded != ch else code
        if len(self) < _FOLD_CACHE_LIMIT:
            self[code] = value
        return value


_FOLD = _Fold()


def _norm(value: Any) -> str:
    """Текст или ключ к виду сравнения: полноширинные и стилизованные буквы — обычными,
    без невидимых символов. Без этого `pａssword` или `pass\u200bword` прошли бы мимо.
    ASCII — быстрым путём, без таблицы."""
    s = str(value)
    return s if s.isascii() else s.translate(_FOLD)


def sensitive_key(key: Any) -> bool:
    """Чувствителен ли ключ структуры. То же решение, что у текстовых правил."""
    k = _norm(key)
    if _LONG_RE.search(re.sub(r"[\s_.-]", "", k)):
        return True
    segments = re.split(r"[\s_.\-]+", _CAMEL.sub("_", k).lower())
    return any(s in SHORT_WORDS for s in segments)


# --------------------------------------------------------------------- текст
# Значение: в кавычках — до закрывающей, с пробелами и экранированными кавычками `\"`
# внутри; без кавычек — до разделителя. Альтернативы в повторении начинаются с разных
# символов, поэтому перебора нет.
_VALUE = (r"(?:\"((?:[^\"\\\r\n]|\\.){0,4096})\""
          r"|'((?:[^'\\\r\n]|\\.){0,4096})'"
          r"|([^\s\"',;&}]{1,4096}))")
_SEP = r"([\"']?\s{0,8}[:=]\s{0,8})"

# `Authorization: Bearer xxx`, `"authorization": "Basic xxx"` — схему оставляем
_AUTH_RE = re.compile(
    r"(?i)\b((?:proxy-)?authorization[\"']?\s{0,8}[:=]\s{0,8}[\"']?)"
    r"(?:(bearer|basic|token|digest)\s{1,8})?([^\s\"',;]{1,4096})")
# `Cookie: a=b; c=d` и `Set-Cookie: …` — до конца строки
_COOKIE_RE = re.compile(r"(?i)\b((?:set-)?cookie[\"']?\s{0,8}[:=]\s{0,8}[\"']?)([^\r\n\"']{1,4096})")
# Длинное слово внутри ключа: совпадение начинается с самого слова — начало ключа
# маскированию не нужно, а перебор префикса от каждой границы слова давал секунды на
# строке `a-a-a-…`. Голое `Authorization:` разобрано правилом выше (со схемой), поэтому
# здесь `authorization` — только с продолжением ключа: `authorization_url`.
_KV_RE = re.compile(
    "(?i)((?:" + "|".join(w if w != "authorization" else "authorization(?=[A-Za-z0-9_-])"
                          for w in LONG_WORDS)
    + r")[A-Za-z0-9_-]{0,64})" + _SEP + _VALUE)
# Короткое слово — последним сегментом ключа: `DB_PASS=`, `pw:`, `PHPSESSID=`.
# Начало ключа — после не-ключевого символа, поэтому точек входа столько, сколько ключей.
_SHORT_KV_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])((?:[A-Za-z0-9]{1,32}[_-]){0,4}(?:"
    + "|".join(sorted(SHORT_WORDS, key=len, reverse=True)) + "))" + _SEP + _VALUE)
# секрет в параметре запроса: `?sig=…`, `&code=…`
_QUERY_RE = re.compile(
    r"(?i)([?&](?:" + "|".join(re.escape(q) for q in QUERY_PARAMS) + r")=)([^&#\s\"']{1,4096})")
# `token abc123…` и `bearer abc…` без `=`/`:`: значение похоже на секрет — с цифрой и не
# короче восьми символов или просто длинное. Иначе «token expired» стало бы «token ***».
_WORD_VALUE_RE = re.compile(
    r"(?i)\b(bearer|basic|token)(\s{1,8})"
    r"((?=[A-Za-z0-9._~+/=-]{0,4096}\d)[A-Za-z0-9._~+/=-]{8,4096}|[A-Za-z0-9._~+/=-]{20,4096})")
# голый JWT: заголовок base64url начинается с `eyJ`
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{5,4096}\.[A-Za-z0-9_-]{5,8192}\.[A-Za-z0-9_-]{0,4096}")
# токены сервисов по префиксу: ключ AWS, GitHub, GitLab, Slack
_SERVICE_RE = re.compile(
    r"\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,255}|glpat-[A-Za-z0-9_-]{20,255}"
    r"|xox[abpr]-[A-Za-z0-9-]{10,255})\b")
# Закрытый ключ PEM и PGP: `-----BEGIN RSA PRIVATE KEY-----`, `… PGP PRIVATE KEY BLOCK-----`.  # publish-ok: комментарий с образцом заголовка, который ищет маскирование
# Разбирается процедурно (`_mask_pem`), а не одной регуляркой: END ищется **первым** в
# пределах 16 КБ, потому что у ключа с заголовками `Proc-Type`/`DEK-Info` и у PGP-блока
# пустая строка стоит внутри тела. Пустая строка — только запасной конец, когда END нет.
_PEM_BEGIN = re.compile(r"-----BEGIN (?:[A-Z0-9]{1,16} ){0,4}PRIVATE KEY(?: BLOCK)?-----")
_PEM_END = re.compile(r"-----END (?:[A-Z0-9]{1,16} ){0,4}PRIVATE KEY(?: BLOCK)?-----")
_BLANK_LINE = re.compile(r"\r?\n[ \t]{0,64}\r?\n")
PEM_LIMIT = 16384
# `scheme://user:password@host` и `scheme://:password@host`
_URL_CRED_RE = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]{0,30}://[^/\s:@]{0,256}:)([^@\s/]{1,1024})(@)")


def _kv(m: "re.Match[str]") -> str:
    if m.group(3) is not None:
        value = f'"{MASK}"'
    elif m.group(4) is not None:
        value = f"'{MASK}'"
    else:
        value = MASK
    return m.group(1) + m.group(2) + value


def _mask_pem(s: str) -> str:
    """Закрытые ключи: до END в пределах 16 КБ, иначе до пустой строки, иначе 16 КБ.

    Каждый найденный блок съедает свой кусок, поэтому повторы BEGIN без END не
    перебираются заново с каждого начала.
    """
    out: List[str] = []
    pos = 0
    while True:
        m = _PEM_BEGIN.search(s, pos)
        if m is None:
            break
        out.append(s[pos:m.start()])
        limit = min(len(s), m.end() + PEM_LIMIT)
        end = _PEM_END.search(s, m.end(), limit)
        if end is not None:
            pos = end.end()
        else:
            blank = _BLANK_LINE.search(s, m.end(), limit)
            pos = blank.start() if blank is not None else limit
        out.append(MASK)
    out.append(s[pos:])
    return "".join(out)


def text(value: Any) -> Any:
    """Замаскировать секреты в строке. Не строку вернуть как есть.

    Текст сначала сворачивается так же, как ключи структур (`_norm`): полноширинные и
    стилизованные буквы — обычными, невидимые символы — прочь. Иначе `ｐａｓｓｗｏｒｄ=…`
    или `pass\u200bword=…` проходили бы мимо всех правил. Свёртка посимвольная и
    линейная, длину не увеличивает; ASCII не трогается вовсе. Цена — вывод свёрнут
    (полноширинные буквы становятся обычными, невидимые символы пропадают), для показа в
    интерфейсе это допустимо.
    """
    if not isinstance(value, str) or not value:
        return value
    s = _norm(value)
    if "PRIVATE KEY" in s:
        s = _mask_pem(s)
    s = _JWT_RE.sub(MASK, s)
    s = _SERVICE_RE.sub(MASK, s)
    s = _AUTH_RE.sub(lambda m: m.group(1) + (m.group(2) + " " if m.group(2) else "") + MASK, s)
    s = _COOKIE_RE.sub(lambda m: m.group(1) + MASK, s)
    s = _KV_RE.sub(_kv, s)
    s = _SHORT_KV_RE.sub(_kv, s)
    s = _WORD_VALUE_RE.sub(lambda m: m.group(1) + m.group(2) + MASK, s)
    if "?" in s or "&" in s:
        s = _QUERY_RE.sub(lambda m: m.group(1) + MASK, s)
    if "://" in s:      # схема перед учётными данными: без «://» искать нечего
        s = _URL_CRED_RE.sub(lambda m: m.group(1) + MASK + m.group(3), s)
    return s


# ---------------------------------------------------------------- структуры
def _mask_value(value: Any) -> Any:
    if value is None or value == "":
        return value
    if isinstance(value, (dict, list, tuple)):
        return MASK
    return mask_secret(value, keep_prefix=0)


def data(value: Any, depth: int = DEPTH) -> Any:
    """Структура с замаскированными значениями чувствительных ключей и строками.

    Глубже предела — маска целиком: отдать недосмотренное как есть значило бы сделать
    глубину способом обойти маскирование.
    """
    if depth < 0:
        return MASK
    if isinstance(value, dict):
        return {k: (_mask_value(v) if sensitive_key(k) else data(v, depth - 1))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [data(v, depth - 1) for v in value]
    return text(value)


def strings(value: Any, skip: frozenset = frozenset(), depth: int = DEPTH) -> Any:
    """Только строки через :func:`text`; ключи не трогаются, значения под *skip* — тоже.

    Для артефактов агента и снимков: в них ключи — это схема формата, а не данные, и
    по ним маскировать нельзя (`found.table.session` — имя таблицы, а не сессия). Имена
    таблиц, топиков, эндпоинтов, nodeid идут в *skip*: это идентификаторы соединения.
    """
    if depth < 0:
        return MASK
    if isinstance(value, dict):
        return {k: (v if k in skip else strings(v, skip, depth - 1)) for k, v in value.items()}
    if isinstance(value, list):
        return [strings(v, skip, depth - 1) for v in value]
    return text(value)


def document(raw: str) -> str:
    """Текст вложения: если это JSON — маскируем как структуру, иначе как текст."""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return text(raw)
    if not isinstance(parsed, (dict, list)):
        return text(raw)
    return json.dumps(data(parsed), ensure_ascii=False, indent=1)


def parameters(params: Any) -> Any:
    """Параметры Allure: `[{"name": "api_key", "value": "…"}]`. Чувствительно здесь имя
    параметра, а ключ значения всегда `value` — общий обход его не узнает."""
    if not isinstance(params, list):
        return data(params)
    out = []
    for p in params:
        if isinstance(p, dict) and sensitive_key(p.get("name", "")) \
                and p.get("value") not in (None, ""):
            p = {**p, "value": _mask_value(p["value"])}
        out.append(data(p))
    return out
