def test_version() -> None:
    try:
        import leclerc_loom_server.version  # noqa: PLC0415
    except ImportError:
        raise AssertionError("version file not found") from None

    assert leclerc_loom_server.version.__all__ == ["__version__"]
    assert isinstance(leclerc_loom_server.version.__version__, str)
