"""Capture the application logger without changing production propagation."""

import logging

import pytest


@pytest.fixture
def caplog(caplog):
    logger = logging.getLogger("index-bridge")
    logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)
