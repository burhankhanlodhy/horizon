"""Lossless JSON table encoding (json_table) and its use by compact_json."""

from __future__ import annotations

import importlib.util
import json
import random
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from horizon.desktop_mcp.compression import (
    _minify_json_whitespace,
    compact_json,
    compression_decoding_hint,
)
from horizon.desktop_mcp.json_table import decode, encode, parse_lexical, same_json

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "desktop" / "mcp" / "self-test" / "make_dataset.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "json_table"
SESSION_SEED = 20261016


def _load_generator() -> Any:
    spec = importlib.util.spec_from_file_location("make_dataset", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _roundtrip(text: str) -> tuple[str, Any]:
    """Encode the lexical parse of ``text`` and return (table text, decoded value)."""
    encoded = encode(parse_lexical(text))
    return encoded, decode(encoded)


def _assert_lossless(text: str) -> str:
    expected = json.loads(text, parse_float=Decimal)
    encoded, decoded = _roundtrip(text)
    assert same_json(decoded, expected), encoded
    assert encode(parse_lexical(text)) == encoded, "encoder must be deterministic"
    return encoded


# ------------------------------------------------------------ golden layouts


def test_golden_layout_for_tables_lists_and_empty_containers() -> None:
    source = (
        '{"id":"A1","items":[{"sku":"S1","qty":2},{"sku":"S2","qty":1}],'
        '"tags":["x","y"],"meta":{},"note":"","ok":true,"n":1.50}'
    )
    assert _assert_lossless(source) == (
        "id: A1\n"
        "items[2]{sku,qty}:\n"
        "  S1,2\n"
        "  S2,1\n"
        "tags[2]: x,y\n"
        "meta: {}\n"
        'note: ""\n'
        "ok: true\n"
        "n: 1.50"
    )


def test_golden_layout_for_nested_table_cells() -> None:
    source = '{"orders":[{"id":1,"lines":[{"sku":"S1"}]},{"id":2,"lines":[]}]}'
    assert _assert_lossless(source) == (
        "orders[2]{id,lines}:\n  1,\n    lines[1]{sku}:\n      S1\n  2,\n    lines[0]:"
    )


def test_golden_layout_for_list_items_and_unkeyed_arrays() -> None:
    source = '{"m":[{"a":1,"b":[1]},{"c":2}],"z":[1,[2]]}'
    assert _assert_lossless(source) == (
        "m[2]:\n  - a: 1\n    b[1]: 1\n  - c: 2\nz[2]:\n  - 1\n  - [1]: 2"
    )


def test_top_level_array_and_scalar_documents() -> None:
    assert _assert_lossless('[{"a":1},{"a":2}]') == "[2]{a}:\n  1\n  2"
    assert _assert_lossless("[]") == "[0]:"
    assert _assert_lossless("{}") == "{}"
    assert _assert_lossless('"plain"') == "plain"
    assert _assert_lossless('"42"') == '"42"'
    assert _assert_lossless("null") == "null"


# ------------------------------------------------------------ edge cases


@pytest.mark.parametrize(
    "source",
    [
        '{"a":"127.63","b":127.63,"c":"-1","d":"1e5","e":"007","f":"+3"}',
        '{"a":true,"b":"true","c":null,"d":"null","e":"false"}',
        '{"":"","a":{},"b":[],"c":[[]],"d":[{}],"e":[""],"f":{"g":{}}}',
        '{"u":"héllo ✓ 漢字","n":"line1\\nline2","t":"tab\\there","q":"quote\\"d",'
        '"bs":"back\\\\slash","sp":"  pad  ","nul":"a\\u0000b"}',
        '{"a:b":1,"c,d":2,"[x]":3,"{y}":4,"- z":5,"":6,"true":7,"2026":8," sp ":9,'
        '"q\\"k":10,"back\\\\slash":11,"a b":12}',
        '{"m":[{"a":1},{"b":2}],"mixed":[1,"x",{"a":1},[2,3]],"empty":[{},{}]}',
        '{"big":123456789012345678901234567890.5,"exp":1E+400,"neg":-0.000,"z":-0,'
        '"e":1e-7,"i":-9007199254740993,"d":0.1000000000000000055511151231257827}',
        '{"items":[{"id":1,"tags":["a","b"],"meta":{"k":"v"}},{"id":2,"tags":[],"meta":{}}]}',
        '{"d":"2026-08-21","v":"1.2.3","x":"-x","y":"+","z":"1.","w":".5","h":"0x1F"}',
    ],
)
def test_hand_written_edge_cases_round_trip(source: str) -> None:
    _assert_lossless(source)


def test_number_spelling_is_kept_exactly() -> None:
    encoded = _assert_lossless('{"exp":1.2300e+02,"neg":-0.000,"big":1E+400,"z":-0}')
    assert encoded.splitlines() == ["exp: 1.2300e+02", "neg: -0.000", "big: 1E+400", "z: -0"]


def test_strings_and_numbers_stay_distinct_types() -> None:
    encoded, decoded = _roundtrip('{"p":"127.63","q":127.63,"r":"true","s":true}')
    assert decoded == {"p": "127.63", "q": Decimal("127.63"), "r": "true", "s": True}
    assert type(decoded["p"]) is str and type(decoded["q"]) is Decimal
    assert not same_json(decoded, {"p": Decimal("127.63"), "q": "127.63", "r": "true", "s": True})
    assert 'p: "127.63"' in encoded and "q: 127.63" in encoded


def test_keys_that_collide_with_syntax_are_quoted() -> None:
    encoded = _assert_lossless('{"a:b":1,"true":2,"2026":3,"":4,"x,y":5}')
    assert encoded.splitlines() == [
        '"a:b": 1',
        '"true": 2',
        '"2026": 3',
        '"": 4',
        '"x,y": 5',
    ]


def test_non_uniform_arrays_use_list_items() -> None:
    encoded = _assert_lossless('{"mixed":[1,"x",{"a":1},[2,3]]}')
    assert encoded.splitlines() == [
        "mixed[4]:",
        "  - 1",
        "  - x",
        "  - a: 1",
        "  - [2]: 2,3",
    ]


def test_deep_nesting_round_trips() -> None:
    depth = 150
    source = "[" * depth + '{"leaf":1}' + "]" * depth
    _assert_lossless(source)
    nested = '{"k":' * depth + "0" + "}" * depth
    _assert_lossless(nested)


def test_duplicate_keys_are_rejected_by_the_lexical_parse() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        parse_lexical('{"a":1,"a":2}')
    outcome = compact_json('{ "a": 1, "a": 2 }')
    assert not outcome.applied and outcome.fidelity == "byte_exact"


def test_lone_surrogate_cannot_be_encoded_and_compact_falls_back() -> None:
    source = '["\\ud800", "a"]'
    with pytest.raises(ValueError):
        encode(parse_lexical(source))
    outcome = compact_json(source)
    assert outcome.transform == "json_whitespace"


@pytest.mark.parametrize(
    "bad",
    [
        "a: 1\n b: 2",  # odd indentation
        "items[2]{a}:\n  1",  # missing row
        "items[1]{a,b}:\n  1",  # row width mismatch
        "k[2]: 1",  # inline count mismatch
        "k[1]:\n  - 1\n  - 2",  # extra list item
        'a: "unterminated',
        "a: 01",  # leading zero is not a JSON number
        "t[1]{a}:\n  ",  # empty cell with no nested block to fill it
        "t[1]{a}:\n  \n    b: 1",  # nested block under the wrong column name
    ],
)
def test_malformed_text_is_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        decode(bad)


def test_bare_words_that_look_like_constants_are_strings() -> None:
    assert decode("a: Infinity") == {"a": "Infinity"}
    assert decode("a: NaN") == {"a": "NaN"}


# ------------------------------------------------------------ property test


_NUMBER_LEXEMES = [
    "0",
    "-0",
    "1",
    "-7",
    "42",
    "1.50",
    "-0.0",
    "0.1000000000000000055511151231257827",
    "2.2250738585072011e-308",
    "1E+5",
    "1e5",
    "-9007199254740993",
    "123456789012345678901234567890",
    "5.0E-3",
]
_KEY_POOL = [
    "a",
    "b",
    "order id",
    "",
    "true",
    "1x",
    "a:b",
    "x,y",
    'q"k',
    "back\\slash",
    "[b]",
    "{c}",
    "- d",
    " e ",
    "ünï",
    "null",
    "2026",
    "k\nl",
]
_STRING_POOL = [
    "",
    " ",
    "a",
    "true",
    "false",
    "null",
    "127.63",
    "-1",
    "1e5",
    "a,b",
    "a:b",
    "[x]",
    "{}",
    "- item",
    "  pad  ",
    'q"uote',
    "back\\slash",
    "new\nline",
    "tab\there",
    "ünï ✓",
    "\u2028",
    "2026-08-21",
    "1.2.3",
    "-x",
    "+",
    "007",
    "x" * 40,
]


class _Num:
    def __init__(self, lexeme: str) -> None:
        self.lexeme = lexeme


def _random_scalar(rng: random.Random) -> Any:
    roll = rng.random()
    if roll < 0.2:
        return None
    if roll < 0.35:
        return rng.random() < 0.5
    if roll < 0.6:
        return _Num(rng.choice(_NUMBER_LEXEMES))
    return rng.choice(_STRING_POOL)


def _random_value(rng: random.Random, depth: int) -> Any:
    roll = rng.random()
    if depth >= 4 or roll < 0.3:
        return _random_scalar(rng)
    if roll < 0.5:
        keys = rng.sample(_KEY_POOL, rng.randint(0, 4))
        return {key: _random_value(rng, depth + 1) for key in keys}
    if roll < 0.75:
        keys = rng.sample(_KEY_POOL, rng.randint(1, 4))
        return [
            {key: _random_value(rng, depth + 1) for key in keys} for _ in range(rng.randint(1, 5))
        ]
    return [_random_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]


def _to_text(value: Any) -> str:
    if isinstance(value, _Num):
        return value.lexeme
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(
                f"{json.dumps(k, ensure_ascii=False)}:{_to_text(v)}" for k, v in value.items()
            )
            + "}"
        )
    return "[" + ",".join(_to_text(item) for item in value) + "]"


def test_seeded_property_round_trip_over_500_values() -> None:
    rng = random.Random(20261016)
    checked = 0
    for _ in range(520):
        value = _random_value(rng, 0)
        if not isinstance(value, (dict, list)):
            value = {"v": value}
        text = _to_text(value)
        expected = json.loads(text, parse_float=Decimal)
        encoded = encode(parse_lexical(text))
        assert same_json(decode(encoded), expected), encoded
        outcome = compact_json(text)
        if outcome.transform == "json_table":
            assert same_json(decode(outcome.text), expected)
        checked += 1
    assert checked >= 500


# ------------------------------------------------------------ real files


@pytest.fixture(scope="module")
def session_documents(tmp_path_factory: pytest.TempPathFactory) -> Path:
    generator = _load_generator()
    out = tmp_path_factory.mktemp("session-json") / "session"
    generator.generate(out, SESSION_SEED, profile="session")
    return out / "documents"


@pytest.mark.parametrize("name", ["orders_export.json", "platform_config.json"])
def test_session_dataset_json_round_trips(session_documents: Path, name: str) -> None:
    _assert_lossless((session_documents / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["github_issues.json", "package_lock.json"])
def test_realistic_fixture_round_trips(name: str) -> None:
    _assert_lossless((FIXTURES / name).read_text(encoding="utf-8"))


def test_orders_export_table_is_at_least_15_percent_smaller(session_documents: Path) -> None:
    text = (session_documents / "orders_export.json").read_text(encoding="utf-8")
    outcome = compact_json(text)
    assert outcome.transform == "json_table"
    assert outcome.fidelity == "normalized_json_table"
    table_bytes = len(outcome.text.encode("utf-8"))
    ws_bytes = len(_minify_json_whitespace(text).encode("utf-8"))
    assert table_bytes * 100 <= ws_bytes * 85
    hint = compression_decoding_hint(outcome.transform, outcome.fidelity)
    assert hint is not None and "k[N]{c1,c2}:" in hint


def test_config_file_keeps_whitespace_form_below_threshold(session_documents: Path) -> None:
    text = (session_documents / "platform_config.json").read_text(encoding="utf-8")
    outcome = compact_json(text)
    assert outcome.transform == "json_whitespace"
    assert outcome.fidelity == "normalized_json_whitespace"


@pytest.mark.parametrize("name", ["github_issues.json", "package_lock.json"])
def test_realistic_fixtures_keep_whitespace_form(name: str) -> None:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    outcome = compact_json(text)
    assert outcome.applied
    assert outcome.transform == "json_whitespace"
