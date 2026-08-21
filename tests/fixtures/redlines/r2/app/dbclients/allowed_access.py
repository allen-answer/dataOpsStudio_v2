from typing import Any


def connect(config: Any) -> tuple[object, object]:
    return config.password, config["password"]
