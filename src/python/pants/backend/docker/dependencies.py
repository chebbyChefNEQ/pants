# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).


from pants.backend.docker.parser import DockerfileInfo, DockerfileParseRequest
from pants.backend.docker.target_types import DockerDependencies, DockerImageSources
from pants.backend.python.goals.package_pex_binary import PexBinaryFieldSet
from pants.engine.addresses import Address, Addresses, UnparsedAddressInputs
from pants.engine.rules import Get, collect_rules, rule
from pants.engine.target import (
    InjectDependenciesRequest,
    InjectedDependencies,
    Targets,
    WrappedTarget,
)
from pants.engine.unions import UnionRule


class InjectDockerDependencies(InjectDependenciesRequest):
    inject_for = DockerDependencies


@rule
async def inject_docker_dependencies(request: InjectDockerDependencies) -> InjectedDependencies:
    """Inspects COPY instructions in the Dockerfile for references to known targets."""
    original_tgt = await Get(WrappedTarget, Address, request.dependencies_field.address)
    sources = original_tgt.target[DockerImageSources]
    if not sources.value:
        return InjectedDependencies()

    dockerfile = await Get(DockerfileInfo, DockerfileParseRequest(sources))
    targets = await Get(
        Targets,
        UnparsedAddressInputs(
            dockerfile.putative_target_addresses,
            owning_address=None,
        ),
    )

    return InjectedDependencies(
        Addresses([tgt.address for tgt in targets if PexBinaryFieldSet.is_applicable(tgt)])
    )


def rules():
    return [
        *collect_rules(),
        UnionRule(InjectDependenciesRequest, InjectDockerDependencies),
    ]
