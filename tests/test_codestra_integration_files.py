from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate-codestra-integration.py"
SPEC = spec_from_file_location("validate_codestra_integration", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_codestra_integration_files_are_fail_closed() -> None:
    MODULE.validate()
