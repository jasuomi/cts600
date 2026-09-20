"""Test setup for the integration.

pip install pytest-homeassistant-custom-component
cd homeassistant && python -m pytest tests
"""

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
