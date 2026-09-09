import pytest

from api.infrastructure.config.config import Settings


@pytest.mark.parametrize(
    "secret", ["", "change-me", "rag-real-estate-lead-mirror-default-secret", "short"]
)
def test_production_rejects_missing_or_weak_lead_mirror_secret(secret: str) -> None:
    with pytest.raises(ValueError, match="LEAD_MIRROR_HMAC_SECRET"):
        Settings(
            _env_file=None,
            app_env="production",
            lead_mirror_hmac_secret=secret,
            anon_identity_secret="a" * 48,
            llm_api_key="configured",
            postgres_password="configured",
        )


def test_production_accepts_explicit_strong_lead_mirror_secret() -> None:
    settings = Settings(
        _env_file=None,
        app_env="production",
        cors_origins=["https://app.example.com"],
        lead_mirror_hmac_secret="lead-mirror-test-fixture-" + "x" * 32,
        anon_identity_secret="a" * 48,
        llm_api_key="configured",
        postgres_password="configured",
    )
    assert len(settings.lead_mirror_hmac_secret) >= 32


# --- CORS allowlist safety (reviewer blocker) ---


def test_production_rejects_missing_cors_origins() -> None:
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        Settings(
            _env_file=None,
            app_env="production",
            cors_origins=None,
            lead_mirror_hmac_secret="lead-mirror-test-fixture-" + "x" * 32,
            anon_identity_secret="a" * 48,
            llm_api_key="configured",
            postgres_password="configured",
        )


def test_production_rejects_empty_cors_origins() -> None:
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        Settings(
            _env_file=None,
            app_env="production",
            cors_origins=[],
            lead_mirror_hmac_secret="lead-mirror-test-fixture-" + "x" * 32,
            anon_identity_secret="a" * 48,
            llm_api_key="configured",
            postgres_password="configured",
        )


@pytest.mark.parametrize("app_env", ["production", "development", "test"])
def test_wildcard_cors_origins_rejected_in_every_environment(app_env: str) -> None:
    kwargs = {"_env_file": None, "app_env": app_env, "cors_origins": ["*"]}
    if app_env == "production":
        # Production needs its required secrets so the CORS wildcard check is
        # the validator that rejects the settings (validators run in order).
        kwargs.update(
            lead_mirror_hmac_secret="lead-mirror-test-fixture-" + "x" * 32,
            anon_identity_secret="a" * 48,
            llm_api_key="configured",
            postgres_password="configured",
        )
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        Settings(**kwargs)


def test_development_allows_safe_localhost_default() -> None:
    settings = Settings(_env_file=None, app_env="development", cors_origins=None)
    assert settings.cors_origins == ["http://localhost:3000"]


def test_development_allows_explicit_safe_origins() -> None:
    settings = Settings(
        _env_file=None,
        app_env="development",
        cors_origins=["https://app.example.com", "http://localhost:5173"],
    )
    assert settings.cors_origins == [
        "https://app.example.com",
        "http://localhost:5173",
    ]


def test_production_accepts_explicit_non_wildcard_origins() -> None:
    settings = Settings(
        _env_file=None,
        app_env="production",
        cors_origins=["https://app.example.com"],
        lead_mirror_hmac_secret="lead-mirror-test-fixture-" + "x" * 32,
        anon_identity_secret="a" * 48,
        llm_api_key="configured",
        postgres_password="configured",
    )
    assert settings.cors_origins == ["https://app.example.com"]


# --- LLM reasoning-effort env contract (thinking-token control) ---


def test_llm_reasoning_effort_defaults_to_low() -> None:
    assert Settings(_env_file=None).llm_reasoning_effort == "low"


@pytest.mark.parametrize("value", ["none", "low", "medium", "high"])
def test_llm_reasoning_effort_accepts_valid_values(value: str) -> None:
    assert Settings(_env_file=None, llm_reasoning_effort=value).llm_reasoning_effort == value


@pytest.mark.parametrize("value", ["ultra", "xhigh", ""])
def test_llm_reasoning_effort_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError, match="LLM_REASONING_EFFORT"):
        Settings(_env_file=None, llm_reasoning_effort=value)
