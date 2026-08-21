def read_credentials(row: dict[str, object]) -> tuple[object, object | None]:
    return row["password"], row.get("api_key")
