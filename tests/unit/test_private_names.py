from __future__ import annotations

import ast
from pathlib import Path

import pytest

# ruff's PLC2701 does not flag a private import inside the same top-level package, so this
# test enforces private-by-default within `pan` (decisions/0003-lint-rules.md): a name with a
# leading underscore is used only by the module that defines it.
_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"


def _is_private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _module_name(source_root: Path, path: Path) -> str:
    parts = list(path.relative_to(source_root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_module(source_root: Path, dotted_name: str) -> bool:
    module_path = source_root.joinpath(*dotted_name.split("."))
    return module_path.with_suffix(".py").is_file() or (module_path / "__init__.py").is_file()


def _imported_from(node: ast.ImportFrom, module_name: str, *, is_package: bool) -> str:
    if node.level == 0:
        return node.module or ""
    package_parts = module_name.split(".")
    if not is_package:
        package_parts.pop()
    base_parts = package_parts[: len(package_parts) - (node.level - 1)]
    return ".".join([*base_parts, *([node.module] if node.module else [])])


def _import_uses(
    node: ast.Import, package: str, module_aliases: dict[str, str]
) -> list[tuple[int, str]]:
    uses: list[tuple[int, str]] = []
    for alias in node.names:
        if not alias.name.startswith(f"{package}."):
            continue
        if any(_is_private(part) for part in alias.name.split(".")):
            uses.append((node.lineno, f"import {alias.name}"))
        if alias.asname is not None:
            module_aliases[alias.asname] = alias.name
    return uses


def _import_from_uses(
    node: ast.ImportFrom,
    *,
    source_root: Path,
    package: str,
    module_name: str,
    is_package: bool,
    module_aliases: dict[str, str],
) -> list[tuple[int, str]]:
    source = _imported_from(node, module_name, is_package=is_package)
    if source != package and not source.startswith(f"{package}."):
        return []
    uses: list[tuple[int, str]] = []
    for alias in node.names:
        target = f"{source}.{alias.name}"
        if _is_module(source_root, target):
            module_aliases[alias.asname or alias.name] = target
        if _is_private(alias.name) and source != module_name:
            uses.append((node.lineno, f"from {source} import {alias.name}"))
    return uses


def _attribute_uses(
    tree: ast.Module, module_name: str, module_aliases: dict[str, str]
) -> list[tuple[int, str]]:
    return [
        (node.lineno, f"{module_aliases[node.value.id]}.{node.attr}")
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in module_aliases
        and _is_private(node.attr)
        and module_aliases[node.value.id] != module_name
    ]


def _module_uses(source_root: Path, path: Path, package: str) -> list[tuple[int, str]]:
    module_name = _module_name(source_root, path)
    tree = ast.parse(path.read_text(), filename=str(path))
    module_aliases: dict[str, str] = {}
    uses: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            uses.extend(_import_uses(node, package, module_aliases))
        elif isinstance(node, ast.ImportFrom):
            uses.extend(
                _import_from_uses(
                    node,
                    source_root=source_root,
                    package=package,
                    module_name=module_name,
                    is_package=path.name == "__init__.py",
                    module_aliases=module_aliases,
                )
            )
    uses.extend(_attribute_uses(tree, module_name, module_aliases))
    return uses


def private_name_uses(source_root: Path, package: str = "pan") -> list[str]:
    """Every use of another `package` module's private name, as `file:line: description`."""
    return [
        f"{path.relative_to(source_root)}:{line}: {description}"
        for path in sorted((source_root / package).rglob("*.py"))
        for line, description in _module_uses(source_root, path, package)
    ]


def test_no_pan_module_uses_another_modules_private_names() -> None:
    assert private_name_uses(_SOURCE_ROOT) == []


@pytest.mark.parametrize(
    "source, caught",
    [
        ("from pan.a import _hidden\n", True),
        ("from .a import _hidden\n", True),
        ("import pan._internal\n", True),
        ("from pan import a\n\na._hidden()\n", True),
        ("import pan.a as a_module\n\na_module._hidden()\n", True),
        ("from pan.a import shown\n", False),
        ("from pan.a import __version__\n", False),
        ("from os import _exit\n", False),
    ],
    ids=[
        "absolute-import",
        "relative-import",
        "private-module",
        "attribute-on-module",
        "attribute-on-aliased-module",
        "public-name",
        "dunder-name",
        "outside-pan",
    ],
)
def test_private_name_uses_catches_another_modules_private_names(
    tmp_path: Path, source: str, caught: bool
) -> None:
    package = tmp_path / "pan"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "a.py").write_text("shown = 1\n_hidden = 2\n_hidden_alone = _hidden\n")
    (package / "b.py").write_text(source)

    offenders = private_name_uses(tmp_path)

    assert [offender.startswith("pan/b.py:") for offender in offenders] == (
        [True] if caught else []
    )
