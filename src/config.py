"""Application configuration.

Loads environment variables from a local .env file (via python-dotenv) and
exposes them to the rest of the app. Secrets live in .env only — never in code.
"""

import os

from dotenv import load_dotenv

# Load .env from the project root if present. Safe to call when the file is
# missing — it simply does nothing.
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")


def get_database_url() -> str:
    """Return DATABASE_URL or raise a clear error if it isn't set.

    Use this anywhere a database connection is needed so failures are obvious.
    """
    # strip() guards against invisible whitespace/newlines that sneak in when
    # the value is pasted into a secrets UI (this broke every CI run once).
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and paste your "
            "Neon/Supabase connection string into it."
        )
    return url
