"""Fixtures for the tests that need a real Home Assistant runtime.

The detection suites (``test_detector.py``, ``test_samples.py``,
``test_fingerprints.py``) need none of this: they import the ``logic`` package
directly and run under plain pytest. Only the wiring tests below require
``pytest-homeassistant-custom-component``, and they skip themselves when it is
absent.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "custom_components" / "appliance_watch"))

from logic.cost import CycleAccumulator  # noqa: E402
from logic.loads.pool import Inputs as PoolInputs  # noqa: E402
from logic.loads.pool import Thresholds as PoolThresholds  # noqa: E402
from logic.loads.water_heater import Inputs, Thresholds  # noqa: E402


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


# ---------------------------------------------------------------------------
# Fixtures reprises telles quelles de ha-water-heater : elles définissent la
# journée de référence sur laquelle les 94 cas portés ont été écrits.
# ---------------------------------------------------------------------------

@pytest.fixture
def t0() -> datetime:
    """Reference time: Tuesday 2026-06-16 14:00 UTC (HP window, summer)."""
    return datetime(2026, 6, 16, 14, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def thr() -> Thresholds:
    return Thresholds()


@pytest.fixture
def base_inputs(t0: datetime) -> Inputs:
    """Mid-afternoon summer day: warm-ish tank, neutral grid, no Tempo Rouge."""
    return Inputs(
        now=t0,
        mode="auto",
        tank_top_c=52.0,
        tank_middle_c=50.0,
        garage_c=18.0,
        outdoor_c=25.0,
        grid_smooth_w=0.0,
        pv_power_w=2000.0,
        heater_power_w=0.0,
        tempo_color="Bleu",
        tempo_next_color="Bleu",
        is_hc=False,
        forecast_today_kwh=15.0,           # ample sun
        forecast_tomorrow_kwh=15.0,
        energy_needed_kwh=1.5,             # small top-up
        cycle=CycleAccumulator(),
        signal_on_at=None,
        signal_currently_on=False,
    )


# ---------------------------------------------------------------------------
# Fixtures reprises de ha-pool, préfixées : les deux portages nommaient
# leurs fixtures pareil.
# ---------------------------------------------------------------------------

@pytest.fixture
def pool_t0() -> datetime:
    """Reference instant for the pool cases."""
    return datetime(2026, 6, 15, 14, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def pool_thr() -> PoolThresholds:
    """Pool thresholds — the CS100 / Hayward VSTD defaults."""
    return PoolThresholds()


@pytest.fixture
def pool_inputs(pool_t0: datetime) -> PoolInputs:
    """A 'normal afternoon' baseline: daylight, balanced grid, pump v1, no v3 history."""
    return PoolInputs(
        now=pool_t0,
        daylight=True,
        grid_w=0.0,
        pump_speed=1,
        water_temp_c=22.0,
        air_temp_c=25.0,
        mode="auto",
        v3_started_at=None,
        v3_last_ended_at=None,
        force_skim_requested=False,
    )
