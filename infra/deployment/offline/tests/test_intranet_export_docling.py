"""Guards for WITH_DOCLING_IMAGE going through the air-gap export.

These tests do not rebuild the 10GB docling image. They pin the wiring:
default off, platform profile stays ASR-only, and the shipped checksum
predicate (bundle_checksum_files + assert_checksum_matches_declared).
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


TESTS_ROOT = Path(__file__).resolve().parent
REPO_ROOT = TESTS_ROOT.parents[3]
EXPORT_SCRIPT = Path(
    os.environ.get(
        "ANILA_EXPORT_SCRIPT",
        str(REPO_ROOT / "infra/deployment/intranet/build-and-export-for-intranet.sh"),
    )
)
DOCLING_README = REPO_ROOT / "services/docling-service/README.md"
DOCLING_SRC = REPO_ROOT / "services/docling-service"
RUNBOOK = REPO_ROOT / "docs/runbooks/intranet-image-bundle.md"

# Ratchet for MUT-2 (dropping 06-docling-gpu-host.tar from the shipped glob).
# Not used to reimplement the glob — tests execute bundle_checksum_files().
CHECKSUM_GLOB = (
    "01-images/*.tar.gz 04-models.tar.gz 05-weights-*.tar 06-docling-gpu-host.tar"
)


def _script_text() -> str:
    return EXPORT_SCRIPT.read_text(encoding="utf-8")


def extract_bash_function(script_text: str, name: str) -> str:
    """Extract `name()` from the shipped script, quote-aware brace match."""
    match = re.search(rf"^{re.escape(name)}\s*\(\)\s*\{{", script_text, re.M)
    if not match:
        raise AssertionError(f"{name}() not found in export script {EXPORT_SCRIPT}")
    index = match.end()
    depth = 1
    in_single = False
    in_double = False
    escape = False
    while index < len(script_text) and depth:
        char = script_text[index]
        if escape:
            escape = False
            index += 1
            continue
        if in_single:
            if char == "'":
                in_single = False
            index += 1
            continue
        if in_double:
            if char == "\\":
                escape = True
                index += 1
                continue
            if char == '"':
                in_double = False
            index += 1
            continue
        if char == "'":
            in_single = True
        elif char == '"':
            in_double = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    if depth != 0:
        raise AssertionError(f"{name}() brace mismatch in {EXPORT_SCRIPT}")
    return script_text[match.start() : index]


def _shipped_helpers() -> str:
    text = _script_text()
    return "\n".join(
        [
            'die() { echo "✗ $*" >&2; exit 1; }',
            extract_bash_function(text, "bundle_checksum_files"),
            extract_bash_function(text, "bundle_declared_checksum_files"),
            extract_bash_function(text, "assert_checksum_matches_declared"),
        ]
    )


def collect_checksum_files(output_dir: Path) -> list[str]:
    """Execute bundle_checksum_files() copied from the shipped script."""
    script = "\n".join(
        [
            "set -euo pipefail",
            _shipped_helpers(),
            'cd "$1"',
            "bundle_checksum_files",
        ]
    )
    result = subprocess.run(
        ["bash", "-c", script, "checksum", str(output_dir)],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def run_assert(
    output_dir: Path,
    *,
    with_image: str = "0",
    with_weights: str = "0",
    with_models: str = "0",
    with_generic_weights: str = "0",
    weights_list: str = "",
) -> subprocess.CompletedProcess[str]:
    script = "\n".join(
        [
            "set -euo pipefail",
            "WITH_DOCLING_IMAGE=" + with_image,
            "WITH_DOCLING_WEIGHTS=" + with_weights,
            "WITH_MODELS=" + with_models,
            "WITH_WEIGHTS=" + with_generic_weights,
            "WEIGHTS_LIST=" + repr(weights_list),
            _shipped_helpers(),
            'cd "$1"',
            "assert_checksum_matches_declared",
        ]
    )
    return subprocess.run(
        ["bash", "-c", script, "assert", str(output_dir)],
        capture_output=True,
        text=True,
        check=False,
    )


def _touch(path: Path, size: int = 32) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _platform_pointer(root: Path, name: str = "anila-restart-csp.tar.gz") -> None:
    (root / "01-compose-images.files.txt").write_text(
        f"{name}\tanila-restart-csp:latest\n",
        encoding="utf-8",
    )


class IntranetExportDoclingTests(unittest.TestCase):
    def test_export_script_is_executable(self) -> None:
        self.assertTrue(EXPORT_SCRIPT.is_file())
        self.assertTrue(os.access(EXPORT_SCRIPT, os.X_OK) or bool(EXPORT_SCRIPT.stat().st_mode & stat.S_IXUSR))

    def test_default_with_docling_image_is_off(self) -> None:
        self.assertIn('WITH_DOCLING_IMAGE="${WITH_DOCLING_IMAGE:-0}"', _script_text())

    def test_platform_profile_args_stay_asr_only(self) -> None:
        text = _script_text()
        match = re.search(
            r"COMPOSE_PROFILE_ARGS=\(\)\n"
            r'if \[ "\$INCLUDE_ASR" = "1" \]; then\n'
            r"    COMPOSE_PROFILE_ARGS=\(--profile asr\)\n"
            r"fi",
            text,
        )
        self.assertIsNotNone(match, "COMPOSE_PROFILE_ARGS must remain ASR-only")
        self.assertNotIn("docling-local", match.group(0))

    def test_docling_bake_does_not_join_platform_image_list(self) -> None:
        text = _script_text()
        self.assertIn("not added to platform image list", text)
        self.assertIn("BUILT_IMAGES+=(\"$DOCLING_IMAGE\")", text)
        self.assertNotRegex(text, r'(?<![A-Z_])IMAGES\+=\("\$DOCLING_IMAGE"\)')
        self.assertIn("buildx_export_one \"$DOCLING_IMAGE\"", text)
        self.assertIn("06-docling-image.files.txt", text)

    def test_checksum_glob_is_single_source_in_script(self) -> None:
        body = extract_bash_function(_script_text(), "bundle_checksum_files")
        self.assertIn(f"for f in {CHECKSUM_GLOB}; do", body)
        extract_bash_function(_script_text(), "assert_checksum_matches_declared")
        extract_bash_function(_script_text(), "bundle_declared_checksum_files")

    def test_checksum_combo_default_images_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch(root / "01-images" / "anila-restart-csp.tar.gz")
            self.assertEqual(
                collect_checksum_files(root),
                ["01-images/anila-restart-csp.tar.gz"],
            )

    def test_checksum_combo_weights_without_docling_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch(root / "01-images" / "anila-restart-csp.tar.gz")
            _touch(root / "05-weights-docling.tar")
            self.assertEqual(
                collect_checksum_files(root),
                [
                    "01-images/anila-restart-csp.tar.gz",
                    "05-weights-docling.tar",
                ],
            )

    def test_checksum_combo_models_without_docling_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch(root / "01-images" / "anila-restart-csp.tar.gz")
            _touch(root / "04-models.tar.gz")
            self.assertEqual(
                collect_checksum_files(root),
                [
                    "01-images/anila-restart-csp.tar.gz",
                    "04-models.tar.gz",
                ],
            )

    def test_checksum_combo_docling_image_and_gpu_host_and_weights(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch(root / "01-images" / "anila-restart-csp.tar.gz")
            _touch(root / "01-images" / "anila_docling-service_0.1.0.tar.gz")
            _touch(root / "05-weights-docling.tar")
            _touch(root / "06-docling-gpu-host.tar")
            self.assertEqual(
                sorted(collect_checksum_files(root)),
                [
                    "01-images/anila-restart-csp.tar.gz",
                    "01-images/anila_docling-service_0.1.0.tar.gz",
                    "05-weights-docling.tar",
                    "06-docling-gpu-host.tar",
                ],
            )

    def test_missing_optional_tars_are_not_checksummed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch(root / "01-images" / "anila-restart-csp.tar.gz")
            self.assertNotIn("04-models.tar.gz", collect_checksum_files(root))
            self.assertNotIn("06-docling-gpu-host.tar", collect_checksum_files(root))
            self.assertNotIn("05-weights-docling.tar", collect_checksum_files(root))

    def test_image_flag_without_weights_dir_fails_loud(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env = os.environ.copy()
            env["WITH_DOCLING_IMAGE"] = "1"
            env.pop("DOCLING_WEIGHTS_DIR", None)
            result = subprocess.run(
                ["bash", str(EXPORT_SCRIPT), temporary],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(REPO_ROOT),
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DOCLING_WEIGHTS_DIR", result.stderr)
        self.assertIn("four-piece", result.stderr)

    def test_gpu_host_four_piece_sources_exist(self) -> None:
        for name in (
            "docker-compose.standalone.yml",
            ".env.example",
            "README.md",
        ):
            self.assertTrue((DOCLING_SRC / name).is_file(), name)
        self.assertTrue((DOCLING_SRC / "Dockerfile").is_file(), "Dockerfile (bake source, not in four-piece tar)")

    def test_readme_drops_asr_decoder_packaging_analogy(self) -> None:
        text = DOCLING_README.read_text(encoding="utf-8")
        self.assertNotIn("同 asr-decoder", text)
        self.assertNotIn("asr-decoder 那個類比", text)
        self.assertNotRegex(text, r"asr-decoder.*打包")
        self.assertNotRegex(text, r"INCLUDE_ASR")

    def test_readme_enablement_gate_is_all_conditions(self) -> None:
        text = DOCLING_README.read_text(encoding="utf-8")
        self.assertIn("以下條件**全部**滿足", text)
        self.assertIn("才可把平台側 `DOC_PARSER` 設成", text)
        self.assertIn("WITH_DOCLING_IMAGE=1", text)
        self.assertIn("docker-compose.standalone.yml", text)
        self.assertIn("複製**（不要移動）", text)
        self.assertIn("不要**加 `--profile docling-local`", text)

    def test_runbook_documents_default_off_and_operator_up(self) -> None:
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn("WITH_DOCLING_IMAGE=0", text)
        self.assertIn("不要加 --profile docling-local", text)
        self.assertIn(
            "-f compose.yaml -f intranet-image-overrides.yml --profile asr",
            text,
        )

    def test_intranet_load_skips_docling_images_txt_on_platform(self) -> None:
        text = _script_text()
        self.assertIn("06-docling-image.images.txt", text)
        self.assertIn("GPU-host image; not loaded on this platform host", text)

    def test_does_not_delete_operator_leftovers(self) -> None:
        text = _script_text()
        self.assertNotIn("strip_stale_optional_payload", text)
        self.assertNotIn('rm -f "$IMG_DIR"/*docling*', text)
        self.assertNotIn('rm -f "$OUTPUT_DIR/05-weights-docling.tar"', text)
        self.assertNotIn('rm -f "$OUTPUT_DIR/06-docling-gpu-host.tar"', text)

    def test_skip_build_dies_when_docling_tar_missing(self) -> None:
        text = _script_text()
        self.assertIn(
            'die "SKIP_BUILD=1 但 01-images/ 缺少 docling image tar:',
            text,
        )
        self.assertNotRegex(
            text,
            r'if \[ "\$SKIP_BUILD" = "1" \] && \[ -f "\$docling_out" \]',
        )

    def test_manifest_and_loader_provenance_use_flags_not_file_existence(self) -> None:
        text = _script_text()
        self.assertIn('if [ "$WITH_DOCLING_IMAGE" = "1" ]; then', text)
        self.assertIn(
            "GPU-host four-piece set (WITH_DOCLING_IMAGE=$WITH_DOCLING_IMAGE)",
            text,
        )
        self.assertNotIn(
            'if [ -f "$OUTPUT_DIR/06-docling-gpu-host.tar" ]; then',
            text,
        )
        self.assertNotIn(
            "if [ -f 06-docling-image.files.txt ] || [ -f 06-docling-gpu-host.tar ]; then",
            text,
        )

    def test_readme_does_not_hardcode_image_tar_filename(self) -> None:
        text = DOCLING_README.read_text(encoding="utf-8")
        self.assertNotIn("anila_docling-service_0.1.0.tar.gz", text)
        self.assertIn("06-docling-image.files.txt", text)

    def test_readme_gpu_host_steps_source_env_before_weights_extract(self) -> None:
        text = DOCLING_README.read_text(encoding="utf-8")
        unpack = text.find("tar -xf 06-docling-gpu-host.tar")
        source = text.find(". ./.env")
        weights = text.find('tar -xf 05-weights-docling.tar -C "$DOCLING_MODEL_HOST_DIR"')
        self.assertNotEqual(unpack, -1)
        self.assertNotEqual(source, -1)
        self.assertNotEqual(weights, -1)
        self.assertLess(unpack, source, "unpack four-piece before sourcing .env")
        self.assertLess(source, weights, "source .env before using DOCLING_MODEL_HOST_DIR")

    def test_docling_out_assignment_is_inside_checksum_glob(self) -> None:
        """MUT-1: moving docling_out off IMG_DIR must go red."""
        text = _script_text()
        match = re.search(r'^\s*docling_out="([^"]+)"', text, re.M)
        self.assertIsNotNone(match, "docling_out assignment missing from shipped script")
        assignment = match.group(0)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "01-images").mkdir()
            script = "\n".join(
                [
                    "set -euo pipefail",
                    "OUTPUT_DIR=" + repr(str(root)),
                    'IMG_DIR="$OUTPUT_DIR/01-images"',
                    'docling_safe="anila_docling-service_0.1.0"',
                    assignment,
                    'mkdir -p "$(dirname "$docling_out")"',
                    'printf "x" > "$docling_out"',
                    'printf "%s\\n" "$docling_out"',
                    _shipped_helpers(),
                    'cd "$OUTPUT_DIR"',
                    "bundle_checksum_files",
                ]
            )
            result = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                check=True,
            )
        lines = [line for line in result.stdout.splitlines() if line]
        self.assertGreaterEqual(len(lines), 2, result.stdout)
        written = Path(lines[0])
        listed = lines[1:]
        rel = written.relative_to(root).as_posix()
        self.assertIn(
            rel,
            listed,
            f"docling_out={written} is not picked up by shipped bundle_checksum_files(): {listed}",
        )

    def test_assert_clean_default_off_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch(root / "01-images" / "anila-restart-csp.tar.gz")
            _platform_pointer(root)
            result = run_assert(root, with_image="0", with_weights="0")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_assert_orphan_docling_tar_fails_loud(self) -> None:
        """Orphan tar in 01-images/ with IMAGE=0 (no 06-docling-* pointers)."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch(root / "01-images" / "anila-restart-csp.tar.gz")
            _touch(root / "01-images" / "anila_docling-service_0.1.0.tar.gz")
            _platform_pointer(root)
            result = run_assert(root, with_image="0", with_weights="0")
            leftover = root / "01-images" / "anila_docling-service_0.1.0.tar.gz"
            self.assertTrue(leftover.is_file())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("anila_docling-service_0.1.0.tar.gz", result.stderr)
        self.assertIn("did not declare", result.stderr)
        self.assertIn("will not delete", result.stderr)

    def test_assert_stale_docling_payload_fails_loud(self) -> None:
        """Dirty OUTPUT_DIR: leftover 06 tar + weights + image tar, flags off."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch(root / "01-images" / "anila-restart-csp.tar.gz")
            _touch(root / "01-images" / "anila_docling-service_0.1.0.tar.gz")
            _touch(root / "05-weights-docling.tar")
            _touch(root / "06-docling-gpu-host.tar")
            _platform_pointer(root)
            result = run_assert(root, with_image="0", with_weights="0")
            self.assertTrue((root / "06-docling-gpu-host.tar").is_file())
            self.assertTrue((root / "05-weights-docling.tar").is_file())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("06-docling-gpu-host.tar", result.stderr)
        self.assertIn("05-weights-docling.tar", result.stderr)
        self.assertIn("anila_docling-service_0.1.0.tar.gz", result.stderr)

    def test_assert_stale_weights_without_image_fails_loud(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch(root / "01-images" / "anila-restart-csp.tar.gz")
            _touch(root / "05-weights-docling.tar")
            _platform_pointer(root)
            result = run_assert(root, with_image="0", with_weights="0")
            self.assertTrue((root / "05-weights-docling.tar").is_file())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("05-weights-docling.tar", result.stderr)

    def test_assert_declared_missing_from_glob_fails_loud(self) -> None:
        """IMAGE=1 declared 06 tar, glob does not see it → red (MUT-3 class)."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch(root / "01-images" / "anila-restart-csp.tar.gz")
            _touch(root / "01-images" / "anila_docling-service_0.1.0.tar.gz")
            _touch(root / "05-weights-docling.tar")
            (root / "06-docling-image.files.txt").write_text(
                "anila_docling-service_0.1.0.tar.gz\tanila/docling-service:0.1.0\n",
                encoding="utf-8",
            )
            _platform_pointer(root)
            result = run_assert(root, with_image="1", with_weights="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("06-docling-gpu-host.tar", result.stderr)
        self.assertIn("did not pick up", result.stderr)

    def test_assert_image_on_with_full_set_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch(root / "01-images" / "anila-restart-csp.tar.gz")
            _touch(root / "01-images" / "anila_docling-service_0.1.0.tar.gz")
            _touch(root / "05-weights-docling.tar")
            _touch(root / "06-docling-gpu-host.tar")
            (root / "06-docling-image.files.txt").write_text(
                "anila_docling-service_0.1.0.tar.gz\tanila/docling-service:0.1.0\n",
                encoding="utf-8",
            )
            _platform_pointer(root)
            result = run_assert(root, with_image="1", with_weights="1")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pack_docling_gpu_host_tar_readme_matches_repo(self) -> None:
        """MUT-3: replacing tar -cf with true must go red (empty-tar die)."""
        text = _script_text()
        pack = extract_bash_function(text, "pack_docling_gpu_host_tar")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch(root / "05-weights-docling.tar")
            (root / "06-docling-image.files.txt").write_text(
                "anila_docling-service_0.1.0.tar.gz\tanila/docling-service:0.1.0\n",
                encoding="utf-8",
            )
            script = "\n".join(
                [
                    "set -euo pipefail",
                    'die() { echo "✗ $*" >&2; exit 1; }',
                    "REPO_ROOT=" + repr(str(REPO_ROOT)),
                    "OUTPUT_DIR=" + repr(str(root)),
                    'DOCLING_IMAGE="anila/docling-service:0.1.0"',
                    pack,
                    "pack_docling_gpu_host_tar",
                ]
            )
            result = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            tar_path = root / "06-docling-gpu-host.tar"
            self.assertTrue(tar_path.is_file())
            self.assertGreater(tar_path.stat().st_size, 0)
            self.assertFalse((root / "06-docling-gpu-host").exists())
            extracted = subprocess.run(
                ["tar", "-xOf", str(tar_path), "README.md"],
                capture_output=True,
                check=True,
            )
            self.assertEqual(
                extracted.stdout,
                (DOCLING_SRC / "README.md").read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
