import logging

import pytest


def _reset():
    import logging

    import nordicintel_harvest.core.config as cfg
    import nordicintel_harvest.core.logging as lc

    lc._CONFIGURED = False
    cfg.get_settings.cache_clear()

    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.StreamHandler):
            root.removeHandler(h)
            h.close()

    request_manager_logger = logging.getLogger("RequestManager")
    for h in list(request_manager_logger.handlers):
        request_manager_logger.removeHandler(h)
        h.close()


@pytest.mark.parametrize(
    ("environment", "expected_level", "expected_level_field"),
    [
        ("dev", logging.DEBUG, "%(levelname)-8s"),
        ("production", logging.INFO, "%(levelname)s"),
    ],
)
def test_configure_logging_uses_environment_format_and_console_only(
    monkeypatch, environment, expected_level, expected_level_field
):
    _reset()
    monkeypatch.setenv("ENVIRONMENT", environment)
    from nordicintel_harvest.core.logging import configure_logging

    configure_logging()
    root = logging.getLogger()
    stream_handlers = [
        handler for handler in root.handlers if type(handler) is logging.StreamHandler
    ]
    assert len(stream_handlers) == 1
    assert not any(isinstance(handler, logging.FileHandler) for handler in root.handlers)
    assert logging.getLogger("RequestManager").handlers == []
    assert root.level == expected_level
    assert stream_handlers[0].formatter is not None
    assert expected_level_field in stream_handlers[0].formatter._fmt


def test_configure_logging_is_idempotent():
    _reset()
    from nordicintel_harvest.core.logging import configure_logging

    configure_logging()
    configure_logging()
    root = logging.getLogger()
    stream_handlers = [h for h in root.handlers if type(h) is logging.StreamHandler]
    assert len(stream_handlers) == 1


def test_configure_logging_format_contains_levelname():
    _reset()
    from nordicintel_harvest.core.logging import configure_logging

    configure_logging()
    root = logging.getLogger()
    stream_handlers = [h for h in root.handlers if type(h) is logging.StreamHandler]
    assert stream_handlers
    fmt = stream_handlers[0].formatter._fmt if stream_handlers[0].formatter else ""
    assert "%(levelname)" in fmt  # type: ignore #
