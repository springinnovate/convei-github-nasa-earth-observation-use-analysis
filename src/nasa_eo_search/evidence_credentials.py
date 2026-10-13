"""Load classification credentials without logging or persisting their contents."""

import os
from pathlib import Path


DEFAULT_OPENAI_KEY_FILE = Path("secrets/openai.key")
MAX_API_KEY_BYTES = 4096


def load_openai_api_key(api_key_file: Path | None = None) -> str:
    """Read a key from an explicit file, the environment, or the default file.

    An explicit file takes precedence over OPENAI_API_KEY and must be readable;
    it never silently falls back. Otherwise use a nonblank environment value,
    then secrets/openai.key relative to the current working directory. Read a
    bounded UTF-8 file, accepting a byte-order mark and surrounding whitespace.
    Reject blank, multiline, non-ASCII, or control-character credentials before
    constructing an HTTP header. Call only when a paid request is needed, never
    during preparation or a dry run. No key material is written or logged.

    Args:
        api_key_file: Optional explicit key-file path, relative to the current
            working directory or absolute. Contents must be only the raw key.

    Returns:
        The stripped credential string for an authorization header.

    Raises:
        ValueError: If no credential is available, the selected file is not a
            readable regular file, or its contents/environment value are invalid.
            Errors never include file contents or the underlying exception text.
    """
    environment_key = os.environ.get("OPENAI_API_KEY", "").strip() if api_key_file is None else ""
    if environment_key:
        api_key = environment_key
    else:
        selected_file = api_key_file if api_key_file is not None else DEFAULT_OPENAI_KEY_FILE
        try:
            if not selected_file.is_file():
                raise OSError("Not a regular file.")
            with selected_file.open("rb") as key_input:
                key_bytes = key_input.read(MAX_API_KEY_BYTES + 1)
        except OSError:
            raise ValueError(
                "Cannot read the API key file. Use --api-key-file PATH, save a key in "
                "./secrets/openai.key, or set OPENAI_API_KEY when no explicit file is selected."
            ) from None
        if len(key_bytes) > MAX_API_KEY_BYTES:
            raise ValueError(f"API key file exceeds the {MAX_API_KEY_BYTES}-byte limit.")
        try:
            api_key = key_bytes.decode("utf-8-sig").strip()
        except UnicodeDecodeError:
            raise ValueError("API key file must be UTF-8 text containing only the raw key.") from None
    if (not api_key or len(api_key) > MAX_API_KEY_BYTES
            or any(not 33 <= ord(character) <= 126 for character in api_key)):
        raise ValueError("API key must be a single nonempty ASCII token without whitespace or control characters.")
    return api_key
