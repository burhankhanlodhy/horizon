"""The API image copies an explicit file list; a module left out of it makes
the container crash on start (ModuleNotFoundError), as contact.py once did."""

from pathlib import Path

API = Path(__file__).resolve().parents[1]


def test_every_api_module_is_copied_into_the_image():
    dockerfile = (API / "Dockerfile").read_text(encoding="utf-8")
    copied = {
        token
        for line in dockerfile.replace("\\\n", " ").splitlines()
        if line.strip().startswith("COPY ")
        for token in line.split()[1:-1]
    }
    modules = {p.name for p in API.glob("*.py")} | {"schema.sql"}
    assert modules <= copied, f"missing from api/Dockerfile COPY: {sorted(modules - copied)}"
