"""The web service must not import the scraper stack.

    python -m pytest tests/ -q

`requirements-api.txt` is a deliberately slim install — "no scrapers" — and the
API runs on it. Importing anything under `src/adapters/` drags in
`RiderResolver` and therefore RapidFuzz, which is not installed there, so the
service fails to start and Render refuses the deploy.

That happened on 09-11: a single `from ..adapters.results_html import classify`
in main.py, added the night before a race. The only reason it was survivable is
that Render keeps the previous build running when a deploy fails. Nothing in
the test suite noticed, because the tests run on the FULL requirements where
RapidFuzz is present — the failure only exists in production.

So this checks the import graph by reading the source, not by importing it.
"""

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
API = ROOT / "src" / "api" / "main.py"

# Installed on the web service. Anything the API imports must be here, stdlib,
# or a dependency-free module of our own.
API_REQUIREMENTS = {
    "fastapi", "uvicorn", "psycopg", "psycopg_pool", "dotenv", "requests",
    "bs4", "jwt", "cryptography", "httpx",
    "pydantic",          # ships with fastapi
    "starlette",         # ditto
}
# Ours, and each one must stay free of scraper dependencies.
OUR_SLIM_MODULES = {"apns", "names", "config", "notify", "mockrace", "sessions",
                    "db", "standings"}


def imported_modules(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            # Relative: "..adapters.results_html" -> "adapters"
            out.add((node.module or "").split(".")[0])
    return {m for m in out if m}


def test_the_api_does_not_import_any_adapter():
    assert "adapters" not in imported_modules(API), (
        "src/api/main.py imports from src/adapters/, which needs RapidFuzz. "
        "That is not in requirements-api.txt and the deploy will fail. Put the "
        "shared logic in a dependency-free module (see src/sessions.py)."
    )


def test_the_api_does_not_import_the_rider_resolver():
    assert "resolve" not in imported_modules(API)


def test_sessions_is_safe_for_the_api_to_import():
    # The whole reason it exists. If this grows a dependency, the API breaks.
    mods = imported_modules(ROOT / "src" / "sessions.py")
    assert mods <= {"re"}, f"src/sessions.py must stay stdlib-only, imports {mods}"


def test_every_third_party_import_in_the_api_is_installed_there():
    stdlib = {
        "__future__", "ast", "asyncio", "base64", "collections", "concurrent",
        "contextlib", "csv", "datetime", "email", "functools", "hashlib",
        "html", "io", "itertools", "json", "logging", "math", "os", "pathlib",
        "random", "re", "socket", "ssl", "statistics", "string", "subprocess",
        "sys", "threading", "time", "traceback", "typing", "urllib", "uuid",
        "zoneinfo",
    }
    unknown = imported_modules(API) - stdlib - API_REQUIREMENTS - OUR_SLIM_MODULES
    assert not unknown, (
        f"src/api/main.py imports {unknown}, which are not in "
        "requirements-api.txt. Either add them there deliberately or keep them "
        "out of the web service."
    )
