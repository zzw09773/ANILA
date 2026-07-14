from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_make_install_resolves_internal_contract_from_this_checkout():
    """The developer install must not let PyPI satisfy the internal name."""
    makefile = (PACKAGE_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "$(BIN)/pip install -e ../anila-contracts -e '.[dev]'" in makefile


def test_agent_metadata_keeps_contract_version_constraint():
    """Metadata remains explicit while Makefile controls source provenance."""
    pyproject = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"anila-contracts>=1.0.0,<2.0.0"' in pyproject
