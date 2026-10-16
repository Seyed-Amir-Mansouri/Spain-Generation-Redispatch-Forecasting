import dataclasses

import pytest

from congestion_es.config import DEFAULT_CONFIG, load_config


@pytest.fixture
def config():
    """Load the Spanish config with fewer boosting rounds."""
    loaded = load_config(DEFAULT_CONFIG)
    return dataclasses.replace(loaded, model=dataclasses.replace(loaded.model, max_iter=40, test_days=10))
