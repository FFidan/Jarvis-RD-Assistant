"""Opt-in smoke tests for Marker/Surya dependency compatibility."""

from __future__ import annotations

import importlib.metadata

import pytest


@pytest.mark.integration
def test_locked_surya_recognition_config_loads() -> None:
    """The locked Marker/Surya/Transformers set must load Surya OCR config."""
    from surya.recognition.model.config import SuryaOCRConfig
    from surya.settings import settings

    config = SuryaOCRConfig.from_pretrained(settings.RECOGNITION_MODEL_CHECKPOINT)

    assert importlib.metadata.version("marker-pdf")
    assert importlib.metadata.version("surya-ocr")
    assert importlib.metadata.version("transformers")
    assert isinstance(config.encoder, dict)
    assert isinstance(config.decoder, dict)
    assert isinstance(config.text_encoder, dict)
