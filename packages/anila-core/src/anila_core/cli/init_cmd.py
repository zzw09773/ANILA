"""anila-core init — scaffold a new ANILA agent project.

Creates a new directory with all the boilerplate needed to build,
run, and register an agent on the ANILA platform.
"""

from __future__ import annotations

import importlib.resources
import re
import shutil
import sys
from pathlib import Path


_TEMPLATE_PKG = "anila_core.cli.templates.agent-template"
_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def _render(text: str, variables: dict[str, str]) -> str:
    return _PLACEHOLDER_RE.sub(lambda m: variables.get(m.group(1), m.group(0)), text)


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")


def run(args: list[str]) -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="anila-core init",
        description="Scaffold a new ANILA agent project.",
    )
    parser.add_argument("name", nargs="?", help="Agent name (slug, e.g. hr-policy-agent)")
    parser.add_argument(
        "--description", "-d",
        default="",
        help="Short description for the Router LLM (used in agent manifest).",
    )
    parser.add_argument(
        "--endpoint", "-e",
        default="http://localhost:9100",
        help="Default endpoint URL for the agent (can be changed in anila.yaml later).",
    )
    parser.add_argument(
        "--output", "-o",
        default=".",
        help="Parent directory to create the project in (default: current dir).",
    )
    parsed = parser.parse_args(args)

    # Prompt for name if not provided
    name = parsed.name
    if not name:
        name = input("Agent name (slug, e.g. hr-policy-agent): ").strip()
        if not name:
            print("error: agent name is required", file=sys.stderr)
            sys.exit(1)

    slug = _slugify(name)
    display_name = slug.replace("-", " ").title()

    description = parsed.description
    if not description:
        description = input(
            f"Description for Router (what does {display_name} do?): "
        ).strip()
        if not description:
            description = f"A custom ANILA agent: {display_name}"

    output_dir = Path(parsed.output) / slug
    if output_dir.exists():
        print(f"error: directory '{output_dir}' already exists", file=sys.stderr)
        sys.exit(1)

    variables = {
        "AGENT_NAME": slug,
        "AGENT_DISPLAY_NAME": display_name,
        "AGENT_DESCRIPTION": description,
        "ENDPOINT_URL": parsed.endpoint,
    }

    _scaffold(output_dir, variables)

    print(f"\n✓ Created agent project: {output_dir}/")
    print(f"\nNext steps:")
    print(f"  cd {output_dir}")
    print(f"  cp .env.example .env  # fill in CSP_BASE_URL, CSP_API_KEY")
    print(f"  pip install -r requirements.txt")
    print(f"  API_DEV_MODE=true uvicorn agent:app --reload --port 9100")
    print(f"  # When ready to publish:")
    print(f"  anila-core register")


def _scaffold(output_dir: Path, variables: dict[str, str]) -> None:
    """Copy template files into output_dir, rendering placeholders."""
    output_dir.mkdir(parents=True)

    # Prefer filesystem path (works in both editable installs and wheels)
    fs_path = Path(__file__).parent / "templates" / "agent-template"
    if fs_path.exists():
        _copy_tree_path(fs_path, output_dir, variables)
        return

    try:
        template_path = importlib.resources.files(_TEMPLATE_PKG)
        _copy_tree(template_path, output_dir, variables)
    except (ModuleNotFoundError, TypeError):
        print("error: template directory not found", file=sys.stderr)
        sys.exit(1)


def _copy_tree(src, dst: Path, variables: dict[str, str]) -> None:
    """Recursively copy importlib.resources tree, rendering placeholders."""
    for item in src.iterdir():
        dest_item = dst / item.name
        if item.is_dir():
            dest_item.mkdir(exist_ok=True)
            _copy_tree(item, dest_item, variables)
        else:
            content = item.read_text(encoding="utf-8")
            dest_item.write_text(_render(content, variables), encoding="utf-8")


def _copy_tree_path(src: Path, dst: Path, variables: dict[str, str]) -> None:
    """Recursively copy from a filesystem path, rendering placeholders."""
    for item in src.iterdir():
        dest_item = dst / item.name
        if item.is_dir():
            dest_item.mkdir(exist_ok=True)
            _copy_tree_path(item, dest_item, variables)
        else:
            try:
                content = item.read_text(encoding="utf-8")
                dest_item.write_text(_render(content, variables), encoding="utf-8")
            except UnicodeDecodeError:
                shutil.copy2(item, dest_item)
