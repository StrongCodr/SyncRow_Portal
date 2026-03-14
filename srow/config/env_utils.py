"""Environment file loading utilities.

Provides a simple .env file parser that supports:
- Comments (lines starting with #)
- export keyword (export FOO=bar)
- Quoted values (single or double quotes)
- Inline comments (FOO=bar # comment)

Does not override existing environment variables.
"""

import os
from typing import MutableMapping


def load_env_file(
    path: str = ".env",
    environ: MutableMapping[str, str] | None = None,
) -> None:
    """Load environment variables from a .env file.

    Args:
        path: Path to the .env file. Defaults to ".env".
        environ: Environment mapping to update. Defaults to os.environ.

    Note:
        Does not override existing environment variables.
        Silently skips if the file does not exist.
    """
    if environ is None:
        environ = os.environ

    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()

            # Skip empty lines and comments
            if not line or line.startswith("#"):
                continue

            # Handle export keyword
            if line.startswith("export "):
                line = line[len("export "):].strip()

            # Must have an equals sign
            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()

            if not key:
                continue

            value = value.strip()

            # Handle quoted values
            if value and value[0] in ("'", '"'):
                quote = value[0]
                if value.endswith(quote):
                    value = value[1:-1]
            else:
                # Strip inline comments for unquoted values
                if " #" in value:
                    value = value.split(" #", 1)[0].rstrip()

            # setdefault: don't override existing env vars
            environ.setdefault(key, value)
