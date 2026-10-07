"""Build template/ for the Claude Code read-maturation test.

Copies four standard-library modules from the running Python (large, real
files) into template/vendor/, plants two bugs, and writes the visible tests.
hidden_test.py holds the extra checks run.py applies after each run.
Usage: python make_template.py
"""

import shutil
import sysconfig
from pathlib import Path

HERE = Path(__file__).parent
T = HERE / "template"
LIB = Path(sysconfig.get_paths()["stdlib"])

TESTS = {
    "test_shlex.py": '''from vendor import shlex


def test_quote_spaces_and_roundtrip():
    assert shlex.quote("a b") == "'a b'"
    for s in ("plain", "with space", "tab\\there", "quote's"):
        assert shlex.split(shlex.quote(s)) == [s]
''',
    "test_fnmatch.py": '''from vendor import fnmatch


def test_negated_class():
    assert not fnmatch.fnmatchcase("a", "[!a]")
    assert fnmatch.fnmatchcase("b", "[!a]")
    assert fnmatch.filter(["x1", "y1", "z1"], "[!y]1") == ["x1", "z1"]
''',
    "test_textwrap.py": '''from vendor import textwrap


def test_wrap_paragraphs():
    text = "one two three four five six\\n\\nseven eight nine ten eleven"
    assert textwrap.wrap_paragraphs(text, width=10) == "one two\\nthree four\\nfive six\\n\\nseven\\neight nine\\nten eleven"
''',
}

# (module, original, planted) — each must match exactly once.
BUGS = [
    ("shlex.py", r"_find_unsafe = re.compile(r'[^\w@%+=:,./-]', re.ASCII).search",
     r"_find_unsafe = re.compile(r'[^\w@%+=:,./\s-]', re.ASCII).search"),
    ("fnmatch.py", "stuff = '^' + stuff[1:]", "stuff = stuff[1:]"),
]


def main() -> None:
    shutil.rmtree(T, ignore_errors=True)
    (T / "vendor").mkdir(parents=True)
    (T / "tests").mkdir()
    for module in ("configparser.py", "textwrap.py", "shlex.py", "fnmatch.py"):
        shutil.copy(LIB / module, T / "vendor" / module)
    (T / "vendor" / "__init__.py").write_text("")
    (T / "conftest.py").write_text("")
    for module, original, planted in BUGS:
        path = T / "vendor" / module
        src = path.read_text(encoding="utf-8")
        if src.count(original) != 1:
            raise SystemExit(f"{module}: expected text not found once; this Python's stdlib differs")
        path.write_text(src.replace(original, planted), encoding="utf-8")
    for name, body in TESTS.items():
        (T / "tests" / name).write_text(body, encoding="utf-8")
    print(f"template ready in {T} (from {LIB})")


if __name__ == "__main__":
    main()
