from __future__ import annotations

from importlib import import_module, util
from pathlib import Path
from types import ModuleType


def _is_valid_extension(module: ModuleType) -> bool:
    return hasattr(module, "NodeConfig") and hasattr(module, "VeilNode")


def _load_extension_file(path: Path) -> ModuleType | None:
    module_name = f"{__package__}._veil_core_ext" if __package__ else "_veil_core_ext"
    spec = util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        return None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_extension() -> tuple[ModuleType | None, bool, str | None]:
    errors: list[str] = []

    for import_target, package in (
        ("._veil_core_ext", __package__),
        ("_veil_core_ext", None),
    ):
        try:
            module = import_module(import_target, package=package)
        except ImportError as exc:
            errors.append(f"{import_target}: {exc}")
            continue

        if _is_valid_extension(module):
            return module, True, None

        module_file = getattr(module, "__file__", None) or "<namespace package>"
        errors.append(
            f"{import_target}: imported {module_file} but it does not expose "
            "NodeConfig/VeilNode"
        )

    package_dir = Path(__file__).resolve().parent
    search_dirs = (
        package_dir,
        package_dir / "Release",
        package_dir / "Debug",
        package_dir / "RelWithDebInfo",
        package_dir / "MinSizeRel",
    )
    for search_dir in search_dirs:
        if not search_dir.is_dir():
            continue
        for path in sorted(search_dir.glob("_veil_core_ext*.pyd")) + sorted(search_dir.glob("_veil_core_ext*.so")):
            try:
                module = _load_extension_file(path)
            except ImportError as exc:
                errors.append(f"{path}: {exc}")
                continue
            if module is None:
                errors.append(f"{path}: could not create import spec")
                continue
            if _is_valid_extension(module):
                return module, True, None
            errors.append(f"{path}: loaded but it does not expose NodeConfig/VeilNode")

    return None, False, "; ".join(errors)
