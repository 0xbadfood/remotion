"""Shared test guards."""
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def no_real_mode_switch():
    """Tests never reach the amdserver's mode switch. On 2026-10-22 a pipeline test's image stage called
    ensure_image_mode with the default settings, found the amdserver lent to the digest in LLM mode, and switched
    it to image mode in the middle of a digest run. Tests that check switching patch switch_mode themselves."""
    with patch("infofilm.video_pool.switch_mode", return_value=None):
        yield
