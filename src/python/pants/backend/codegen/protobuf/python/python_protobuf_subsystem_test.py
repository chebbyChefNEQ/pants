# Copyright 2020 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from pants.backend.codegen.protobuf.python import python_protobuf_subsystem
from pants.backend.codegen.protobuf.python.python_protobuf_subsystem import (
    InjectPythonProtobufDependencies,
)
from pants.backend.codegen.protobuf.target_types import ProtobufDependencies, ProtobufLibrary
from pants.core.target_types import Files
from pants.engine.addresses import Address
from pants.engine.target import InjectedDependencies
from pants.testutil.rule_runner import QueryRule, RuleRunner


def test_inject_dependencies() -> None:
    rule_runner = RuleRunner(
        rules=[
            *python_protobuf_subsystem.rules(),
            QueryRule(InjectedDependencies, (InjectPythonProtobufDependencies,)),
        ],
        target_types=[ProtobufLibrary, Files],
    )
    rule_runner.set_options(
        [
            "--backend-packages=pants.backend.codegen.protobuf.python",
            "--python-protobuf-runtime-dependencies=protos:injected_dep",
        ]
    )
    # Note that injected deps can be any target type for `--python-protobuf-runtime-dependencies`.
    rule_runner.write_files(
        {"protos/BUILD": "protobuf_library()\nfiles(name='injected_dep', sources=[])"}
    )
    tgt = rule_runner.get_target(Address("protos"))
    injected = rule_runner.request(
        InjectedDependencies, [InjectPythonProtobufDependencies(tgt[ProtobufDependencies])]
    )
    assert injected == InjectedDependencies([Address("protos", target_name="injected_dep")])
