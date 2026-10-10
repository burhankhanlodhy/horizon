"""Build a public-only tool-output corpus for the Kompress #3119 measurement.

Sources: the Python standard library (sources and directory listings), the
public Headroom clone's commit metadata, installed packages' public
descriptions, and pytest runs of a throwaway sample project. Never the
user's own code; owner names and local paths are scrubbed.
Writes corpus.json: [{category, id, text}].
argv: <python Lib dir> <site-packages dir> <public git clone> <out dir>
"""

from __future__ import annotations

import getpass
import json
import random
import re
import subprocess
import sys
import textwrap
from collections import Counter
from pathlib import Path

LIB, SITE, CLONE, OUT = (Path(a) for a in sys.argv[1:5])
OUT.mkdir(parents=True, exist_ok=True)
BASH = r"C:\Program Files\Git\usr\bin\bash.exe"
USER = getpass.getuser()
HOME = str(Path.home())
rng = random.Random(3119)
items: list[dict] = []


def sh(cmd: str, cwd: Path) -> str:
    r = subprocess.run([BASH, "-c", cmd], cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    return (r.stdout + r.stderr).rstrip()


def scrub(text: str) -> str:
    """No personal details: file owner names and local paths."""
    text = re.sub(re.escape(USER) + r"\s+\d+", "dev dev", text)
    for home in (HOME, HOME.replace("\\", "/"), "/c/Users/" + USER):
        text = text.replace(home, "~")
    return text.replace(USER, "dev")


def add(category: str, ident: str, text: str) -> None:
    text = scrub(text)
    if len(text) >= 1000:  # the proxy only offers tool results of 1,000+ chars
        items.append({"category": category, "id": ident, "text": text[:24000]})


def entries(d: Path) -> int:
    try:
        return sum(1 for _ in d.iterdir())
    except OSError:
        return 0


pkgs = sorted(p for p in LIB.iterdir() if p.is_dir() and any(p.glob("*.py")) and p.name != "test")
pkgs = rng.sample(pkgs, min(25, len(pkgs)))

# 1. Multi-command shell transcripts (what a Bash tool returns).
for p in pkgs:
    files = sorted(p.glob("*.py"))[:6]
    target = files[0].name if files else ""
    script = [
        f"ls -la {p.name}",
        f"wc -l {p.name}/*.py | sort -n | tail -8",
        f"grep -n 'def \\|class ' {p.name}/{target} | head -30" if target else "true",
        f"du -a {p.name} | sort -n | tail -5",
    ]
    add("shell_transcript", p.name, "\n".join(f"$ {cmd}\n{sh(cmd, LIB)}" for cmd in script))

# 2. Fixed-width tables: ls -la of directories with many entries.
big = [d for d in sorted(SITE.iterdir()) if d.is_dir() and entries(d) >= 18]
big += [d for d in sorted(LIB.iterdir()) if d.is_dir() and entries(d) >= 18]
for d in rng.sample(big, min(22, len(big))):
    add("ls_table", d.name, f"$ ls -la\n{sh('ls -la', d)}")

# 3. git log --stat windows of a public repository.
for i in range(20):
    fmt = "commit %H%nAuthor: %an%nDate: %ad%n%n    %s%n"
    add("git_log", f"skip{i * 12}",
        sh(f"git log --stat --no-color -n 12 --skip={i * 12} --format='{fmt}' origin/main", CLONE))

# 4. grep -rn results in packages with many matches.
for word in ["return None", "raise ValueError", "self._", "isinstance(", "encoding",
             "TODO", "def __init__", "import ", "yield ", "except ", "logging", "@property",
             "async def", "with open(", "return self", "if not ", "None)", "self.assert",
             "class ", "kwargs"]:
    pkg = rng.choice(["email", "asyncio", "xml", "http", "logging", "unittest",
                      "multiprocessing", "importlib", "json", "concurrent"])
    add("grep", f"{word}@{pkg}", f"$ grep -rn '{word}' {pkg}\n" + sh(f"grep -rn '{word}' {pkg} | head -80", LIB))

# 5. Prose: public package descriptions.
metas = []
for m in SITE.glob("*.dist-info/METADATA"):
    body = m.read_text(encoding="utf-8", errors="replace").split("\n\n", 1)
    if len(body) == 2 and len(body[1]) > 3000:
        metas.append((m.parent.name, body[1]))
for name, text in rng.sample(metas, min(20, len(metas))):
    add("prose", name, text)

# 6. Code reads: stdlib source slices, numbered like a Read tool.
for m in rng.sample(sorted(LIB.glob("*.py")), 20):
    lines = m.read_text(encoding="utf-8", errors="replace").splitlines()
    start = rng.randrange(0, max(1, len(lines) - 180))
    add("code", m.name, "\n".join(f"{i + start + 1:>6}\t{l}" for i, l in enumerate(lines[start:start + 180])))

# 7. pip list tables: overlapping 45-row windows.
pip = sh(f'"{sys.executable}" -m pip list --disable-pip-version-check', Path.cwd()).splitlines()
pip = [l for l in pip if "Editable" not in l or l.startswith("Package")]
for i in range(0, max(1, len(pip) - 45), 15):
    add("pip_table", f"rows{i}", "\n".join(pip[:2] + pip[2 + i: 2 + i + 45]))

# 8. pytest output from a throwaway sample project with failures.
proj = OUT / "sample_project"
(proj / "tests").mkdir(parents=True, exist_ok=True)
(proj / "shop.py").write_text(textwrap.dedent("""
    def price(qty, unit, discount=0.0):
        if qty < 0:
            raise ValueError("negative quantity")
        return round(qty * unit * (1 - discount), 2)
"""))
for seed in range(12):
    r = random.Random(seed)
    tests = []
    for t in range(r.randint(12, 30)):
        q, u, dsc = r.randint(0, 9), r.choice([1.5, 2.25, 9.99]), r.choice([0, 0.1, 0.25])
        expect = round(q * u * (1 - dsc), 2) + (r.random() < 0.25) * 0.01
        tests.append(f"def test_price_{t}():\n    from shop import price\n    assert price({q}, {u}, {dsc}) == {expect}\n")
    (proj / "tests" / "test_shop.py").write_text("\n".join(tests))
    out = sh(f'"{sys.executable}" -m pytest -q -p no:cacheprovider tests 2>&1 | head -400', proj)
    add("pytest_log", f"seed{seed}", "$ pytest -q tests\n" + out)

(OUT / "corpus.json").write_text(json.dumps(items, indent=0))
print(len(items), dict(Counter(i["category"] for i in items)))
