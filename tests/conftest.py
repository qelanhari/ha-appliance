"""Fixtures for the tests that need a real Home Assistant runtime.

The detection suites (``test_detector.py``, ``test_samples.py``,
``test_fingerprints.py``) need none of this: they import the ``logic`` package
directly and run under plain pytest. Only the wiring tests below require
``pytest-homeassistant-custom-component``, and they skip themselves when it is
absent.
"""

import pytest


def _ha_plugin_available() -> bool:
    try:
        import pytest_homeassistant_custom_component  # noqa: F401
    except ImportError:
        return False
    return True


if _ha_plugin_available():

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):
        """Without this, Home Assistant refuses to load a custom component."""
        yield
