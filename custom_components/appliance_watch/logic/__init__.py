"""Pure detection logic — no Home Assistant import anywhere under this package.

Keeping it a self-contained subpackage is what lets the test suite replay real
power traces under plain ``pytest``, without a Home Assistant runtime: the
tests put ``custom_components/appliance_watch`` on ``sys.path`` and import
``logic.detector`` directly.
"""
