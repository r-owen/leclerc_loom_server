import importlib.resources

from base_loom_server.app_runner import AppRunner
from fastapi import FastAPI

from .loom_server import LoomServer

PKG_NAME = "leclerc_loom_server"
PKG_FILES = importlib.resources.files(PKG_NAME)

app = FastAPI()


app_runner = AppRunner(
    app=app,
    server_class=LoomServer,
    favicon=PKG_FILES.joinpath("favicon-32x32.png").read_bytes(),
    app_package_name=f"{PKG_NAME}.main:app",
)


def run_leclerc_loom() -> None:
    """Run the Leclerc loom server."""
    app_runner.run()
