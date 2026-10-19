"""Build the distribution on one live dependency graph."""

from pathlib import Path

from agent.composition import CompositionRoot
from agent.config import load_dotenv, load_toml_config
from bootstrap.application import Application
from bootstrap.distribution import modules


async def build_runtime(
    workdir: Path,
    *,
    enable_web: bool = False,
    enable_telegram: bool = False,
    enable_qq: bool = False,
    enable_qqbot: bool = False,
    config_path: Path | None = None,
) -> Application:
    load_dotenv(workdir / ".env")
    config = load_toml_config(config_path or workdir / "config.toml")
    root = CompositionRoot()
    await root.start(
        modules(workdir, config, (enable_web, enable_telegram, enable_qq, enable_qqbot))
    )
    try:
        return Application.from_composition(root)
    except BaseException:
        await root.close()
        raise
