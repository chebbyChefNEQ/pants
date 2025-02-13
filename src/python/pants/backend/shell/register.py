# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from pants.backend.shell import dependency_inference, shell_command, shunit2_test_runner, tailor
from pants.backend.shell.target_types import ShellCommand, ShellLibrary, Shunit2Tests
from pants.backend.shell.target_types import rules as target_types_rules


def target_types():
    return [ShellCommand, ShellLibrary, Shunit2Tests]


def rules():
    return [
        *dependency_inference.rules(),
        *shell_command.rules(),
        *shunit2_test_runner.rules(),
        *tailor.rules(),
        *target_types_rules(),
    ]
