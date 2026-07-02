"""Tests for the ``ingestion_worker.parsers`` back-compat shim.

``parsers.py`` is a 16-line module that simply re-exports
``anila_core.ingestion.parsers.extract_text`` for existing call sites.
These tests pin that contract:

* the symbol is importable from ``ingestion_worker.parsers``;
* it is the *same object* as ``anila_core.ingestion.parsers.extract_text``
  (i.e. a true re-export, not a copy/wrapper);
* the module's public surface is ``__all__ == ["extract_text"]``.
"""

import ingestion_worker.parsers as parsers_module
from anila_core.ingestion.parsers import extract_text as core_extract_text
from ingestion_worker.parsers import extract_text


def test_extract_text_is_importable():
    """The ``extract_text`` symbol can be imported from the shim."""
    assert extract_text is not None
    assert callable(extract_text)


def test_extract_text_is_same_object_as_core():
    """The shim re-exports the exact object from anila_core (identity)."""
    assert extract_text is core_extract_text
    assert parsers_module.extract_text is core_extract_text


def test_all_is_exactly_extract_text():
    """The module's public surface is limited to ``extract_text``."""
    assert parsers_module.__all__ == ["extract_text"]


def test_all_entries_are_real_module_attributes():
    """Everything advertised in ``__all__`` actually exists on the module."""
    for name in parsers_module.__all__:
        assert hasattr(parsers_module, name)
