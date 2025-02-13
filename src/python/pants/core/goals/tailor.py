# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import dataclasses
import itertools
import os
from abc import ABCMeta
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Set, Tuple, Type, cast

from pants.base.specs import (
    AddressSpecs,
    AscendantAddresses,
    MaybeEmptyDescendantAddresses,
    Spec,
    Specs,
)
from pants.build_graph.address import Address
from pants.engine.collection import DeduplicatedCollection
from pants.engine.console import Console
from pants.engine.fs import (
    CreateDigest,
    Digest,
    DigestContents,
    FileContent,
    PathGlobs,
    Paths,
    Workspace,
)
from pants.engine.goal import Goal, GoalSubsystem
from pants.engine.internals.selectors import Get, MultiGet
from pants.engine.rules import collect_rules, goal_rule, rule
from pants.engine.target import (
    Sources,
    SourcesPaths,
    SourcesPathsRequest,
    Target,
    UnexpandedTargets,
)
from pants.engine.unions import UnionMembership, union
from pants.util.docutil import doc_url
from pants.util.frozendict import FrozenDict
from pants.util.logging import LogLevel
from pants.util.memo import memoized
from pants.util.meta import frozen_after_init


@union
@dataclass(frozen=True)
class PutativeTargetsRequest(metaclass=ABCMeta):
    search_paths: PutativeTargetsSearchPaths


@dataclass(frozen=True)
class PutativeTargetsSearchPaths:
    dirs: tuple[str, ...]

    def path_globs(self, filename_glob: str) -> PathGlobs:
        return PathGlobs([os.path.join(d, "**", filename_glob) for d in self.dirs])


@memoized
def default_sources_for_target_type(tgt_type: Type[Target]) -> Tuple[str, ...]:
    for field in tgt_type.core_fields:
        if issubclass(field, Sources):
            return field.default or tuple()
    return tuple()


@frozen_after_init
@dataclass(order=True, unsafe_hash=True)
class PutativeTarget:
    """A potential target to add, detected by various heuristics.

    This class uses the term "target" in the loose sense. It can also represent an invocation of a
    target-generating macro.
    """

    # Note that field order is such that the dataclass order will be by address (path+name).
    path: str
    name: str
    type_alias: str

    # The sources that triggered creating of this putative target.
    # The putative target will own these sources, but may also glob over other sources.
    # If the putative target does not have a `sources` field, then this value must be the
    # empty tuple.
    triggering_sources: Tuple[str, ...]

    # The globs of sources owned by this target.
    # If kwargs contains an explicit sources key, it should be identical to this value.
    # Otherwise, this field should contain the default globs that the target type will apply.
    # If the putative target does not have a `sources` field, then this value must be the
    # empty tuple.
    # TODO: If target_type is a regular target (and not a macro) we can derive the default
    #  source globs for that type from BuildConfiguration.  However that is fiddly and not
    #  a high priority.
    owned_sources: Tuple[str, ...]

    # Whether the pututative target has an address (or, e.g., is a macro with no address).
    addressable: bool

    # Note that we generate the BUILD file target entry exclusively from these kwargs (plus the
    # type_alias), not from the fields above, which are broken out for other uses.
    # This allows the creator of instances of this class to control whether the generated
    # target should assume default kwarg values or provide them explicitly.
    kwargs: FrozenDict[str, str | int | bool | Tuple[str, ...]]

    # Any comment lines to add above the BUILD file stanza we generate for this putative target.
    # Should include the `#` prefix, which will not be added.
    comments: Tuple[str, ...]

    @classmethod
    def for_target_type(
        cls,
        target_type: Type[Target],
        path: str,
        name: str,
        triggering_sources: Iterable[str],
        kwargs: Mapping[str, str | int | bool | Tuple[str, ...]] | None = None,
        comments: Iterable[str] = tuple(),
    ):
        explicit_sources = (kwargs or {}).get("sources")
        if explicit_sources is not None and not isinstance(explicit_sources, tuple):
            raise TypeError(
                "Explicit sources passed to PutativeTarget.for_target_type must be a Tuple[str]."
            )

        default_sources = default_sources_for_target_type(target_type)
        if (explicit_sources or triggering_sources) and not default_sources:
            raise ValueError(
                f"A target of type {target_type.__name__} was proposed at "
                f"address {path}:{name} with explicit sources {', '.join(explicit_sources or triggering_sources)}, "
                "but this target type does not have a `sources` field."
            )
        owned_sources = explicit_sources or default_sources or tuple()
        return cls(
            path,
            name,
            target_type.alias,
            triggering_sources,
            owned_sources,
            addressable=True,  # "Real" targets are always addressable.
            kwargs=kwargs,
            comments=comments,
        )

    def __init__(
        self,
        path: str,
        name: str,
        type_alias: str,
        triggering_sources: Iterable[str],
        owned_sources: Iterable[str],
        *,
        addressable: bool = True,
        kwargs: Mapping[str, str | int | bool | Tuple[str, ...]] | None = None,
        comments: Iterable[str] = tuple(),
    ) -> None:
        self.path = path
        self.name = name
        self.type_alias = type_alias
        self.triggering_sources = tuple(triggering_sources)
        self.owned_sources = tuple(owned_sources)
        self.addressable = addressable
        self.kwargs = FrozenDict(kwargs or {})
        self.comments = tuple(comments)

    @property
    def address(self) -> Address:
        if not self.addressable:
            raise ValueError(
                f"Cannot compute address for non-addressable putative target of type "
                f"{self.type_alias} at path {self.path}"
            )
        return Address(self.path, target_name=self.name)

    def realias(self, new_alias: str | None) -> PutativeTarget:
        """A copy of this object with the alias replaced to the given alias.

        Returns this object if the alias is None or is identical to this objects existing alias.
        """
        return (
            self
            if (new_alias is None or new_alias == self.type_alias)
            else dataclasses.replace(self, type_alias=new_alias)
        )

    def rename(self, new_name: str) -> PutativeTarget:
        """A copy of this object with the name replaced to the given name."""
        # We assume that a rename imposes an explicit "name=" kwarg, overriding any previous
        # explicit "name=" kwarg, even if the rename happens to be to the default name.
        return dataclasses.replace(self, name=new_name, kwargs={**self.kwargs, "name": new_name})

    def restrict_sources(self) -> PutativeTarget:
        """A copy of this object with the sources explicitly set to just the triggering sources."""
        owned_sources = self.triggering_sources
        return dataclasses.replace(
            self,
            owned_sources=owned_sources,
            kwargs={**self.kwargs, "sources": owned_sources},
        )

    def add_comments(self, comments: Iterable[str]) -> PutativeTarget:
        return dataclasses.replace(self, comments=self.comments + tuple(comments))

    def generate_build_file_stanza(self, indent: str) -> str:
        def fmt_val(v) -> str:
            if isinstance(v, str):
                return f'"{v}"'
            if isinstance(v, tuple):
                val_parts = [f"\n{indent*2}{fmt_val(x)}" for x in v]
                val_str = ",".join(val_parts) + ("," if v else "")
                return f"[{val_str}\n{indent}]"
            return repr(v)

        if self.kwargs:
            kwargs_str_parts = [f"\n{indent}{k}={fmt_val(v)}" for k, v in self.kwargs.items()]
            kwargs_str = ",".join(kwargs_str_parts) + ",\n"
        else:
            kwargs_str = ""

        comment_str = ("\n".join(self.comments) + "\n") if self.comments else ""
        return f"{comment_str}{self.type_alias}({kwargs_str})\n"


class PutativeTargets(DeduplicatedCollection[PutativeTarget]):
    sort_input = True

    @classmethod
    def merge(cls, tgts_iters: Iterable[PutativeTargets]) -> PutativeTargets:
        all_tgts: List[PutativeTarget] = []
        for tgts in tgts_iters:
            all_tgts.extend(tgts)
        return cls(all_tgts)


class TailorSubsystem(GoalSubsystem):
    name = "tailor"
    help = "Auto-generate BUILD file targets for new source files."

    required_union_implementations = (PutativeTargetsRequest,)

    @classmethod
    def register_options(cls, register):
        super().register_options(register)
        register(
            "--build-file-name",
            advanced=True,
            type=str,
            default="BUILD",
            help="The name to use for generated BUILD files.",
        )

        register(
            "--build-file-header",
            advanced=True,
            type=str,
            default="",
            help="A header, e.g., a copyright notice, to add to the content of created BUILD files.",
        )

        register(
            "--build-file-indent",
            advanced=True,
            type=str,
            default="    ",
            help="The indent to use when auto-editing BUILD files.",
        )

        register(
            "--alias-mapping",
            advanced=True,
            type=dict,
            help="A mapping from standard target type to custom type to use instead. The custom "
            "type can be a custom target type or a macro that offers compatible functionality "
            f"to the one it replaces (see {doc_url('macros')}).",
        )

    @property
    def build_file_name(self) -> str:
        return cast(str, self.options.build_file_name)

    @property
    def build_file_header(self) -> str:
        return cast(str, self.options.build_file_header)

    @property
    def build_file_indent(self) -> str:
        return cast(str, self.options.build_file_indent)

    def alias_for(self, standard_type: str) -> str | None:
        # The get() could return None, but casting to str | None errors.
        # This cast suffices to avoid typecheck errors.
        return cast(str, self.options.alias_mapping.get(standard_type))


class Tailor(Goal):
    subsystem_cls = TailorSubsystem


def group_by_dir(paths: Iterable[str]) -> dict[str, set[str]]:
    """For a list of file paths, returns a dict of directory path -> files in that dir."""
    ret = defaultdict(set)
    for path in paths:
        dirname, filename = os.path.split(path)
        ret[dirname].add(filename)
    return ret


def group_by_build_file(
    build_file_name: str, ptgts: Iterable[PutativeTarget]
) -> Dict[str, List[PutativeTarget]]:
    ret = defaultdict(list)
    for ptgt in ptgts:
        ret[os.path.join(ptgt.path, build_file_name)].append(ptgt)
    return ret


class AllOwnedSources(DeduplicatedCollection[str]):
    """All files in the project already owned by targets."""


@rule(desc="Determine all files already owned by targets", level=LogLevel.DEBUG)
async def determine_all_owned_sources() -> AllOwnedSources:
    all_tgts = await Get(UnexpandedTargets, AddressSpecs([MaybeEmptyDescendantAddresses("")]))
    all_sources_paths = await MultiGet(
        Get(SourcesPaths, SourcesPathsRequest(tgt.get(Sources))) for tgt in all_tgts
    )
    return AllOwnedSources(
        itertools.chain.from_iterable(sources_paths.files for sources_paths in all_sources_paths)
    )


@dataclass(frozen=True)
class UniquelyNamedPutativeTargets:
    """Putative targets that have no name conflicts with existing targets (or each other)."""

    putative_targets: PutativeTargets


@rule
async def rename_conflicting_targets(ptgts: PutativeTargets) -> UniquelyNamedPutativeTargets:
    """Ensure that no target addresses collide."""
    all_existing_tgts = await Get(
        UnexpandedTargets, AddressSpecs([MaybeEmptyDescendantAddresses("")])
    )
    existing_addrs: Set[str] = {tgt.address.spec for tgt in all_existing_tgts}
    uniquely_named_putative_targets: List[PutativeTarget] = []
    for ptgt in ptgts:
        if not ptgt.addressable:
            # Non-addressable PutativeTargets never have collision issues.
            uniquely_named_putative_targets.append(ptgt)
            continue

        idx = 0
        possibly_renamed_ptgt = ptgt
        # Targets in root-level BUILD files must be named explicitly.
        if possibly_renamed_ptgt.path == "" and possibly_renamed_ptgt.kwargs.get("name") is None:
            possibly_renamed_ptgt = possibly_renamed_ptgt.rename("root")
        # Eliminate any address collisions.
        while possibly_renamed_ptgt.address.spec in existing_addrs:
            possibly_renamed_ptgt = ptgt.rename(f"{ptgt.name}{idx}")
            idx += 1
        uniquely_named_putative_targets.append(possibly_renamed_ptgt)
        existing_addrs.add(possibly_renamed_ptgt.address.spec)

    return UniquelyNamedPutativeTargets(PutativeTargets(uniquely_named_putative_targets))


@dataclass(frozen=True)
class DisjointSourcePutativeTarget:
    """Putative target whose sources don't overlap with those of any existing targets."""

    putative_target: PutativeTarget


@rule
async def restrict_conflicting_sources(ptgt: PutativeTarget) -> DisjointSourcePutativeTarget:
    source_paths = await Get(
        Paths,
        PathGlobs(Sources.prefix_glob_with_dirpath(ptgt.path, glob) for glob in ptgt.owned_sources),
    )
    source_path_set = set(source_paths.files)
    source_dirs = {os.path.dirname(path) for path in source_path_set}
    possible_owners = await Get(
        UnexpandedTargets, AddressSpecs(AscendantAddresses(d) for d in source_dirs)
    )
    possible_owners_sources = await MultiGet(
        Get(SourcesPaths, SourcesPathsRequest(t.get(Sources))) for t in possible_owners
    )
    conflicting_targets = []
    for tgt, sources in zip(possible_owners, possible_owners_sources):
        if source_path_set.intersection(sources.files):
            conflicting_targets.append(tgt)

    if conflicting_targets:
        conflicting_addrs = sorted(tgt.address.spec for tgt in conflicting_targets)
        explicit_srcs_str = ", ".join(ptgt.kwargs.get("sources") or [])  # type: ignore[arg-type]
        orig_sources_str = (
            f"[{explicit_srcs_str}]" if explicit_srcs_str else f"the default for {ptgt.type_alias}"
        )
        ptgt = ptgt.restrict_sources().add_comments(
            [f"# NOTE: Sources restricted from {orig_sources_str} due to conflict with"]
            + [f"#   - {caddr}" for caddr in conflicting_addrs]
        )
    return DisjointSourcePutativeTarget(ptgt)


@dataclass(frozen=True)
class EditBuildFilesRequest:
    putative_targets: PutativeTargets
    name: str
    header: str
    indent: str


@dataclass(frozen=True)
class EditedBuildFiles:
    digest: Digest
    created_paths: Tuple[str, ...]
    updated_paths: Tuple[str, ...]


def make_content_str(
    existing_content: str | None, indent: str, pts: Iterable[PutativeTarget]
) -> str:
    new_content = ([] if existing_content is None else [existing_content]) + [
        pt.generate_build_file_stanza(indent) for pt in pts
    ]
    new_content = [s.rstrip() for s in new_content]
    return "\n\n".join(new_content) + "\n"


@rule(desc="Edit BUILD files with new targets", level=LogLevel.DEBUG)
async def edit_build_files(req: EditBuildFilesRequest) -> EditedBuildFiles:
    ptgts_by_build_file = group_by_build_file(req.name, req.putative_targets)
    # There may be an existing *directory* whose name collides with that of a BUILD file
    # we want to create. This is more likely on a system with case-insensitive paths,
    # such as MacOS. We detect such cases and use an alt BUILD file name to fix.
    existing_paths = await Get(Paths, PathGlobs(ptgts_by_build_file.keys()))
    existing_dirs = set(existing_paths.dirs)
    # Technically there could be a dir named "BUILD.pants" as well, but that's pretty unlikely.
    ptgts_by_build_file = {
        (f"{bf}.pants" if bf in existing_dirs else bf): pts
        for bf, pts in ptgts_by_build_file.items()
    }
    existing_build_files_contents = await Get(DigestContents, PathGlobs(ptgts_by_build_file.keys()))
    existing_build_files_contents_by_path = {
        ebfc.path: ebfc.content for ebfc in existing_build_files_contents
    }

    def make_content(bf_path: str, pts: Iterable[PutativeTarget]) -> FileContent:
        existing_content_bytes = existing_build_files_contents_by_path.get(bf_path)
        existing_content = (
            req.header if existing_content_bytes is None else existing_content_bytes.decode()
        )
        new_content_bytes = make_content_str(existing_content, req.indent, pts).encode()
        return FileContent(bf_path, new_content_bytes)

    new_digest = await Get(
        Digest,
        CreateDigest([make_content(path, ptgts) for path, ptgts in ptgts_by_build_file.items()]),
    )

    updated = set(existing_build_files_contents_by_path.keys())
    created = set(ptgts_by_build_file.keys()) - updated
    return EditedBuildFiles(new_digest, tuple(sorted(created)), tuple(sorted(updated)))


def specs_to_dirs(specs: Specs) -> tuple[str, ...]:
    """Extract cmd-line specs that look like directories.

    Error on all other specs.

    This is a hack that allows us to emulate "directory specs", until we are able to
    support those more intrinsically.

    TODO: If other goals need "directory specs", move this logic to a rule that produces them.
    """
    # Specs that look like directories are interpreted by the SpecsParser as the address
    # of the target with the same name as the directory of the BUILD file.
    # Note that we can't tell the difference between the user specifying foo/bar and the
    # user specifying foo/bar:bar, so we will consider the latter a "directory spec" too.
    dir_specs = []
    other_specs: list[Spec] = [
        *specs.filesystem_specs.includes,
        *specs.filesystem_specs.ignores,
        *specs.address_specs.globs,
    ]
    for spec in specs.address_specs.literals:
        if spec.is_directory_shorthand:
            dir_specs.append(spec)
        else:
            other_specs.append(spec)
    if other_specs:
        raise ValueError(
            "The tailor goal only accepts literal directories as arguments, but you "
            f"specified: {', '.join(str(spec) for spec in other_specs)}.  You can also "
            "specify no arguments to run against the entire repository."
        )
    # No specs at all means search the entire repo (represented by ("",)).
    return tuple(spec.path_component for spec in specs.address_specs.literals) or ("",)


@goal_rule
async def tailor(
    tailor_subsystem: TailorSubsystem,
    console: Console,
    workspace: Workspace,
    union_membership: UnionMembership,
    specs: Specs,
) -> Tailor:
    search_paths = PutativeTargetsSearchPaths(specs_to_dirs(specs))
    putative_target_request_types: Iterable[type[PutativeTargetsRequest]] = union_membership[
        PutativeTargetsRequest
    ]
    putative_targets_results = await MultiGet(
        Get(PutativeTargets, PutativeTargetsRequest, req_type(search_paths))
        for req_type in putative_target_request_types
    )
    putative_targets = PutativeTargets.merge(putative_targets_results)
    putative_targets = PutativeTargets(
        pt.realias(tailor_subsystem.alias_for(pt.type_alias)) for pt in putative_targets
    )
    fixed_names_ptgts = await Get(UniquelyNamedPutativeTargets, PutativeTargets, putative_targets)
    fixed_sources_ptgts = await MultiGet(
        Get(DisjointSourcePutativeTarget, PutativeTarget, ptgt)
        for ptgt in fixed_names_ptgts.putative_targets
    )
    ptgts = [dspt.putative_target for dspt in fixed_sources_ptgts]

    if ptgts:
        edited_build_files = await Get(
            EditedBuildFiles,
            EditBuildFilesRequest(
                PutativeTargets(ptgts),
                tailor_subsystem.build_file_name,
                tailor_subsystem.build_file_header,
                tailor_subsystem.build_file_indent,
            ),
        )
        updated_build_files = set(edited_build_files.updated_paths)
        workspace.write_digest(edited_build_files.digest)
        ptgts_by_build_file = group_by_build_file(tailor_subsystem.build_file_name, ptgts)
        for build_file_path, ptgts in ptgts_by_build_file.items():
            verb = "Updated" if build_file_path in updated_build_files else "Created"
            console.print_stdout(f"{verb} {console.blue(build_file_path)}:")
            for ptgt in ptgts:
                console.print_stdout(
                    f"  - Added {console.green(ptgt.type_alias)} target "
                    f"{console.cyan(ptgt.name)}"
                )
    return Tailor(0)


def rules():
    return collect_rules()
