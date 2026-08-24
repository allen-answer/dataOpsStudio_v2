from pathlib import Path

import yaml  # type: ignore[import-untyped]

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_dependabot_version_updates_are_paused_for_all_ecosystems() -> None:
    config = yaml.safe_load((_REPO_ROOT / ".github" / "dependabot.yml").read_text("utf-8"))

    limits = {
        (update["package-ecosystem"], update["directory"]): update.get("open-pull-requests-limit")
        for update in config["updates"]
    }

    assert limits == {
        ("uv", "/"): 0,
        ("npm", "/frontend"): 0,
        ("github-actions", "/"): 0,
    }
