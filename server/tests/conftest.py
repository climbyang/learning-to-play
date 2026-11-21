import pytest

import ltp.config


@pytest.fixture(autouse=True)
def _fixed_test_timezone(monkeypatch):
    """Tests are written against a fixed UTC+10 local day; pin it regardless of the default."""
    monkeypatch.setattr(ltp.config, "DEFAULT_TIMEZONE", "Etc/GMT-10")
