def read_bootstrap_secret(config: object) -> object:
    return getattr(config, "password")  # noqa: B009 - R2 getattr fixture
