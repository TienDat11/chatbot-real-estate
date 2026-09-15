"""Clean Architecture dependency regression guard (LGN-P0-003).

Forbidden edges are discovered by AST inspection of the source tree; the
application is never imported to inspect its own dependencies. The baseline in
``current_debt.json`` is a CEILING, not a snapshot: the suite fails only when a
forbidden edge appears that is not already recorded, so paying debt down passes
without editing the baseline while new debt cannot be waved through by rewriting
it upward.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = Path(__file__).with_name("current_debt.json")

# Layers scanned, and the layers each of them may not import. Infrastructure is
# the outermost layer and may import anything, so it is never a source here.
SCANNED_LAYERS = ("domain", "application")
FORBIDDEN_TARGETS = {
    "domain": frozenset({"application", "interfaces", "infrastructure"}),
    "application": frozenset({"interfaces"}),
}


@dataclass(frozen=True, order=True)
class DependencyViolation:
    """One forbidden edge: a source file importing a module in a banned layer.

    Identity is the (source, imported module) pair. Line numbers and individual
    imported symbols are deliberately excluded: re-importing the same module a
    second time, at module level or inside a function, is still one edge.
    """

    source: str
    imported: str

    @property
    def target_layer(self) -> str:
        return self.imported.split(".")[1]


def _imported_modules(tree: ast.AST, package: tuple[str, ...]) -> set[str]:
    """Every module the file imports, with relative imports resolved.

    ``package`` is the dotted package containing the file, so a level-1 relative
    import resolves to that package, level-2 to its parent, and so on.

    ``from base import name`` additionally binds ``base.name`` when ``base``
    resolves to the bare ``api`` package: that form reaches a forbidden layer
    through the imported name alone. Once ``base`` already names a layer it is
    the edge itself, and qualifying each name would only invent symbols
    (``...dependencies.get_llm``) rather than modules.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                parts = (*base, *(node.module.split(".") if node.module else ()))
            elif node.module:
                parts = tuple(node.module.split("."))
            else:
                continue
            found.add(".".join(parts))
            if len(parts) < 2:
                found.update(
                    ".".join((*parts, alias.name))
                    for alias in node.names
                    if alias.name != "*"
                )
    return found


def _scan_forbidden_dependencies(root: Path) -> set[DependencyViolation]:
    """Return every forbidden layer edge under ``root`` (pure; no imports)."""
    violations: set[DependencyViolation] = set()
    for layer in SCANNED_LAYERS:
        for path in sorted((root / "api" / layer).rglob("*.py")):
            # Parse errors propagate: a file this guard cannot read must fail the
            # run, never silently drop its imports and let a violation through.
            # utf-8-sig mirrors Python's own import: it strips a leading BOM, so a
            # BOM-prefixed module is scanned instead of being mistaken for broken.
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            package = path.relative_to(root).parts[:-1]
            for module in _imported_modules(tree, package):
                parts = module.split(".")
                if len(parts) < 2 or parts[0] != "api":
                    continue
                if parts[1] in FORBIDDEN_TARGETS[layer]:
                    violations.add(
                        DependencyViolation(
                            source=path.relative_to(root).as_posix(),
                            imported=module,
                        )
                    )
    return violations


def _load_baseline() -> set[DependencyViolation]:
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    return {DependencyViolation(edge["source"], edge["imported"]) for edge in data["edges"]}


def test_no_new_forbidden_dependency_edges():
    """Current violations must be a subset of the recorded ceiling."""
    new = sorted(_scan_forbidden_dependencies(REPO_ROOT) - _load_baseline())
    assert not new, (
        "New Clean Architecture dependency violation(s). Introduce or reuse a port "
        "in api/application/ports instead of importing across layers:\n"
        + "\n".join(f"  {v.source} -> {v.imported}" for v in new)
    )


def test_baseline_entries_are_structurally_forbidden_edges():
    """Every recorded entry has a banned layer direction.

    Structural only: it does not assert the edge still exists, because paying
    debt down must pass under ceiling semantics without editing the baseline.
    """
    for edge in sorted(_load_baseline()):
        source_layer = edge.source.split("/")[1]
        assert source_layer in FORBIDDEN_TARGETS, f"baseline source not scanned: {edge.source}"
        assert edge.target_layer in FORBIDDEN_TARGETS[source_layer], (
            f"baseline records an allowed import as debt: {edge.source} -> {edge.imported}"
        )


def test_scanner_flags_temporary_violation_then_clears_it(tmp_path):
    """Acceptance: a temporary violating module fails the scan, removal passes."""
    bad = tmp_path / "api" / "domain" / "bad_dependency.py"
    bad.parent.mkdir(parents=True)
    bad.write_text("from api.infrastructure.dependencies import get_llm\n", encoding="utf-8")

    detected = _scan_forbidden_dependencies(tmp_path)
    assert DependencyViolation(
        "api/domain/bad_dependency.py", "api.infrastructure.dependencies"
    ) in detected

    bad.unlink()
    assert _scan_forbidden_dependencies(tmp_path) == set()


def test_scanner_catches_function_local_and_relative_imports(tmp_path):
    """Function-local and relative imports are edges too, not only module-level ones."""
    services = tmp_path / "api" / "domain" / "services"
    services.mkdir(parents=True)
    (services / "local_import.py").write_text(
        "def run():\n"
        "    from api.infrastructure.dependencies import get_llm\n"
        "    return get_llm\n",
        encoding="utf-8",
    )
    (services / "relative_import.py").write_text(
        "from ...infrastructure.dependencies import get_llm\n",
        encoding="utf-8",
    )

    detected = _scan_forbidden_dependencies(tmp_path)
    assert DependencyViolation(
        "api/domain/services/local_import.py", "api.infrastructure.dependencies"
    ) in detected
    assert DependencyViolation(
        "api/domain/services/relative_import.py", "api.infrastructure.dependencies"
    ) in detected


def test_scanner_fails_closed_on_unparsable_file(tmp_path):
    """An unreadable module must fail the scan, not be silently skipped."""
    services = tmp_path / "api" / "domain" / "services"
    services.mkdir(parents=True)
    (services / "broken.py").write_text("def broken(:\n", encoding="utf-8")

    with pytest.raises(SyntaxError):
        _scan_forbidden_dependencies(tmp_path)


def test_scanner_catches_package_form_imports(tmp_path):
    """`from api import infrastructure` and its relative form reach a banned layer."""
    domain = tmp_path / "api" / "domain"
    services = domain / "services"
    services.mkdir(parents=True)
    (domain / "package_import.py").write_text(
        "from api import infrastructure\n",
        encoding="utf-8",
    )
    # Level 3 from api/domain/services resolves to `api`, matching
    # `from ...infrastructure.dependencies import ...` in the real tree.
    (services / "relative_package_import.py").write_text(
        "from ... import infrastructure\n",
        encoding="utf-8",
    )

    detected = _scan_forbidden_dependencies(tmp_path)
    assert DependencyViolation(
        "api/domain/package_import.py", "api.infrastructure"
    ) in detected
    assert DependencyViolation(
        "api/domain/services/relative_package_import.py", "api.infrastructure"
    ) in detected
