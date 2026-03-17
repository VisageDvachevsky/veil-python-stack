from __future__ import annotations

from importlib import import_module
from types import ModuleType


def _is_valid_extension(module: ModuleType) -> bool:
    return hasattr(module, "NodeConfig") and hasattr(module, "VeilNode")


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

    return None, False, "; ".join(errors)
