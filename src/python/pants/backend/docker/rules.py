# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from pants.backend.docker.dependencies import rules as dependencies_rules
from pants.backend.docker.docker_binary import rules as binary_rules
from pants.backend.docker.docker_build import rules as build_rules
from pants.backend.docker.docker_build_context import rules as context_rules
from pants.backend.docker.parser import rules as parser_rules


def rules():
    return [
        *binary_rules(),
        *build_rules(),
        *context_rules(),
        *dependencies_rules(),
        *parser_rules(),
    ]
