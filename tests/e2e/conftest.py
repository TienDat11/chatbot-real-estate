"""Temporary e2e conftest: load repo .env so E2E aliases resolve. Delete after use."""

import dotenv

dotenv.load_dotenv(".env", override=True)
