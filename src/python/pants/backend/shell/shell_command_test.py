# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import logging
from textwrap import dedent

import pytest

from pants.backend.shell.shell_command import GenerateFilesFromShellCommandRequest
from pants.backend.shell.shell_command import rules as shell_command_rules
from pants.backend.shell.target_types import ShellCommand, ShellLibrary
from pants.core.target_types import Files
from pants.core.target_types import rules as target_type_rules
from pants.core.util_rules.archive import rules as archive_rules
from pants.core.util_rules.source_files import SourceFiles, SourceFilesRequest
from pants.core.util_rules.source_files import rules as source_files_rules
from pants.engine.addresses import Address
from pants.engine.fs import EMPTY_SNAPSHOT, DigestContents
from pants.engine.target import GeneratedSources, TransitiveTargets, TransitiveTargetsRequest
from pants.testutil.rule_runner import QueryRule, RuleRunner


@pytest.fixture
def rule_runner() -> RuleRunner:
    rule_runner = RuleRunner(
        rules=[
            *archive_rules(),
            *shell_command_rules(),
            *source_files_rules(),
            *target_type_rules(),
            QueryRule(GeneratedSources, [GenerateFilesFromShellCommandRequest]),
            QueryRule(TransitiveTargets, [TransitiveTargetsRequest]),
            QueryRule(SourceFiles, [SourceFilesRequest]),
        ],
        target_types=[ShellCommand, ShellLibrary, Files],
    )
    rule_runner.set_options([], env_inherit={"PATH"})
    return rule_runner


def assert_shell_command_result(
    rule_runner: RuleRunner, address: Address, expected_contents: dict[str, str]
) -> None:
    target = rule_runner.get_target(address)
    result = rule_runner.request(
        GeneratedSources, [GenerateFilesFromShellCommandRequest(EMPTY_SNAPSHOT, target)]
    )
    assert result.snapshot.files == tuple(expected_contents)
    contents = rule_runner.request(DigestContents, [result.snapshot.digest])
    for fc in contents:
        assert fc.content == expected_contents[fc.path].encode()


def assert_logged(caplog, expect_logged=None):
    if expect_logged:
        assert len(caplog.records) == len(expect_logged)
        for idx, (lvl, msg) in enumerate(expect_logged):
            log_record = caplog.records[idx]
            assert msg in log_record.message
            assert lvl == log_record.levelno
    else:
        assert not caplog.records


def test_sources_and_files(rule_runner: RuleRunner) -> None:
    MSG = ["Hello shell_command", ", nice cut."]
    rule_runner.write_files(
        {
            "src/BUILD": dedent(
                """\
                experimental_shell_command(
                  name="hello",
                  dependencies=[":build-utils", ":files"],
                  tools=[
                    "bash",
                    "cat",
                    "env",
                    "mkdir",
                    "tee",
                  ],
                  outputs=["message.txt", "res/"],
                  command="./script.sh",
                )

                files(
                  name="files",
                  sources=["*.txt"],
                )

                shell_library(name="build-utils")
                """
            ),
            "src/intro.txt": MSG[0],
            "src/outro.txt": MSG[1],
            "src/script.sh": (
                "#!/usr/bin/env bash\n"
                "mkdir res && cat *.txt > message.txt && cat message.txt | tee res/log.txt"
            ),
        }
    )

    # Set script.sh mode to rwxr-xr-x.
    rule_runner.chmod("src/script.sh", 0o755)

    RES = "".join(MSG)
    assert_shell_command_result(
        rule_runner,
        Address("src", target_name="hello"),
        expected_contents={
            "src/message.txt": RES,
            "src/res/log.txt": RES,
        },
    )


def test_quotes_command(rule_runner: RuleRunner) -> None:
    rule_runner.write_files(
        {
            "src/BUILD": dedent(
                """\
                experimental_shell_command(
                  name="quotes",
                  tools=["echo", "tee"],
                  command='echo "foo bar" | tee out.log',
                  outputs=["out.log"],
                )
                """
            ),
        }
    )

    assert_shell_command_result(
        rule_runner,
        Address("src", target_name="quotes"),
        expected_contents={"src/out.log": "foo bar\n"},
    )


def test_chained_shell_commands(rule_runner: RuleRunner) -> None:
    rule_runner.write_files(
        {
            "src/a/BUILD": dedent(
                """\
                experimental_shell_command(
                  name="msg",
                  tools=["echo"],
                  outputs=["msg"],
                  command="echo 'shell_command:a' > msg",
                )
                """
            ),
            "src/b/BUILD": dedent(
                """\
                experimental_shell_command(
                  name="msg",
                  tools=["cp", "echo"],
                  outputs=["msg"],
                  command="cp ../a/msg . ; echo 'shell_command:b' >> msg",
                  dependencies=["src/a:msg"],
                )
                """
            ),
        }
    )

    assert_shell_command_result(
        rule_runner,
        Address("src/a", target_name="msg"),
        expected_contents={"src/a/msg": "shell_command:a\n"},
    )

    assert_shell_command_result(
        rule_runner,
        Address("src/b", target_name="msg"),
        expected_contents={"src/b/msg": "shell_command:a\nshell_command:b\n"},
    )


def test_side_effecting_command(caplog, rule_runner: RuleRunner) -> None:
    caplog.set_level(logging.INFO)
    caplog.clear()

    rule_runner.write_files(
        {
            "src/BUILD": dedent(
                """\
                experimental_shell_command(
                  name="side-effect",
                  command="echo 'server started' && echo 'warn msg' >&2",
                  tools=["echo"],
                  log_output=True,
                )
                """
            ),
        }
    )

    assert_shell_command_result(
        rule_runner,
        Address("src", target_name="side-effect"),
        expected_contents={},
    )

    assert_logged(
        caplog,
        [
            (logging.INFO, "server started\n"),
            (logging.WARNING, "warn msg\n"),
        ],
    )


def test_tool_search_path_stable(rule_runner: RuleRunner) -> None:
    rule_runner.write_files(
        {
            "src/BUILD": dedent(
                """\
                experimental_shell_command(
                  name="paths",
                  command="mkdir subdir; cd subdir; ls .",
                  tools=["cd", "ls", "mkdir"],
                )
                """
            ),
        }
    )

    assert_shell_command_result(
        rule_runner,
        Address("src", target_name="paths"),
        expected_contents={},
    )
