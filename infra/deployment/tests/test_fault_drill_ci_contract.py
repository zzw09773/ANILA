from __future__ import annotations

import ast
import copy
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "gate1-ci.yml"
REDIS_TEST_PATH = ROOT / "infra" / "deployment" / "tests" / "test_redis_durability.py"
LEASE_STEP_NAME = "Prove Gate 3 worker lease, DLQ, and similarity concurrency"
LEASE_TEST_PATH = "services/ingestion-worker/tests/test_job_state_pg.py"
LEASE_JUNIT_PATH = "/tmp/gate3-worker-lease.xml"
SIMILARITY_TEST_PATH = "services/ingestion-worker/tests/test_similarity_relations_pg.py"
SIMILARITY_JUNIT_PATH = "/tmp/gate3-similarity-relations.xml"
SUITES = (
    ("lease", LEASE_TEST_PATH, LEASE_JUNIT_PATH),
    ("similarity", SIMILARITY_TEST_PATH, SIMILARITY_JUNIT_PATH),
)
EXPECTED_CASE_COUNTS = {
    LEASE_TEST_PATH: 7,
    SIMILARITY_TEST_PATH: 4,
}


def _source_case_identities(test_path: str) -> tuple[tuple[str, str], ...]:
    """Derive the JUnit identities from the exact module-level PostgreSQL tests."""

    source_path = ROOT / test_path
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    classname = f"tests.{source_path.stem}"
    return tuple(
        (classname, node.name)
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    )


EXPECTED_CASE_IDENTITIES = {
    test_path: _source_case_identities(test_path)
    for _suite_name, test_path, _report_path in SUITES
}

_SHELL_PUNCTUATION = frozenset(";&|()<>")
_UNSUPPORTED_SHELL_KEYWORDS = frozenset(
    {
        "if",
        "then",
        "elif",
        "else",
        "fi",
        "while",
        "until",
        "for",
        "do",
        "done",
        "case",
        "esac",
        "!",
    }
)
_FORBIDDEN_ENVIRONMENT_KEYS = frozenset(
    {
        "BASHOPTS",
        "BASH_ENV",
        "CDPATH",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "ENV",
        "GLOBIGNORE",
        "IFS",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "PATH",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONOPTIMIZE",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "SHELLOPTS",
    }
)
_PERSISTENCE_TOKEN_RE = re.compile(r"GITHUB_(?:ENV|PATH)")


def _is_literal_true(value: object) -> bool:
    if value is True:
        return True
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    return candidate == "true" or bool(re.fullmatch(r"\$\{\{\s*true\s*\}\}", candidate))


def _lease_job_and_step(workflow: dict) -> tuple[dict, dict]:
    matches = [
        (job, step)
        for job in workflow.get("jobs", {}).values()
        for step in job.get("steps", [])
        if step.get("name") == LEASE_STEP_NAME
    ]
    if len(matches) != 1:
        raise AssertionError(
            "workflow must contain exactly one PostgreSQL lease proof step"
        )
    job, step = matches[0]
    for scope, container in (("job", job), ("step", step)):
        if "if" in container and not _is_literal_true(container["if"]):
            raise AssertionError(
                f"lease proof {scope} must run unconditionally or use literal true"
            )
    for scope, container in (("workflow", workflow), ("job", job), ("step", step)):
        environment = container.get("env")
        if environment is None:
            environment = {}
        elif not isinstance(environment, dict):
            raise AssertionError(f"lease proof {scope} env must be a mapping")
        if any(
            isinstance(key, str) and key.startswith("PYTEST_") for key in environment
        ):
            raise AssertionError(
                f"lease proof {scope} must not define PYTEST_* environment variables"
            )
        if any(
            isinstance(key, str) and key.upper() in _FORBIDDEN_ENVIRONMENT_KEYS
            for key in environment
        ):
            raise AssertionError(
                f"lease proof {scope} must not define shell or interpreter influence environment variables"
            )
    for scope, container in (("workflow", workflow), ("job", job)):
        defaults = container.get("defaults")
        if defaults is None:
            defaults = {}
        elif not isinstance(defaults, dict):
            raise AssertionError(f"lease proof {scope} defaults must be a mapping")
        run_defaults = defaults.get("run")
        if run_defaults is None:
            run_defaults = {}
        elif not isinstance(run_defaults, dict):
            raise AssertionError(f"lease proof {scope} run defaults must be a mapping")
        if "shell" in run_defaults:
            raise AssertionError(
                f"lease proof {scope} must not override the Actions run shell"
            )
        if "working-directory" in run_defaults:
            raise AssertionError(
                f"lease proof {scope} must not override the Actions working directory"
            )
    if "shell" in step:
        raise AssertionError("lease proof step must not override the Actions run shell")
    if "working-directory" in step:
        raise AssertionError(
            "lease proof step must not override the Actions working directory"
        )
    if "continue-on-error" in job or "continue-on-error" in step:
        raise AssertionError("lease proof job and step must fail closed")
    if "ANILA_JOB_STATE_PG_URL" not in (step.get("env") or {}):
        raise AssertionError("lease proof step must set its PostgreSQL DSN")
    for candidate in job.get("steps", []):
        candidate_script = candidate.get("run")
        if isinstance(candidate_script, str) and _PERSISTENCE_TOKEN_RE.search(
            candidate_script
        ):
            raise AssertionError(
                "postgres-rls run steps must not reference GITHUB_ENV or GITHUB_PATH"
            )
    script = step.get("run")
    if not isinstance(script, str):
        raise AssertionError("lease proof step must have a shell script")
    return job, step


def _logical_shell_commands(script: str) -> list[list[str]]:
    """Tokenize the step's simple commands and fail closed on shell control flow."""

    commands: list[list[str]] = []
    continued: list[str] = []
    heredoc_delimiter: str | None = None
    for raw_line in script.splitlines():
        stripped = raw_line.strip()
        if heredoc_delimiter is not None:
            if stripped == heredoc_delimiter:
                heredoc_delimiter = None
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            continued.append(stripped[:-1].rstrip())
            continue
        logical = " ".join((*continued, stripped))
        continued.clear()
        try:
            lexer = shlex.shlex(
                logical,
                posix=True,
                punctuation_chars=";&|()<>",
            )
            lexer.whitespace_split = True
            lexer.commenters = "#"
            tokens = list(lexer)
        except ValueError as exc:
            raise AssertionError(
                "lease proof step contains invalid shell quoting"
            ) from exc
        if not tokens:
            continue
        heredoc_positions = [
            index for index, token in enumerate(tokens) if token == "<<"
        ]
        if heredoc_positions:
            if (
                len(heredoc_positions) != 1
                or heredoc_positions[0] != 2
                or tokens[:2] != ["python", "-"]
                or len(tokens) != 4
            ):
                raise AssertionError(
                    "lease proof step contains unsupported shell control"
                )
            heredoc_delimiter = tokens[3]
            if not heredoc_delimiter:
                raise AssertionError("lease proof step has an empty heredoc delimiter")
            commands.append(tokens)
            continue

        if tokens[:3] == ["python", "-m", "pytest"] and (
            "$" in logical or "`" in logical
        ):
            raise AssertionError(
                "lease proof pytest command must not contain shell expansion"
            )
        if "$(" in logical or "`" in logical:
            raise AssertionError("lease proof step contains unsupported shell control")

        if any(
            token and all(character in _SHELL_PUNCTUATION for character in token)
            for token in tokens
        ):
            raise AssertionError("lease proof step contains unsupported shell control")
        if tokens[0] in _UNSUPPORTED_SHELL_KEYWORDS or tokens[:2] == ["set", "+e"]:
            raise AssertionError("lease proof step contains unsupported shell control")
        if tokens[:3] != ["python", "-m", "pytest"]:
            raise AssertionError("lease proof step contains unsupported shell command")
        commands.append(tokens)

    if continued:
        raise AssertionError("lease proof step has an unterminated line continuation")
    if heredoc_delimiter is not None:
        raise AssertionError("lease proof step has an unterminated heredoc")
    return commands


def _require_bound_pytest_invocation(
    script: str,
    *,
    test_path: str,
    report_path: str,
) -> None:
    commands = _logical_shell_commands(script)
    report_token = f"--junitxml={report_path}"
    pytest_commands = [
        tokens for tokens in commands if tokens[:3] == ["python", "-m", "pytest"]
    ]
    matching = [
        tokens
        for tokens in pytest_commands
        if test_path in tokens and report_token in tokens
    ]
    if len(matching) != 1:
        raise AssertionError(
            f"{test_path} and {report_path} must be bound to one python -m pytest command"
        )
    command = matching[0]
    expected_command = [
        "python",
        "-m",
        "pytest",
        test_path,
        "-q",
        report_token,
    ]
    if command != expected_command:
        raise AssertionError(
            f"pytest command for {test_path} must use exact allowed argv"
        )
    allowed_commands = [
        ["python", "-m", "pytest", path, "-q", f"--junitxml={report}"]
        for _name, path, report in SUITES
    ]
    if sorted(pytest_commands) != sorted(allowed_commands):
        raise AssertionError(
            "lease proof pytest commands must match exact allowed argv"
        )


def _extract_junit_guard(
    workflow: dict,
    *,
    test_path: str,
    report_path: str,
) -> str:
    _job, step = _lease_job_and_step(workflow)
    script = step.get("run")
    if script.count(test_path) != 1:
        raise AssertionError(f"{test_path} must appear exactly once")
    report_token = f"--junitxml={report_path}"
    if script.count(report_token) != 1:
        raise AssertionError(f"{report_path} must be one unique JUnit report")
    _require_bound_pytest_invocation(
        script,
        test_path=test_path,
        report_path=report_path,
    )
    match_start = script.index(report_token)
    if script.index(test_path) > match_start:
        raise AssertionError(f"{test_path} must run before its JUnit guard")
    heredoc = "python - <<'PY'\n"
    guard_start = script.find(heredoc, match_start + len(report_token))
    if guard_start < 0:
        raise AssertionError("JUnit guard Python heredoc is missing")
    guard_start += len(heredoc)
    guard_end = script.find("\nPY", guard_start)
    if guard_end < 0:
        raise AssertionError("JUnit guard Python heredoc is unterminated")
    source = script[guard_start:guard_end]
    tree = ast.parse(source)

    if any(isinstance(node, ast.Assert) for node in ast.walk(tree)):
        raise AssertionError("JUnit guard must not use optimization-sensitive assert")
    assignments = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value
    report = assignments.get("report")
    if not (
        isinstance(report, ast.Call)
        and isinstance(report.func, ast.Name)
        and report.func.id == "Path"
        and len(report.args) == 1
        and isinstance(report.args[0], ast.Constant)
        and report.args[0].value == report_path
    ):
        raise AssertionError("JUnit guard must bind report to the emitted report path")
    root = assignments.get("root")
    if not (
        isinstance(root, ast.Call)
        and isinstance(root.func, ast.Attribute)
        and root.func.attr == "getroot"
        and not root.args
        and isinstance(root.func.value, ast.Call)
    ):
        raise AssertionError("JUnit guard must get the parsed report root")
    parse_call = root.func.value
    if not (
        isinstance(parse_call.func, ast.Attribute)
        and isinstance(parse_call.func.value, ast.Name)
        and parse_call.func.value.id == "ET"
        and parse_call.func.attr == "parse"
        and len(parse_call.args) == 1
        and isinstance(parse_call.args[0], ast.Name)
        and parse_call.args[0].id == "report"
    ):
        raise AssertionError("JUnit guard must parse the bound report path")
    cases = assignments.get("cases")
    if not (
        isinstance(cases, ast.Call)
        and isinstance(cases.func, ast.Attribute)
        and isinstance(cases.func.value, ast.Name)
        and cases.func.value.id == "root"
        and cases.func.attr == "findall"
        and len(cases.args) == 1
        and isinstance(cases.args[0], ast.Constant)
        and cases.args[0].value == ".//testcase"
    ):
        raise AssertionError("JUnit guard must enumerate testcase nodes")
    expected_cases = assignments.get("expected_cases")
    try:
        pinned_identities = ast.literal_eval(expected_cases)
    except (TypeError, ValueError):
        pinned_identities = None
    source_identities = set(EXPECTED_CASE_IDENTITIES[test_path])
    if pinned_identities != source_identities:
        raise AssertionError(
            f"JUnit guard identities for {test_path} must exactly match test source"
        )
    if len(source_identities) != EXPECTED_CASE_COUNTS[test_path]:
        raise AssertionError(
            f"{test_path} source must contain exactly "
            f"{EXPECTED_CASE_COUNTS[test_path]} PostgreSQL tests"
        )
    case_ids = assignments.get("case_ids")
    case_id_fields: set[str] = set()
    if isinstance(case_ids, ast.ListComp):
        case_id_fields = {
            node.args[0].value
            for node in ast.walk(case_ids)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in {"classname", "name"}
        }
    if not (
        isinstance(case_ids, ast.ListComp) and case_id_fields == {"classname", "name"}
    ):
        raise AssertionError("JUnit guard must derive classname and name identities")
    expected_count = EXPECTED_CASE_COUNTS[test_path]
    exact_count_guards = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Call)
        and isinstance(node.test.left.func, ast.Name)
        and node.test.left.func.id == "len"
        and len(node.test.left.args) == 1
        and isinstance(node.test.left.args[0], ast.Name)
        and node.test.left.args[0].id == "cases"
        and len(node.test.ops) == 1
        and isinstance(node.test.ops[0], ast.NotEq)
        and len(node.test.comparators) == 1
        and isinstance(node.test.comparators[0], ast.Constant)
        and node.test.comparators[0].value == expected_count
    ]
    if len(exact_count_guards) != 1:
        raise AssertionError(
            f"JUnit guard must require exactly {expected_count} testcase nodes"
        )
    for result_name, child_name in (
        ("skipped", "skipped"),
        ("failures", "failure"),
        ("errors", "error"),
    ):
        result = assignments.get(result_name)
        if not isinstance(result, ast.ListComp) or not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "find"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == child_name
            for node in ast.walk(result)
        ):
            raise AssertionError(
                f"JUnit guard must derive {result_name} cases from the report"
            )
    exits = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == "SystemExit"
    ]
    if len(exits) < 7:
        raise AssertionError(
            "JUnit guard must explicitly exit nonzero for every bad state"
        )
    return source


def _run_guard(
    source: str,
    report_path: Path,
    *,
    canonical_path: str,
) -> subprocess.CompletedProcess[str]:
    rewritten = source.replace(f'"{canonical_path}"', repr(str(report_path)))
    if rewritten == source:
        raise AssertionError("test harness could not redirect the validated JUnit path")
    return subprocess.run(
        [sys.executable, "-c", rewritten],
        check=False,
        capture_output=True,
        text=True,
    )


class FaultDrillCiContractTests(unittest.TestCase):
    def test_redis_contract_explicitly_disclaims_runtime_durability(self) -> None:
        tree = ast.parse(REDIS_TEST_PATH.read_text(encoding="utf-8"))
        docstring = ast.get_docstring(tree) or ""
        self.assertIn("Static Redis Compose contract", docstring)
        self.assertIn("do not start Redis", docstring)
        self.assertIn("RPO", docstring)

    def test_postgres_junit_guards_execute_fail_closed(self) -> None:
        workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))

        def report_xml(
            identities: tuple[tuple[str, str], ...],
            *,
            outcomes: dict[int, str] | None = None,
            aggregate: bool = False,
        ) -> str:
            outcomes = outcomes or {}
            rows = [
                f'<testcase classname="{classname}" name="{name}">'
                f"{outcomes.get(index, '')}</testcase>"
                for index, (classname, name) in enumerate(identities)
            ]
            if aggregate:
                midpoint = len(identities) // 2
                return (
                    '<testsuites><testsuite name="first">'
                    + "".join(rows[:midpoint])
                    + '</testsuite><testsuite name="second">'
                    + "".join(rows[midpoint:])
                    + "</testsuite></testsuites>"
                )
            return (
                '<testsuites><testsuite name="suite">'
                + "".join(rows)
                + ("</testsuite></testsuites>")
            )

        with tempfile.TemporaryDirectory() as temp:
            for suite_name, test_path, canonical_path in SUITES:
                expected_count = EXPECTED_CASE_COUNTS[test_path]
                expected_identities = EXPECTED_CASE_IDENTITIES[test_path]
                label = (
                    "Gate 3 PostgreSQL lease"
                    if suite_name == "lease"
                    else "Gate 3 similarity"
                )
                cases = (
                    ("all-pass", report_xml(expected_identities), 0, ""),
                    (
                        "xunit2-aggregate",
                        report_xml(expected_identities, aggregate=True),
                        0,
                        "",
                    ),
                    (
                        "all-skipped",
                        report_xml(expected_identities, outcomes={0: "<skipped/>"}),
                        1,
                        f"{label} suite contains skipped tests\n",
                    ),
                    (
                        "failure",
                        report_xml(expected_identities, outcomes={0: "<failure/>"}),
                        1,
                        f"{label} suite contains failed tests\n",
                    ),
                    (
                        "error",
                        report_xml(expected_identities, outcomes={0: "<error/>"}),
                        1,
                        f"{label} suite contains errored tests\n",
                    ),
                    (
                        "mixed",
                        report_xml(
                            expected_identities,
                            outcomes={0: "<skipped/>", 1: "<failure/>", 2: "<error/>"},
                        ),
                        1,
                        f"{label} suite contains skipped tests\n",
                    ),
                    (
                        "zero-collected",
                        report_xml(()),
                        1,
                        f"{label} suite expected exactly {expected_count} tests\n",
                    ),
                    (
                        "under-collected",
                        report_xml(expected_identities[:-1]),
                        1,
                        f"{label} suite expected exactly {expected_count} tests\n",
                    ),
                    (
                        "over-collected",
                        report_xml(
                            (*expected_identities, ("attacker.extra", "test_extra"))
                        ),
                        1,
                        f"{label} suite expected exactly {expected_count} tests\n",
                    ),
                    (
                        "duplicate-identity",
                        report_xml((*expected_identities[:-1], expected_identities[0])),
                        1,
                        f"{label} suite contains duplicate testcase identities\n",
                    ),
                    (
                        "wrong-identity",
                        report_xml(
                            (*expected_identities[:-1], ("attacker.swap", "test_swap"))
                        ),
                        1,
                        f"{label} suite does not match exact testcase identities\n",
                    ),
                    (
                        "missing-file",
                        None,
                        1,
                        f"{label} JUnit report is missing or invalid\n",
                    ),
                    (
                        "bad-xml",
                        "not XML",
                        1,
                        f"{label} JUnit report is missing or invalid\n",
                    ),
                )
                source = _extract_junit_guard(
                    workflow,
                    test_path=test_path,
                    report_path=canonical_path,
                )
                for name, xml, expected_returncode, expected_stderr in cases:
                    with self.subTest(suite=suite_name, case=name):
                        report = Path(temp) / f"{suite_name}-{name}.xml"
                        if xml is not None:
                            report.write_text(xml, encoding="utf-8")
                        result = _run_guard(
                            source,
                            report,
                            canonical_path=canonical_path,
                        )
                        self.assertEqual(result.returncode, expected_returncode)
                        self.assertEqual(result.stderr, expected_stderr)

    def test_postgres_guard_contract_rejects_exact_fail_open_mutations(self) -> None:
        workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        _job, step = _lease_job_and_step(workflow)
        for _suite_name, test_path, report_path in SUITES:
            _extract_junit_guard(
                workflow,
                test_path=test_path,
                report_path=report_path,
            )
        self.assertNotEqual(LEASE_JUNIT_PATH, SIMILARITY_JUNIT_PATH)
        mutations: list[tuple[str, dict, str, str, str]] = []

        heredoc_comment = copy.deepcopy(workflow)
        _j, comment_step = _lease_job_and_step(heredoc_comment)
        original = comment_step["run"]
        comment_step["run"] = original.replace(
            "import sys\n",
            "import sys\n# harmless contract fixture: || true\n",
            1,
        )
        self.assertNotEqual(comment_step["run"], original)
        _extract_junit_guard(
            heredoc_comment,
            test_path=LEASE_TEST_PATH,
            report_path=LEASE_JUNIT_PATH,
        )

        for scope in ("workflow", "job"):
            null_environment = copy.deepcopy(workflow)
            if scope == "workflow":
                container = null_environment
            else:
                container, _step = _lease_job_and_step(null_environment)
            container["env"] = None
            _extract_junit_guard(
                null_environment,
                test_path=LEASE_TEST_PATH,
                report_path=LEASE_JUNIT_PATH,
            )

        for scope in ("job", "step"):
            for condition in (True, "true", "${{ true }}", "${{   true   }}"):
                literal_true = copy.deepcopy(workflow)
                mutated_job, mutated_step = _lease_job_and_step(literal_true)
                container = mutated_job if scope == "job" else mutated_step
                container["if"] = condition
                _extract_junit_guard(
                    literal_true,
                    test_path=LEASE_TEST_PATH,
                    report_path=LEASE_JUNIT_PATH,
                )

        for scope, pytest_key in (
            ("workflow", "PYTEST_ADDOPTS"),
            ("job", "PYTEST_PLUGINS"),
            ("step", "PYTEST_DISABLE_PLUGIN_AUTOLOAD"),
        ):
            environment_override = copy.deepcopy(workflow)
            if scope == "workflow":
                container = environment_override
            else:
                mutated_job, mutated_step = _lease_job_and_step(environment_override)
                container = mutated_job if scope == "job" else mutated_step
            container.setdefault("env", {})[pytest_key] = "-k selected"
            mutations.append(
                (
                    f"{scope} {pytest_key}",
                    environment_override,
                    LEASE_TEST_PATH,
                    LEASE_JUNIT_PATH,
                    f"lease proof {scope} must not define PYTEST_* environment variables",
                )
            )

        influence_keys = (
            ("workflow", "BASH_ENV"),
            ("job", "ENV"),
            ("step", "PATH"),
            ("workflow", "PYTHONPATH"),
            ("job", "SHELLOPTS"),
            ("step", "PYTHONHOME"),
            ("workflow", "LD_PRELOAD"),
        )
        for scope, environment_key in influence_keys:
            environment_override = copy.deepcopy(workflow)
            mutated_job, mutated_step = _lease_job_and_step(environment_override)
            container = {
                "workflow": environment_override,
                "job": mutated_job,
                "step": mutated_step,
            }[scope]
            container.setdefault("env", {})[environment_key] = "/tmp/attacker"
            mutations.append(
                (
                    f"{scope} {environment_key}",
                    environment_override,
                    LEASE_TEST_PATH,
                    LEASE_JUNIT_PATH,
                    f"lease proof {scope} must not define shell or interpreter influence environment variables",
                )
            )

        skipped_erased = copy.deepcopy(workflow)
        _j, skipped_step = _lease_job_and_step(skipped_erased)
        original = skipped_step["run"]
        skipped_step["run"] = original.replace(
            'skipped = [case for case in cases if case.find("skipped") is not None]',
            "skipped = []",
            1,
        )
        self.assertNotEqual(skipped_step["run"], original)
        mutations.append(
            (
                "skipped list erased",
                skipped_erased,
                LEASE_TEST_PATH,
                LEASE_JUNIT_PATH,
                "JUnit guard must derive skipped cases from the report",
            )
        )

        weak_count = copy.deepcopy(workflow)
        _j, weak_count_step = _lease_job_and_step(weak_count)
        original = weak_count_step["run"]
        weak_count_step["run"] = original.replace(
            "if len(cases) != 7:",
            "if not cases:",
            1,
        )
        self.assertNotEqual(weak_count_step["run"], original)
        mutations.append(
            (
                "lease exact count weakened",
                weak_count,
                LEASE_TEST_PATH,
                LEASE_JUNIT_PATH,
                "JUnit guard must require exactly 7 testcase nodes",
            )
        )

        wrong_parse_path = copy.deepcopy(workflow)
        _j, wrong_path_step = _lease_job_and_step(wrong_parse_path)
        original = wrong_path_step["run"]
        wrong_path_step["run"] = original.replace(
            f'report = Path("{LEASE_JUNIT_PATH}")',
            'report = Path("/tmp/wrong-worker-lease.xml")',
            1,
        )
        self.assertNotEqual(wrong_path_step["run"], original)
        mutations.append(
            (
                "wrong ET.parse path",
                wrong_parse_path,
                LEASE_TEST_PATH,
                LEASE_JUNIT_PATH,
                "JUnit guard must bind report to the emitted report path",
            )
        )

        job_continue = copy.deepcopy(workflow)
        mutated_job, _s = _lease_job_and_step(job_continue)
        mutated_job["continue-on-error"] = True
        mutations.append(
            (
                "job continue-on-error",
                job_continue,
                LEASE_TEST_PATH,
                LEASE_JUNIT_PATH,
                "lease proof job and step must fail closed",
            )
        )

        step_continue = copy.deepcopy(workflow)
        _j, mutated_step = _lease_job_and_step(step_continue)
        mutated_step["continue-on-error"] = True
        mutations.append(
            (
                "step continue-on-error",
                step_continue,
                LEASE_TEST_PATH,
                LEASE_JUNIT_PATH,
                "lease proof job and step must fail closed",
            )
        )

        for scope, condition in (
            ("job", False),
            ("job", "${{ false }}"),
            ("step", False),
            ("step", "${{ github.ref == 'refs/heads/never' }}"),
        ):
            conditional = copy.deepcopy(workflow)
            mutated_job, mutated_step = _lease_job_and_step(conditional)
            container = mutated_job if scope == "job" else mutated_step
            container["if"] = condition
            mutations.append(
                (
                    f"{scope} conditional skip {condition!r}",
                    conditional,
                    LEASE_TEST_PATH,
                    LEASE_JUNIT_PATH,
                    f"lease proof {scope} must run unconditionally or use literal true",
                )
            )

        workflow_shell = copy.deepcopy(workflow)
        workflow_shell.setdefault("defaults", {}).setdefault("run", {})["shell"] = (
            "bash {0}"
        )
        mutations.append(
            (
                "workflow shell override",
                workflow_shell,
                LEASE_TEST_PATH,
                LEASE_JUNIT_PATH,
                "lease proof workflow must not override the Actions run shell",
            )
        )

        job_shell = copy.deepcopy(workflow)
        mutated_job, _s = _lease_job_and_step(job_shell)
        mutated_job.setdefault("defaults", {}).setdefault("run", {})["shell"] = (
            "bash {0}"
        )
        mutations.append(
            (
                "job shell override",
                job_shell,
                LEASE_TEST_PATH,
                LEASE_JUNIT_PATH,
                "lease proof job must not override the Actions run shell",
            )
        )

        step_shell = copy.deepcopy(workflow)
        _j, mutated_step = _lease_job_and_step(step_shell)
        mutated_step["shell"] = "bash {0}"
        mutations.append(
            (
                "step shell override",
                step_shell,
                LEASE_TEST_PATH,
                LEASE_JUNIT_PATH,
                "lease proof step must not override the Actions run shell",
            )
        )

        workflow_directory = copy.deepcopy(workflow)
        workflow_directory.setdefault("defaults", {}).setdefault("run", {})[
            "working-directory"
        ] = "attacker"
        mutations.append(
            (
                "workflow working-directory override",
                workflow_directory,
                LEASE_TEST_PATH,
                LEASE_JUNIT_PATH,
                "lease proof workflow must not override the Actions working directory",
            )
        )

        job_directory = copy.deepcopy(workflow)
        mutated_job, _s = _lease_job_and_step(job_directory)
        mutated_job.setdefault("defaults", {}).setdefault("run", {})[
            "working-directory"
        ] = "attacker"
        mutations.append(
            (
                "job working-directory override",
                job_directory,
                LEASE_TEST_PATH,
                LEASE_JUNIT_PATH,
                "lease proof job must not override the Actions working directory",
            )
        )

        step_directory = copy.deepcopy(workflow)
        _j, mutated_step = _lease_job_and_step(step_directory)
        mutated_step["working-directory"] = "attacker"
        mutations.append(
            (
                "step working-directory override",
                step_directory,
                LEASE_TEST_PATH,
                LEASE_JUNIT_PATH,
                "lease proof step must not override the Actions working directory",
            )
        )

        for persistence_target in (
            "$GITHUB_ENV",
            "${GITHUB_PATH}",
            "${GITHUB_ENV:?required}",
            "${!GITHUB_PATH}",
            "${ GITHUB_ENV }",
        ):
            persistence = copy.deepcopy(workflow)
            mutated_job, _s = _lease_job_and_step(persistence)
            run_step = next(
                candidate
                for candidate in mutated_job["steps"]
                if isinstance(candidate.get("run"), str)
                and candidate.get("name") != LEASE_STEP_NAME
            )
            run_step["run"] += f'\necho "attacker=1" >> "{persistence_target}"'
            mutations.append(
                (
                    f"postgres run persistence {persistence_target}",
                    persistence,
                    LEASE_TEST_PATH,
                    LEASE_JUNIT_PATH,
                    "postgres-rls run steps must not reference GITHUB_ENV or GITHUB_PATH",
                )
            )

        identity_drift = copy.deepcopy(workflow)
        _j, identity_step = _lease_job_and_step(identity_drift)
        original = identity_step["run"]
        identity_step["run"] = original.replace(
            "test_claim_is_single_owner_and_stale_attempt_cannot_reopen_terminal",
            "test_attacker_selected_replacement",
            1,
        )
        self.assertNotEqual(identity_step["run"], original)
        mutations.append(
            (
                "lease JUnit identity drift",
                identity_drift,
                LEASE_TEST_PATH,
                LEASE_JUNIT_PATH,
                f"JUnit guard identities for {LEASE_TEST_PATH} must exactly match test source",
            )
        )

        for shell_suffix in ("|| true", "|| :"):
            suppressed = copy.deepcopy(workflow)
            _j, suppressed_step = _lease_job_and_step(suppressed)
            original = suppressed_step["run"]
            suppressed_step["run"] = original.replace(
                f"-q --junitxml={LEASE_JUNIT_PATH}",
                f"-q --junitxml={LEASE_JUNIT_PATH} {shell_suffix}",
                1,
            )
            self.assertNotEqual(suppressed_step["run"], original)
            mutations.append(
                (
                    f"pytest {shell_suffix}",
                    suppressed,
                    LEASE_TEST_PATH,
                    LEASE_JUNIT_PATH,
                    "lease proof step contains unsupported shell control",
                )
            )

        for shell_suffix in (
            "&> /tmp/gate3-proof.log",
            "|& true",
            "2>/tmp/gate3-proof.log",
            "$(true)",
            "<(true)",
        ):
            redirected = copy.deepcopy(workflow)
            _j, redirected_step = _lease_job_and_step(redirected)
            original = redirected_step["run"]
            redirected_step["run"] = original.replace(
                f"-q --junitxml={LEASE_JUNIT_PATH}",
                f"-q --junitxml={LEASE_JUNIT_PATH} {shell_suffix}",
                1,
            )
            self.assertNotEqual(redirected_step["run"], original)
            expected = (
                "lease proof pytest command must not contain shell expansion"
                if "$" in shell_suffix or "`" in shell_suffix
                else "lease proof step contains unsupported shell control"
            )
            mutations.append(
                (
                    f"pytest shell control {shell_suffix}",
                    redirected,
                    LEASE_TEST_PATH,
                    LEASE_JUNIT_PATH,
                    expected,
                )
            )

        for name, suffix, expected in (
            (
                "pytest selection -k",
                " -k selected_test",
                f"pytest command for {LEASE_TEST_PATH} must use exact allowed argv",
            ),
            (
                "pytest selection -m",
                " -m integration",
                f"pytest command for {LEASE_TEST_PATH} must use exact allowed argv",
            ),
            (
                "pytest deselect",
                " --deselect=test_name",
                f"pytest command for {LEASE_TEST_PATH} must use exact allowed argv",
            ),
            (
                "pytest ignore",
                " --ignore=other.py",
                f"pytest command for {LEASE_TEST_PATH} must use exact allowed argv",
            ),
            (
                "pytest dollar expansion",
                " $EXTRA",
                "lease proof pytest command must not contain shell expansion",
            ),
            (
                "pytest braced expansion",
                " ${EXTRA}",
                "lease proof pytest command must not contain shell expansion",
            ),
            (
                "pytest extra junit",
                " --junitxml=/tmp/attacker.xml",
                f"pytest command for {LEASE_TEST_PATH} must use exact allowed argv",
            ),
            (
                "pytest extra path",
                " attacker-tests/",
                f"pytest command for {LEASE_TEST_PATH} must use exact allowed argv",
            ),
        ):
            extra_argv = copy.deepcopy(workflow)
            _j, extra_argv_step = _lease_job_and_step(extra_argv)
            original = extra_argv_step["run"]
            extra_argv_step["run"] = original.replace(
                f"-q --junitxml={LEASE_JUNIT_PATH}",
                f"-q --junitxml={LEASE_JUNIT_PATH}{suffix}",
                1,
            )
            self.assertNotEqual(extra_argv_step["run"], original)
            mutations.append(
                (
                    name,
                    extra_argv,
                    LEASE_TEST_PATH,
                    LEASE_JUNIT_PATH,
                    expected,
                )
            )

        report_collision = copy.deepcopy(workflow)
        _j, collision_step = _lease_job_and_step(report_collision)
        original = collision_step["run"]
        collision_step["run"] = original.replace(
            SIMILARITY_JUNIT_PATH,
            LEASE_JUNIT_PATH,
        )
        self.assertNotEqual(collision_step["run"], original)
        mutations.append(
            (
                "report collision",
                report_collision,
                SIMILARITY_TEST_PATH,
                SIMILARITY_JUNIT_PATH,
                f"{SIMILARITY_JUNIT_PATH} must be one unique JUnit report",
            )
        )

        for suite_name, test_path, report_path in SUITES:
            invocation = (
                f"python -m pytest \\\n  {test_path} \\\n  -q --junitxml={report_path}"
            )

            echoed_path = copy.deepcopy(workflow)
            _j, echoed_step = _lease_job_and_step(echoed_path)
            original = echoed_step["run"]
            replacement = f"echo {test_path}\n" + invocation.replace(
                test_path, f"some-other-{suite_name}-test.py"
            )
            echoed_step["run"] = original.replace(invocation, replacement, 1)
            self.assertNotEqual(echoed_step["run"], original)
            mutations.append(
                (
                    f"{suite_name} echoed path with unrelated pytest source",
                    echoed_path,
                    test_path,
                    report_path,
                    "lease proof step contains unsupported shell command",
                )
            )

            split_binding = copy.deepcopy(workflow)
            _j, split_step = _lease_job_and_step(split_binding)
            original = split_step["run"]
            replacement = (
                f"python -m pytest {test_path} -q\necho --junitxml={report_path}"
            )
            split_step["run"] = original.replace(invocation, replacement, 1)
            self.assertNotEqual(split_step["run"], original)
            mutations.append(
                (
                    f"{suite_name} test and report split across commands",
                    split_binding,
                    test_path,
                    report_path,
                    "lease proof step contains unsupported shell command",
                )
            )

        subshell = copy.deepcopy(workflow)
        _j, subshell_step = _lease_job_and_step(subshell)
        original = subshell_step["run"]
        invocation = (
            "python -m pytest \\\n"
            f"  {LEASE_TEST_PATH} \\\n"
            f"  -q --junitxml={LEASE_JUNIT_PATH}"
        )
        subshell_step["run"] = original.replace(invocation, f"({invocation})", 1)
        self.assertNotEqual(subshell_step["run"], original)
        mutations.append(
            (
                "pytest subshell",
                subshell,
                LEASE_TEST_PATH,
                LEASE_JUNIT_PATH,
                "lease proof step contains unsupported shell control",
            )
        )

        masked_exit = copy.deepcopy(workflow)
        _j, masked_exit_step = _lease_job_and_step(masked_exit)
        original = masked_exit_step["run"]
        masked_exit_step["run"] = original.replace(
            invocation,
            f"{invocation}\nexit 0",
            1,
        )
        self.assertNotEqual(masked_exit_step["run"], original)
        mutations.append(
            (
                "pytest followed by success exit",
                masked_exit,
                LEASE_TEST_PATH,
                LEASE_JUNIT_PATH,
                "lease proof step contains unsupported shell command",
            )
        )

        self.assertIn(LEASE_TEST_PATH, step["run"])
        self.assertIn(SIMILARITY_TEST_PATH, step["run"])
        for name, mutated, test_path, report_path, expected in mutations:
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(
                    AssertionError,
                    re.escape(expected),
                ),
            ):
                _extract_junit_guard(
                    mutated,
                    test_path=test_path,
                    report_path=report_path,
                )


if __name__ == "__main__":
    unittest.main()
