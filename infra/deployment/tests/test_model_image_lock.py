from __future__ import annotations

import importlib.util
import gzip
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "infra/deployment/scripts/verify-model-image-lock.py"
EXPORTER = ROOT / "infra/deployment/intranet/build-and-export-for-intranet.sh"
SPEC = importlib.util.spec_from_file_location("verify_model_image_lock", SCRIPT)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError(f"cannot import {SCRIPT}")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ModelImageLockTests(unittest.TestCase):
    @staticmethod
    def _require_bash() -> str:
        bash = shutil.which("bash")
        if bash is None:
            raise AssertionError("Bash is required for the exporter loader contract")
        return bash

    @staticmethod
    def _extract_loader() -> str:
        exporter = EXPORTER.read_text(encoding="utf-8")
        marker = 'cat > "$OUTPUT_DIR/INTRANET-LOAD.sh" <<\'EOF\'\n'
        suffix = '\nEOF\nchmod +x "$OUTPUT_DIR/INTRANET-LOAD.sh"'
        try:
            body = exporter.split(marker, 1)[1]
            loader, _ = body.split(suffix, 1)
        except (IndexError, ValueError) as exc:
            raise AssertionError("exporter INTRANET-LOAD heredoc is missing") from exc
        return loader.replace("\r\n", "\n") + "\n"

    @staticmethod
    def _write_gzip(path: Path) -> None:
        with gzip.open(path, "wb") as stream:
            stream.write(b"synthetic docker archive")

    @staticmethod
    def _write_weight_tar(path: Path) -> None:
        with tarfile.open(path, "w") as archive:
            payload = b"synthetic model weight"
            info = tarfile.TarInfo("weight.bin")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    @staticmethod
    def _write_manifest(root: Path, *, omit: set[str] | None = None) -> None:
        omitted = omit or set()
        rows: list[str] = []
        for path in sorted(root.iterdir(), key=lambda item: item.name):
            if path.name == "CHECKSUMS.sha256" or path.name in omitted:
                continue
            if not path.is_file():
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            rows.append(f"{digest}  {path.name}")
        (root / "CHECKSUMS.sha256").write_bytes(
            ("\n".join(rows) + "\n").encode("utf-8")
        )

    @classmethod
    def _make_loader_bundle(
        cls,
        root: Path,
        *,
        include_models: bool = False,
        include_lock: bool = False,
        include_weights: bool = False,
        omit_checksums: set[str] | None = None,
        duplicate_checksum: str | None = None,
        binary_checksum_marker: bool = False,
    ) -> str:
        required = {
            "PLATFORM-IMAGE-INVENTORY.tsv": "# service\timage\tbundle\tactivation\tsource\n",
            "PLATFORM-IMAGE-LOCK.tsv": "# service\timage\timage_id\tbundle\tactivation\n",
            "MODEL-IMAGE-INVENTORY.tsv": "# service\timage\tactivation\n",
            "MODEL-IMAGE-STATUS.tsv": "# image\tactivation\tstatus\tnote\n",
        }
        for name, content in required.items():
            (root / name).write_text(content, encoding="utf-8")
        for name in ("01-anila-built.tar.gz", "02-base.tar.gz", "03-cold.tar.gz"):
            cls._write_gzip(root / name)
        if include_models:
            cls._write_gzip(root / "04-models.tar.gz")
        if include_lock:
            (root / "MODEL-IMAGE-LOCK.tsv").write_text(
                "# service\timage\timage_id\tactivation\n", encoding="utf-8"
            )
        if include_weights:
            cls._write_weight_tar(root / "05-weights-demo.tar")
        cls._write_manifest(root, omit=omit_checksums)
        if duplicate_checksum is not None:
            checksum_path = root / "CHECKSUMS.sha256"
            lines = checksum_path.read_text(encoding="utf-8").splitlines()
            matching = [line for line in lines if line.endswith(f"  {duplicate_checksum}")]
            if not matching:
                raise AssertionError(f"fixture checksum missing: {duplicate_checksum}")
            checksum_path.write_bytes(
                ("\n".join(lines + [matching[0]]) + "\n").encode("utf-8")
            )
        if binary_checksum_marker:
            checksum_path = root / "CHECKSUMS.sha256"
            lines = checksum_path.read_text(encoding="utf-8").splitlines()
            replaced = [
                line.replace("  04-models.tar.gz", " *04-models.tar.gz", 1)
                if line.endswith("  04-models.tar.gz")
                else line
                for line in lines
            ]
            checksum_path.write_bytes(("\n".join(replaced) + "\n").encode("utf-8"))
        return cls._extract_loader()

    @staticmethod
    def _write_fake_docker(bin_dir: Path) -> None:
        docker = bin_dir / "docker"
        docker.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "case \"${1:-}\" in\n"
            "  load) cat >/dev/null ;;\n"
            "  images) : ;;\n"
            "  *) : ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        docker.chmod(0o755)

    @classmethod
    def _run_loader(cls, root: Path, loader: str) -> subprocess.CompletedProcess[str]:
        bin_dir = root / "bin"
        bin_dir.mkdir()
        cls._write_fake_docker(bin_dir)
        bash = cls._require_bash()
        environment = os.environ.copy()
        environment["PATH"] = os.pathsep.join((str(bin_dir), environment["PATH"]))
        environment["ANILA_HF_DIR"] = str(root / "weights")
        # Feeding the generated heredoc on stdin is portable across Windows
        # and Linux, but Bash has no BASH_SOURCE[0] for stdin scripts.  Keep
        # the exact source for the syntax test; normalize only this launcher
        # path before exercising the loader's integrity gates.
        loader_for_stdin = loader.replace(
            'cd "$(dirname "${BASH_SOURCE[0]}")"', 'cd "."', 1
        )
        completed = subprocess.run(
            [bash],
            input=loader_for_stdin.encode("utf-8"),
            cwd=root,
            env=environment,
            check=False,
            capture_output=True,
            text=False,
        )
        return subprocess.CompletedProcess(
            completed.args,
            completed.returncode,
            completed.stdout.decode("utf-8", errors="replace"),
            completed.stderr.decode("utf-8", errors="replace"),
        )

    def setUp(self) -> None:
        self.inventory = MODULE.read_inventory(MODULE.DEFAULT_INVENTORY)
        self.compose = MODULE.read_compose(MODULE.DEFAULT_COMPOSE)
        MODULE.verify_compose_inventory(self.compose, self.inventory)

    def _lock_text(self, services: set[str] | None = None) -> str:
        selected = services or MODULE.active_services(self.inventory)
        rows = ["# service\timage\timage_id\tactivation"]
        for index, (service, entry) in enumerate(self.inventory.items(), 1):
            if service not in selected:
                continue
            rows.append(
                "\t".join(
                    (
                        service,
                        entry.image,
                        "sha256:" + f"{index:064x}",
                        entry.activation,
                    )
                )
            )
        return "\n".join(rows) + "\n"

    def test_current_compose_and_inventory_form_closed_default_set(self) -> None:
        self.assertEqual(
            MODULE.active_services(self.inventory),
            {"gpt-oss-20b", "gemma4", "nv-embed-triton", "nv-embed-proxy"},
        )

    def test_explicit_profiles_extend_default_set(self) -> None:
        self.assertEqual(
            MODULE.active_services(self.inventory, profiles={"intranet"}),
            {
                "gpt-oss-20b",
                "gemma4",
                "nv-embed-triton",
                "nv-embed-proxy",
                "gemma-4-26b-a4b",
                "gemma-4-12b",
                "gpt-oss-120b",
            },
        )

    def test_unknown_profile_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            MODULE.ModelImageLockError, "unknown model profile"
        ):
            MODULE.active_services(self.inventory, profiles={"not-enabled"})

    def test_default_lock_is_accepted_and_optional_rows_are_extra(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "MODEL-IMAGE-LOCK.tsv"
            path.write_text(self._lock_text(), encoding="utf-8")
            lock = MODULE.read_lock(path, self.inventory, self.compose)
            self.assertEqual(set(lock), MODULE.active_services(self.inventory))

            all_services = MODULE.active_services(self.inventory, include_optional=True)
            path.write_text(self._lock_text(all_services), encoding="utf-8")
            with self.assertRaisesRegex(MODULE.ModelImageLockError, "extra"):
                MODULE.read_lock(path, self.inventory, self.compose)

    def test_profile_lock_requires_selected_profile_and_rejects_missing(self) -> None:
        services = MODULE.active_services(self.inventory, profiles={"flux-approved"})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "MODEL-IMAGE-LOCK.tsv"
            path.write_text(self._lock_text(services), encoding="utf-8")
            MODULE.read_lock(
                path,
                self.inventory,
                self.compose,
                profiles={"flux-approved"},
            )
            with self.assertRaisesRegex(MODULE.ModelImageLockError, "missing"):
                MODULE.read_lock(path, self.inventory, self.compose)

    def test_duplicate_missing_extra_and_invalid_digest_are_rejected(self) -> None:
        base = self._lock_text()
        first_row = base.splitlines()[1]
        mutations = {
            "duplicate": base + first_row + "\n",
            "extra": base + "unknown\tunknown:tag\tsha256:" + "f" * 64 + "\tdefault\n",
            "invalid": base.replace("sha256:000", "tag:", 1),
        }
        with tempfile.TemporaryDirectory() as directory:
            for name, text in mutations.items():
                path = Path(directory) / f"{name}.tsv"
                path.write_text(text, encoding="utf-8")
                with (
                    self.subTest(name=name),
                    self.assertRaises(MODULE.ModelImageLockError),
                ):
                    MODULE.read_lock(path, self.inventory, self.compose)

            missing = "\n".join(base.splitlines()[:-1]) + "\n"
            path = Path(directory) / "missing.tsv"
            path.write_text(missing, encoding="utf-8")
            with self.assertRaisesRegex(MODULE.ModelImageLockError, "missing"):
                MODULE.read_lock(path, self.inventory, self.compose)

    def test_image_and_activation_metadata_must_match_inventory(self) -> None:
        mutated = self._lock_text().replace(
            "gpt-oss-20b\ttensorrt-llm-hf:1.3.0rc10\t",
            "gpt-oss-20b\tother-model:tag\t",
            1,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mutated.tsv"
            path.write_text(mutated, encoding="utf-8")
            with self.assertRaisesRegex(
                MODULE.ModelImageLockError, "metadata mismatch"
            ):
                MODULE.read_lock(path, self.inventory, self.compose)

    def test_compose_image_or_profile_mutation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            compose_path = Path(directory) / "docker-compose.yml"
            compose_text = MODULE.DEFAULT_COMPOSE.read_text(encoding="utf-8")
            compose_path.write_text(
                compose_text.replace(
                    "image: vllm-gemma4:latest", "image: vllm-gemma4:mutated", 1
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(MODULE.ModelImageLockError, "image ref drift"):
                MODULE.verify_compose_inventory(
                    MODULE.read_compose(compose_path), self.inventory
                )

    def test_tag_drift_is_detected_by_optional_docker_readback(self) -> None:
        lock_text = self._lock_text()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "MODEL-IMAGE-LOCK.tsv"
            path.write_text(lock_text, encoding="utf-8")
            lock = MODULE.read_lock(path, self.inventory, self.compose)
            expected = {entry.image: entry.image_id for entry in lock.values()}

            def run(command: list[str]) -> subprocess.CompletedProcess[str]:
                image_ref = command[-1]
                image_id = expected.get(image_ref, image_ref)
                return subprocess.CompletedProcess(command, 0, image_id + "\n", "")

            with mock.patch.object(MODULE, "_run_utf8", side_effect=run):
                MODULE.verify_docker_images(lock)

            mutated = dict(lock)
            first_service = next(iter(mutated))
            first = mutated[first_service]
            mutated[first_service] = MODULE.LockEntry(
                first.service,
                first.image,
                "sha256:" + "f" * 64,
                first.activation,
            )
            with (
                mock.patch.object(MODULE, "_run_utf8", side_effect=run),
                self.assertRaisesRegex(MODULE.ModelImageLockError, "tag drift"),
            ):
                MODULE.verify_docker_images(mutated)

    def test_include_optional_container_readback_enables_every_profile(self) -> None:
        all_services = MODULE.active_services(self.inventory, include_optional=True)
        image_by_ref = {
            image: "sha256:" + f"{index:064x}"
            for index, image in enumerate(
                sorted({self.inventory[service].image for service in all_services}), 1
            )
        }
        lock_by_service = {
            service: image_by_ref[self.inventory[service].image]
            for service in all_services
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "MODEL-IMAGE-LOCK.tsv"
            rows = ["# service\timage\timage_id\tactivation"]
            for service, entry in self.inventory.items():
                if service not in all_services:
                    continue
                rows.append(
                    "\t".join(
                        (
                            service,
                            entry.image,
                            lock_by_service[service],
                            entry.activation,
                        )
                    )
                )
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            commands: list[list[str]] = []

            def run(command: list[str]) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                if command[:3] == ["docker", "image", "inspect"]:
                    ref = command[-1]
                    return subprocess.CompletedProcess(
                        command, 0, image_by_ref.get(ref, ref) + "\n", ""
                    )
                if command[:3] == ["docker", "container", "inspect"]:
                    service = command[-1].removeprefix("cid-")
                    return subprocess.CompletedProcess(
                        command, 0, lock_by_service[service] + "\n", ""
                    )
                if "--services" in command:
                    return subprocess.CompletedProcess(
                        command, 0, "\n".join(sorted(all_services)) + "\n", ""
                    )
                service = command[-1]
                return subprocess.CompletedProcess(command, 0, f"cid-{service}\n", "")

            with mock.patch.object(MODULE, "_run_utf8", side_effect=run):
                MODULE.verify_lock(
                    path,
                    compose_path=MODULE.DEFAULT_COMPOSE,
                    inventory_path=MODULE.DEFAULT_INVENTORY,
                    include_optional=True,
                    inspect_containers=True,
                )
            service_query = next(
                command for command in commands if "--services" in command
            )
            self.assertIn("--profile", service_query)
            self.assertEqual(
                {
                    service_query[index + 1]
                    for index, value in enumerate(service_query)
                    if value == "--profile"
                },
                {"flux-approved", "intranet"},
            )

    def test_cli_discovery_is_unittest_compatible(self) -> None:
        parser = MODULE._parser()
        self.assertEqual(parser.parse_args(["check-wiring"]).command, "check-wiring")
        self.assertEqual(
            parser.parse_args(["emit-lock", "--profile", "intranet"]).profiles_override,
            ["intranet"],
        )

    def test_exporter_uses_one_enabled_closure_for_archive_and_lock(self) -> None:
        exporter = (
            ROOT / "infra/deployment/intranet/build-and-export-for-intranet.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("MODEL_PROFILES", exporter)
        self.assertIn("verify-model-image-lock.py", exporter)
        self.assertIn("emit-lock", exporter)
        self.assertIn("verify-lock", exporter)
        self.assertIn("--inspect-docker", exporter)
        self.assertIn("awk -F '\\t' '", exporter)
        self.assertIn("NF == 4 && !seen[$2]++", exporter)
        self.assertIn(
            "enabled model image disappeared after content-ID lock generation", exporter
        )
        self.assertIn("MODEL-IMAGE-LOCK.tsv", exporter)
        self.assertIn("MODEL-IMAGE-STATUS.tsv", exporter)
        self.assertIn("not a signed release envelope, SBOM", exporter)
        self.assertIn("if [ -f MODEL-IMAGE-LOCK.tsv ]; then", exporter)
        self.assertIn('sha256sum "${CHECKSUM_FILES[@]}" > CHECKSUMS.sha256', exporter)

    def test_exporter_rejects_ambiguous_or_unsafe_model_profiles(self) -> None:
        exporter = (
            ROOT / "infra/deployment/intranet/build-and-export-for-intranet.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("MODEL_PROFILES contains an empty profile token", exporter)
        self.assertIn("MODEL_PROFILES contains duplicate profile", exporter)
        self.assertIn("unsafe profile name", exporter)

    def test_loader_rechecks_model_lock_content_ids_after_load(self) -> None:
        exporter = (
            ROOT / "infra/deployment/intranet/build-and-export-for-intranet.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("Verifying model image content-ID lock", exporter)
        self.assertIn(
            "docker image inspect --format '{{.Id}}' \"$lock_image\"", exporter
        )
        self.assertIn("model image content-ID closure failed", exporter)
        self.assertIn("MODEL-IMAGE-LOCK extra/unknown service", exporter)

    def test_exporter_heredoc_loader_is_platform_executable(self) -> None:
        loader = self._extract_loader()
        bash = self._require_bash()
        completed = subprocess.run(
            [bash, "-n"],
            input=loader.encode("utf-8"),
            check=False,
            capture_output=True,
            text=False,
        )
        stderr = completed.stderr.decode("utf-8", errors="replace")
        self.assertEqual(completed.returncode, 0, stderr)
        self.assertIn("require_exact_checksum_entry", loader)
        self.assertIn("require_regular_bundle_file", loader)

    def test_loader_rejects_optional_artifacts_without_exact_checksum(self) -> None:
        self._require_bash()
        cases = (
            (
                "model archive",
                dict(
                    include_models=True,
                    include_lock=True,
                    omit_checksums={"04-models.tar.gz"},
                ),
                "04-models.tar.gz",
            ),
            (
                "model lock",
                dict(
                    include_models=True,
                    include_lock=True,
                    omit_checksums={"MODEL-IMAGE-LOCK.tsv"},
                ),
                "MODEL-IMAGE-LOCK.tsv",
            ),
            (
                "weight archive",
                dict(include_weights=True, omit_checksums={"05-weights-demo.tar"}),
                "05-weights-demo.tar",
            ),
        )
        for name, options, artifact in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                loader = self._make_loader_bundle(root, **options)
                completed = self._run_loader(root, loader)
                self.assertNotEqual(completed.returncode, 0, completed.stdout)
                self.assertIn(artifact, completed.stderr)
                self.assertIn("exactly one canonical entry", completed.stderr)

    def test_loader_rejects_duplicate_and_binary_marker_checksum_rows(self) -> None:
        self._require_bash()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loader = self._make_loader_bundle(
                root,
                include_models=True,
                include_lock=True,
                duplicate_checksum="04-models.tar.gz",
                binary_checksum_marker=True,
            )
            completed = self._run_loader(root, loader)
            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("04-models.tar.gz", completed.stderr)
            self.assertIn("exactly one canonical entry", completed.stderr)

    def test_loader_rejects_model_archive_lock_presence_mismatch(self) -> None:
        self._require_bash()
        for name, include_models, include_lock in (
            ("archive without lock", True, False),
            ("lock without archive", False, True),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                loader = self._make_loader_bundle(
                    root,
                    include_models=include_models,
                    include_lock=include_lock,
                )
                completed = self._run_loader(root, loader)
                self.assertNotEqual(completed.returncode, 0, completed.stdout)
                self.assertIn("must either both exist or both be absent", completed.stderr)

    def test_loader_requires_optional_regular_non_symlink_files(self) -> None:
        loader = self._extract_loader()
        self.assertIn(
            'if [ -L "$artifact" ] || [ ! -f "$artifact" ]; then', loader
        )
        self.assertIn(
            "Optional bundle artifact must be a regular non-symlink file", loader
        )

    @unittest.skipUnless(shutil.which("docker"), "Docker CLI is not installed")
    def test_docker_smoke_emits_distinct_ids_and_tamper_fails(self) -> None:
        probe = subprocess.run(
            ["docker", "info"], check=False, capture_output=True, text=True
        )
        if probe.returncode != 0:
            self.skipTest("Docker daemon is not available")
        suffix = uuid.uuid4().hex[:10]
        names = [
            f"anila-model-lock-test-{suffix}-{value}:v1" for value in ("one", "two")
        ]
        try:
            with tempfile.TemporaryDirectory() as directory:
                context = Path(directory)
                for value, name in zip(("one", "two"), names):
                    dockerfile = context / f"Dockerfile.{value}"
                    dockerfile.write_text(
                        f"FROM scratch\nLABEL anila.model.lock={value}\n",
                        encoding="utf-8",
                    )
                    built = subprocess.run(
                        [
                            "docker",
                            "build",
                            "-q",
                            "-t",
                            name,
                            "-f",
                            str(dockerfile),
                            str(context),
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(built.returncode, 0, built.stderr)
                image_ids = [MODULE.inspect_image_id(name) for name in names]
                self.assertNotEqual(image_ids[0], image_ids[1])

                inventory_path = context / "inventory.tsv"
                compose_path = context / "docker-compose.yml"
                lock_path = context / "MODEL-IMAGE-LOCK.tsv"
                inventory_path.write_text(
                    f"one\t{names[0]}\tdefault\ntwo\t{names[1]}\tdefault\n",
                    encoding="utf-8",
                )
                compose_path.write_text(
                    "services:\n"
                    f"  one:\n    image: {names[0]}\n"
                    f"  two:\n    image: {names[1]}\n",
                    encoding="utf-8",
                )
                lock_path.write_text(
                    "# service\timage\timage_id\tactivation\n"
                    f"one\t{names[0]}\t{image_ids[0]}\tdefault\n"
                    f"two\t{names[1]}\t{image_ids[1]}\tdefault\n",
                    encoding="utf-8",
                )
                MODULE.verify_lock(
                    lock_path,
                    compose_path=compose_path,
                    inventory_path=inventory_path,
                    inspect_docker=True,
                )
                tampered = lock_path.read_text(encoding="utf-8").replace(
                    image_ids[0], image_ids[1], 1
                )
                lock_path.write_text(tampered, encoding="utf-8")
                with self.assertRaisesRegex(MODULE.ModelImageLockError, "tag drift"):
                    MODULE.verify_lock(
                        lock_path,
                        compose_path=compose_path,
                        inventory_path=inventory_path,
                        inspect_docker=True,
                    )
        finally:
            subprocess.run(["docker", "image", "rm", "-f", *names], check=False)


if __name__ == "__main__":
    unittest.main()
