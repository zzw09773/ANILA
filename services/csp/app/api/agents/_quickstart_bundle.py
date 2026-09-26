"""Assembly of the two agent download bundles (design 2026-09-22 §6).

Two independent artifacts, both plain allow-list ZIPs:

1. quickstart — ``packages/anila-agent-quickstart/`` + the vendored canonical
   ``anila_verify.py`` + generated ``deployment.env`` / ``ca.pem`` /
   ``bundle.json``. No wheelhouse: dependencies are installed into the
   MLSteam lab image (design §12). The zip root is ``anila-agent-quickstart/``.
2. advanced example — ``packages/anila-agent/`` mirrored as-is, zip root
   ``anila-agent-advanced-example/``, import package still ``anila_agent``.

Everything here is **fail-closed**: a missing scaffold file, an unset site
profile, a lock that is not a complete hash lock, a missing lab image
version, or an escaping symlink raises ``BundleError`` (HTTP 503). A
half-written zip is never shipped, and the advanced example is never
silently substituted for a broken quickstart.

Only stdlib + ``cryptography`` are used, so this module stays importable in
the csp image without new dependencies. The site profile carries non-secret
site facts only (``csp_base_url``, optional ``csp_ca_file``) — never the
request ``Host``, and never a credential. The platform does not choose the
model. ``LLM_BASE_URL`` is ``<csp_base_url>/v1`` so usage and audit stay on
CSP; the developer fills ``LLM_MODEL`` and exports their own ``sk-`` key in
the lab. That key is never written into the zip (design §1, §6).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

# ── Env knobs ────────────────────────────────────────────────────────────────
# Read per request (not frozen at import) so a test — and a recreate-and-restart
# in production — can point them at another tree without re-importing csp.
ENV_QUICKSTART_DIR = "ANILA_QUICKSTART_DIR"
ENV_PROFILE_PATH = "ANILA_QUICKSTART_PROFILE"
ENV_TMP_DIR = "ANILA_QUICKSTART_TMPDIR"
ENV_SCAFFOLD_VERSION = "ANILA_QUICKSTART_VERSION"
ENV_TARGET_ABI = "ANILA_QUICKSTART_TARGET_ABI"
ENV_SOURCE_REVISION = "ANILA_QUICKSTART_SOURCE_REVISION"
ENV_ADVANCED_DIR = "ANILA_ADVANCED_EXAMPLE_DIR"

DEFAULT_QUICKSTART_DIR = "/app/anila-quickstart"
DEFAULT_PROFILE_PATH = "/app/anila-quickstart-profile.json"
DEFAULT_ADVANCED_DIR = "/app/anila-template"
DEFAULT_SCAFFOLD_VERSION = "1.0.0"
DEFAULT_TARGET_ABI = "py313-linux-x86_64"

# The scaffold release contract: these files, all of them required before the
# bundle may claim to be directly deployable.
QUICKSTART_FILES: tuple[str, ...] = (
    "agent.py",
    "server.py",
    "platform_io.py",
    "llm.py",
    "requirements.in",
    "requirements.lock",
    "Dockerfile",
    "compose.yaml",
    ".gitignore",
    "README.md",
    "LICENSE",
    "run.sh",
    "IMAGE_VERSION",
)
VERIFIER_FILENAME = "anila_verify.py"
ENV_FILENAME = "deployment.env"
CA_FILENAME = "ca.pem"
MANIFEST_FILENAME = "bundle.json"
IMAGE_VERSION_FILENAME = "IMAGE_VERSION"

QUICKSTART_ROOT = "anila-agent-quickstart"
ADVANCED_ROOT = "anila-agent-advanced-example"

# Advanced example: exclude local secrets, private keys and machine artifacts.
# ``.env.example`` must survive — it is the documented config surface.
_ADVANCED_EXCLUDED_NAMES = {".env", ".env.local"}
_ADVANCED_EXCLUDED_PARTS = {
    ".git",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "wheelhouse",
}
_ADVANCED_EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".pem", ".key", ".crt", ".log", ".zip"}

_AGENT_NAME_CONST = "AGENT_NAME"
_COLLECTION_ID_CONST = "COLLECTION_ID"
_AGENT_ID_ENV = "ANILA_AGENT_ID"

_SHA256_RE = re.compile(r"--hash=sha256:([0-9a-f]{64})")
_REQUIREMENT_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._+-]*)\s*(?:\[[^\]]*\])?\s*(?:==\s*([^\s;,]+))?"
)
_PYPROJECT_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
_IMAGE_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")


class BundleError(Exception):
    """Assembly failure → HTTP 503. Never degraded into a partial zip."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class SiteProfile:
    """Non-secret site facts. The platform does not choose a model.

    ``models`` and ``default_model_key`` are accepted and ignored so an older
    profile file still loads. The developer picks ``LLM_MODEL`` and a CSP
    ``sk-`` key after download.
    """

    csp_base_url: str
    csp_ca_file: Path | None
    extra_keys: tuple[str, ...] = ()

    @property
    def llm_base_url(self) -> str:
        """CSP's own OpenAI-compatible prefix. Callers append ``chat/completions``."""
        return f"{self.csp_base_url.rstrip('/')}/v1"


@dataclass
class BuiltBundle:
    """A temp-file zip ready to be streamed, plus its public manifest."""

    path: Path
    filename: str
    manifest: dict
    bound: bool


@dataclass
class _AgentInputs:
    agent_id: int | None = None
    agent_name: str | None = None
    bound_collection_ids: list[int] = field(default_factory=list)
    requested_collection_id: int | None = None


# ── knob resolution ──────────────────────────────────────────────────────────


def _env(name: str) -> str | None:
    value = (os.environ.get(name) or "").strip()
    return value or None


def quickstart_source_dir() -> Path:
    return Path(_env(ENV_QUICKSTART_DIR) or DEFAULT_QUICKSTART_DIR)


def advanced_source_dir() -> Path:
    return Path(_env(ENV_ADVANCED_DIR) or DEFAULT_ADVANCED_DIR)


def profile_path() -> Path:
    return Path(_env(ENV_PROFILE_PATH) or DEFAULT_PROFILE_PATH)


def scaffold_version() -> str:
    return _env(ENV_SCAFFOLD_VERSION) or DEFAULT_SCAFFOLD_VERSION


def target_abi() -> str:
    return _env(ENV_TARGET_ABI) or DEFAULT_TARGET_ABI


def source_revision() -> str | None:
    """Recorded scaffold source revision, or None — never invented."""
    return _env(ENV_SOURCE_REVISION)


# ── small helpers ────────────────────────────────────────────────────────────


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _checked_file(root: Path, relative: str) -> Path:
    """Resolve ``root/relative``, refusing symlinks that escape ``root``."""
    root_resolved = root.resolve()
    candidate = root / relative
    if not candidate.exists() and not candidate.is_symlink():
        raise BundleError(f"缺少必要檔案：{root}/{relative}")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise BundleError(
            f"{relative} 是指向 root 之外的 symlink（→ {resolved}）；"
            "打包內容會與路徑名不符"
        )
    if not resolved.is_file():
        raise BundleError(f"必要檔案不是一般檔案：{root}/{relative}")
    return resolved


def _split_trailing_comment(rest: str) -> tuple[str, str]:
    """Split ``rest`` into (value, comment) at the first unquoted ``#``."""
    quote: str | None = None
    index = 0
    while index < len(rest):
        char = rest[index]
        if quote is not None:
            if char == "\\" and quote == '"':
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#":
            return rest[:index], rest[index:]
        index += 1
    return rest, ""


def _rewrite_constant(text: str, name: str, literal: str) -> str:
    """Replace one module-level constant's value, keeping annotation + comment.

    Anchored at line start and required to match exactly once: a scaffold whose
    ``agent.py`` drifted must fail rather than be half-prefilled. A ``#`` inside
    a string literal is not a comment, hence the quote-aware split.
    """
    pattern = re.compile(rf"^{re.escape(name)}(?P<ann>\s*:[^=\n]*?)?\s*=")
    lines = text.splitlines()
    hits = [i for i, line in enumerate(lines) if pattern.match(line)]
    if len(hits) != 1:
        raise BundleError(
            f"骨架 agent.py 找不到（或重複）{name} 指派行（找到 {len(hits)} 次）；"
            "下載器無法安全預填"
        )
    index = hits[0]
    match = pattern.match(lines[index])
    assert match is not None
    head_end = match.end()
    value, comment = _split_trailing_comment(lines[index][head_end:])
    lead = value[: len(value) - len(value.lstrip())]
    gap = value[len(value.rstrip()) :]
    lines[index] = f"{lines[index][:head_end]}{lead}{literal}{gap}{comment}"
    body = "\n".join(lines)
    return body + "\n" if text.endswith("\n") else body


def _env_value(value: str) -> str:
    """Quote an env-file value only when it needs it."""
    if value and not re.search(r"[\s\"'#$`\\]", value):
        return value
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "$$")
        .replace("`", "\\`")
    )
    return f'"{escaped}"'


# ── profile + CA ─────────────────────────────────────────────────────────────


def _require_https_origin(raw: object, field_name: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise BundleError(f"profile 的 {field_name} 必須是非空字串（agent 可達的 https origin）")
    value = raw.strip()
    parts = urlsplit(value)
    # Reject before any error text echoes the raw URL. userinfo would otherwise
    # land in CSP_BASE_URL and LLM_BASE_URL, and in the 503 detail.
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise BundleError(
            f"profile 的 {field_name} 不可帶帳號或密碼；"
            "origin 只填 https 主機，憑證不進下載包"
        )
    if parts.scheme != "https":
        raise BundleError(f"profile 的 {field_name} 必須是 https origin（目前 {value!r}）")
    if not parts.netloc or parts.query or parts.fragment:
        raise BundleError(f"profile 的 {field_name} 不是合法 origin：{value!r}")
    if parts.path not in ("", "/"):
        raise BundleError(
            f"profile 的 {field_name} 不應帶路徑（目前 {value!r}）；"
            "請填 origin，由骨架自己接 /api 與 /.well-known"
        )
    return f"{parts.scheme}://{parts.netloc}"


def _read_error(path: Path, exc: OSError) -> BundleError:
    reason = exc.strerror or str(exc)
    return BundleError(f"讀不到發行檔 {path}：{reason}")


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise _read_error(path, exc) from exc


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _read_error(path, exc) from exc


def load_site_profile(path: Path | None = None) -> SiteProfile:
    """Load the operations-provided site profile (fail-closed on site facts).

    Required: ``csp_base_url``. Optional: ``csp_ca_file``. A per-model section
    or ``default_model_key`` is ignored when present so older files still
    download. A missing file still fails with a clear 503 — the origin is not
    guessed from the request.
    """
    target = path or profile_path()
    if not target.is_file():
        raise BundleError(
            f"站台接入 profile 不存在：{target}；請維運提供非祕密 profile 並掛載"
            f"（或設 {ENV_PROFILE_PATH}）。缺少時不發『可直接啟動』包"
        )
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"無法解析站台接入 profile {target}：{exc}") from exc
    if not isinstance(raw, dict):
        raise BundleError(f"站台接入 profile {target} 必須是 JSON 物件")

    csp_base_url = _require_https_origin(raw.get("csp_base_url"), "csp_base_url")

    csp_ca_file: Path | None = None
    raw_ca = raw.get("csp_ca_file")
    if raw_ca is not None:
        if not isinstance(raw_ca, str) or not raw_ca.strip():
            raise BundleError("profile 的 csp_ca_file 必須是非空字串，或整個省略")
        csp_ca_file = Path(raw_ca.strip())

    # Legacy keys. Ignored on purpose: the developer chooses the model.
    known = {"csp_base_url", "csp_ca_file", "default_model_key", "models"}
    return SiteProfile(
        csp_base_url=csp_base_url,
        csp_ca_file=csp_ca_file,
        extra_keys=tuple(sorted(set(raw) - known)),
    )


def read_trust_chain(
    ca_file: Path | None, fallback: Path | None = None
) -> tuple[bytes, list[str], str]:
    """Return ``(pem_bytes, sha256_fingerprints, source)``, validating the bytes.

    A path that merely exists is not evidence that it holds a usable chain, and
    a private key must not be smuggled into ``ca.pem`` — so parse, don't assume.
    """
    source = "profile.csp_ca_file"
    target: Path | None = ca_file
    if target is None or not target.is_file():
        if fallback is not None and Path(fallback).is_file():
            target = Path(fallback)
            source = "platform_bundle_default"
        else:
            raise BundleError(
                f"取不到 CSP 公開 CA（profile.csp_ca_file={ca_file}，"
                f"fallback={fallback}）；請維運提供能驗證本 CSP origin 的公開鏈，"
                "不可只憑路徑存在就放行"
            )
    payload = _read_bytes(target)
    if b"PRIVATE KEY" in payload:
        raise BundleError(f"{target} 含私鑰，不能當公開 ca.pem（source={source}）")
    try:
        certs = x509.load_pem_x509_certificates(payload)
    except ValueError as exc:
        raise BundleError(f"{target} 不是可解析的 PEM 憑證鏈（source={source}）：{exc}") from exc
    if not certs:
        raise BundleError(f"{target} 沒有任何 PEM 憑證（source={source}）")
    fingerprints = [
        "sha256:" + hashlib.sha256(cert.public_bytes(Encoding.DER)).hexdigest()
        for cert in certs
    ]
    return payload, fingerprints, source


# ── lock verification (wheels live in the lab image, not the zip) ───────────


def _parse_requirements(text: str) -> tuple[dict[str, dict], list[str]]:
    """Parse pip-compile style text into ``{canonical_name: {version, hashes}}``.

    Backslash continuations are joined first. Lines that parse but carry no
    ``==`` pin are reported separately so the manifest shows the lock was only
    partly understood instead of pretending full knowledge.
    """
    logical = text.replace("\\\r\n", " ").replace("\\\n", " ")
    requirements: dict[str, dict] = {}
    unparsed: list[str] = []
    for line in logical.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            continue
        header = stripped.split(" --hash", 1)[0].split(";", 1)[0].strip()
        if not header:
            continue
        match = _REQUIREMENT_RE.match(header)
        if match is None or match.group(2) is None:
            unparsed.append(stripped)
            continue
        requirements[_canonical_name(match.group(1))] = {
            "version": match.group(2),
            "hashes": set(_SHA256_RE.findall(stripped)),
        }
    return requirements, unparsed


def validate_hashed_lock(lock_text: str, lock_sha256: str) -> dict:
    """Require a complete hash lock. Wheels are not part of the download.

    Every requirement must be an exact ``==`` pin with at least one sha256.
    Option lines, VCS URLs and direct download URLs are refused: those cannot
    be checked offline when the lab image is built.
    """
    logical = lock_text.replace("\\\r\n", " ").replace("\\\n", " ")
    for line in logical.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("-") or "://" in stripped or stripped.startswith("git+"):
            raise BundleError("requirements.lock 含有不允許的選項或下載 URL：" + stripped)

    requirements, unparsed = _parse_requirements(lock_text)
    if not requirements:
        raise BundleError("requirements.lock 沒有解析出任何 == 釘版本需求")
    if unparsed:
        raise BundleError("requirements.lock 含有無法驗證的需求：" + "、".join(unparsed))
    unhashed = [name for name, spec in requirements.items() if not spec["hashes"]]
    if unhashed:
        raise BundleError("requirements.lock 缺少 wheel hash：" + "、".join(sorted(unhashed)))
    return {
        "requirements_lock_sha256": lock_sha256,
        "lock_requirements": len(requirements),
        "lock_requirements_with_hash": len(requirements),
        "lock_unparsed_lines": unparsed,
    }


def read_lab_image_version(root: Path) -> str:
    """The scaffold's image version, recorded in bundle.json for run.sh."""
    path = _checked_file(root, IMAGE_VERSION_FILENAME)
    text = _read_text(path).strip()
    if not text or any(ch.isspace() for ch in text) or _IMAGE_VERSION_RE.fullmatch(text) is None:
        raise BundleError(
            "IMAGE_VERSION 必須是單一版本號（這個 zip 相容的 lab 映像版本）；"
            "空的、含空白或含路徑字元不能發行"
        )
    return text


def _parse_requirements_in(text: str) -> tuple[list[tuple[str, str | None]], list[str]]:
    """Parse ``requirements.in``: allow bare names *and* ``==`` pins.

    The scaffold's ``.in`` is "direct dependencies + necessary security pins",
    so a bare name is legitimate and still creates an obligation — the lock
    must contain that distribution. Returns ``(requirements, unparsed)``.
    """
    requirements: list[tuple[str, str | None]] = []
    unparsed: list[str] = []
    for line in text.replace("\\\r\n", " ").replace("\\\n", " ").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            continue
        header = stripped.split(" --hash", 1)[0].split(";", 1)[0].strip()
        if not header:
            continue
        match = _REQUIREMENT_RE.match(header)
        if match is None:
            unparsed.append(stripped)
            continue
        requirements.append((_canonical_name(match.group(1)), match.group(2)))
    return requirements, unparsed


def validate_lock_covers_requirements_in(lock_text: str, requirements_in: str) -> dict:
    """``requirements.in`` 的每個直接相依都必須被 lock 釘住（fail-closed）。

    A pinned ``.in`` entry must match the lock exactly; a bare name must at
    least be present in the lock. Either way the obligation is checked, so a
    lock that quietly dropped a direct dependency cannot ship.
    """
    lock, _ = _parse_requirements(lock_text)
    direct, unparsed = _parse_requirements_in(requirements_in)
    if not direct and not unparsed:
        raise BundleError("requirements.in 沒有解析出任何直接相依")

    violations: list[str] = []
    for name, version in direct:
        pinned = lock.get(name)
        if pinned is None:
            violations.append(f"{name}（lock 缺此套件）")
        elif version is not None and pinned["version"] != version:
            violations.append(
                f"{name}（.in 要 {version}，lock 有 {pinned['version']}）"
            )
    for line in unparsed:
        violations.append(f"{line!r}（無法解析為需求行）")
    if violations:
        raise BundleError(
            "requirements.in 與 requirements.lock 不一致：" + "、".join(violations)
        )
    return {
        "requirements_in_count": len(direct),
        "requirements_in_pinned": sum(1 for _, v in direct if v is not None),
    }


# ── zip writing ──────────────────────────────────────────────────────────────


def _make_temp_zip() -> Path:
    tmpdir = _env(ENV_TMP_DIR)
    if tmpdir:
        Path(tmpdir).mkdir(parents=True, exist_ok=True)
    handle, raw_path = tempfile.mkstemp(prefix="anila-bundle-", suffix=".zip", dir=tmpdir)
    os.close(handle)
    return Path(raw_path)


def _write_bytes(zf: zipfile.ZipFile, arcname: str, payload: bytes) -> None:
    info = zipfile.ZipInfo(arcname)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zf.writestr(info, payload)


def _write_file(zf: zipfile.ZipFile, src: Path, arcname: str, *, store: bool) -> None:
    zf.write(src, arcname, compress_type=zipfile.ZIP_STORED if store else zipfile.ZIP_DEFLATED)


# ── deployment.env ───────────────────────────────────────────────────────────


def _resolve_collection_id(inputs: _AgentInputs) -> tuple[int | None, bool]:
    """Choose the single pre-filled collection id, refusing an out-of-set pick."""
    bound = sorted({int(c) for c in inputs.bound_collection_ids})
    requested = inputs.requested_collection_id
    if requested is None:
        # Exactly one binding → prefill it; zero or several → leave None.
        return (bound[0], True) if len(bound) == 1 else (None, False)
    if not bound:
        raise BundleError(f"agent 未綁定任何知識庫，卻指定 collection_id={requested}")
    if requested not in bound:
        raise BundleError(
            f"collection_id={requested} 不在 agent 已綁定的集合 {bound} 內"
        )
    return requested, True


_AGENT_ID_COMMENT = (
    "# 註冊後把 Console 上的數字 agent id 填在下一行，再執行 ./run.sh restart"
)
_MODEL_COMMENT = (
    "# 此 agent 實際使用的模型。註冊後下載會預填；更換需重新送審。"
    "API 金鑰不要寫進此檔。上線用量算提問者；金鑰只在 lab 沒有派工 JWT 時測試（見 README）"
)


def render_deployment_env(
    profile: SiteProfile,
    agent_id: int | None,
    llm_model: str | None = None,
) -> bytes:
    """The six non-secret keys, in a fixed order.

    ``ANILA_AGENT_ID`` 與 ``LLM_MODEL`` 只在綁定某個已註冊 agent 時預填。
    通用包兩個都留空。``LLM_API_KEY`` 不進這個檔。
    """
    lines = [
        f"CSP_BASE_URL={_env_value(profile.csp_base_url)}",
        f"ANILA_CA_FILE=/app/{CA_FILENAME}",
    ]
    if agent_id is None:
        lines.append(_AGENT_ID_COMMENT)
        lines.append(f"{_AGENT_ID_ENV}=")
    else:
        lines.append(f"{_AGENT_ID_ENV}={agent_id}")
    lines.extend(
        [
            f"LLM_BASE_URL={_env_value(profile.llm_base_url)}",
            _MODEL_COMMENT,
            (
                f"LLM_MODEL={_env_value(llm_model.strip())}"
                if llm_model and llm_model.strip()
                else "LLM_MODEL="
            ),
            "LLM_AUTH_REQUIRED=true",
        ]
    )
    return ("\n".join(lines) + "\n").encode("utf-8")


# ── public entry points ──────────────────────────────────────────────────────


def build_quickstart_bundle(
    *,
    agent_id: int | None,
    agent_name: str | None,
    base_model_id: int | None = None,
    base_model_name: str | None = None,
    bound_collection_ids: list[int],
    requested_collection_id: int | None,
    verifier_source: Path,
    platform_ca_default: Path | None,
    profile_file: Path | None = None,
    source_dir: Path | None = None,
) -> BuiltBundle:
    """Assemble the quickstart zip on a temp file. Raises ``BundleError``.

    已綁定 agent 時，``base_model_name`` 寫進 deployment.env 的 LLM_MODEL。
    通用包不預填模型。``base_model_id`` 只為舊呼叫端保留。
    """
    del base_model_id
    inputs = _AgentInputs(
        agent_id=agent_id,
        agent_name=agent_name,
        bound_collection_ids=list(bound_collection_ids),
        requested_collection_id=requested_collection_id,
    )
    root = source_dir or quickstart_source_dir()
    try:
        root_ok = root.is_dir()
    except OSError as exc:
        raise _read_error(root, exc) from exc
    if not root_ok:
        raise BundleError(
            f"找不到快速起步骨架目錄：{root}；請維運提供骨架（或設 {ENV_QUICKSTART_DIR}）"
        )
    try:
        missing = [name for name in QUICKSTART_FILES if not (root / name).exists()]
    except OSError as exc:
        raise _read_error(root, exc) from exc
    if missing:
        raise BundleError(
            f"骨架目錄 {root} 缺少必要檔案：{'、'.join(missing)}；不做半成品打包"
        )
    try:
        sources = {name: _checked_file(root, name) for name in QUICKSTART_FILES}
    except OSError as exc:
        raise _read_error(Path(getattr(exc, "filename", None) or root), exc) from exc

    verifier_bytes = _read_verifier(verifier_source)
    profile = load_site_profile(profile_file)
    ca_bytes, ca_fingerprints, ca_source = read_trust_chain(
        profile.csp_ca_file, platform_ca_default
    )

    lock_text = _read_text(sources["requirements.lock"])
    lock_sha256 = _sha256_bytes(lock_text.encode("utf-8"))
    in_report = validate_lock_covers_requirements_in(
        lock_text, _read_text(sources["requirements.in"])
    )
    abi = target_abi()
    lock_report = validate_hashed_lock(lock_text, lock_sha256)
    lock_report.update(in_report)
    image_version = read_lab_image_version(root)

    collection_id, collection_prefilled = _resolve_collection_id(inputs)
    agent_source = _read_text(sources["agent.py"])
    if inputs.agent_name is not None:
        agent_source = _rewrite_constant(
            agent_source,
            _AGENT_NAME_CONST,
            json.dumps(inputs.agent_name, ensure_ascii=False),
        )
    agent_source = _rewrite_constant(
        agent_source,
        _COLLECTION_ID_CONST,
        "None" if collection_id is None else str(collection_id),
    )

    version = scaffold_version()
    registered_model = None
    if inputs.agent_id is not None and base_model_name and base_model_name.strip():
        registered_model = base_model_name.strip()
    deployment_env = render_deployment_env(profile, inputs.agent_id, registered_model)

    tmp_path = _make_temp_zip()
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            files: dict[str, str] = {}
            for name in QUICKSTART_FILES:
                arcname = f"{QUICKSTART_ROOT}/{name}"
                if name == "agent.py":
                    payload = agent_source.encode("utf-8")
                    _write_bytes(zf, arcname, payload)
                else:
                    _write_file(zf, sources[name], arcname, store=False)
                    payload = _read_bytes(sources[name])
                files[name] = _sha256_bytes(payload)
            for name, payload in (
                (VERIFIER_FILENAME, verifier_bytes),
                (ENV_FILENAME, deployment_env),
                (CA_FILENAME, ca_bytes),
            ):
                _write_bytes(zf, f"{QUICKSTART_ROOT}/{name}", payload)
                files[name] = _sha256_bytes(payload)

            manifest = {
                "scaffold_version": version,
                "target_abi": abi,
                "compatible_lab_image_version": image_version,
                "wheelhouse_included": False,
                "source_revision": source_revision(),
                "zip_root": QUICKSTART_ROOT,
                "verifier_sha256": files[VERIFIER_FILENAME],
                "platform_ca_sha256": files[CA_FILENAME],
                "platform_ca_fingerprints": ca_fingerprints,
                "platform_ca_source": ca_source,
                "deployment_env_keys": [
                    "CSP_BASE_URL",
                    "ANILA_CA_FILE",
                    _AGENT_ID_ENV,
                    "LLM_BASE_URL",
                    "LLM_MODEL",
                    "LLM_AUTH_REQUIRED",
                ],
                "llm_api_key_included": False,
                "agent": {
                    "id": inputs.agent_id,
                    "name": inputs.agent_name,
                    "bound": inputs.agent_id is not None,
                    "collection_id": collection_id,
                    "collection_prefilled": collection_prefilled,
                    "available_collection_ids": sorted(
                        {int(c) for c in inputs.bound_collection_ids}
                    ),
                },
                "profile_model_key": None,
                "profile_llm_auth_required": True,
                "llm_base_url_source": "csp_base_url",
                "profile_extra_keys": list(profile.extra_keys),
                "scaffold_source": str(root),
                "files": files,
                **lock_report,
            }
            _write_bytes(
                zf,
                f"{QUICKSTART_ROOT}/{MANIFEST_FILENAME}",
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )
    except BundleError:
        tmp_path.unlink(missing_ok=True)
        raise
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise _read_error(Path(getattr(exc, "filename", None) or tmp_path), exc) from exc
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return BuiltBundle(
        path=tmp_path,
        filename=f"{QUICKSTART_ROOT}-{version}-{abi}.zip",
        manifest=manifest,
        bound=inputs.agent_id is not None,
    )


def _read_verifier(verifier_source: Path) -> bytes:
    if not verifier_source.is_file():
        raise BundleError(
            f"canonical 驗證器原始碼不存在：{verifier_source}；"
            "請確認 anila_core 已隨映像安裝（不另做第二份手改副本）"
        )
    payload = _read_bytes(verifier_source)
    if not payload.strip():
        raise BundleError(f"canonical 驗證器原始碼是空檔：{verifier_source}")
    return payload


def _skip_advanced(relative: Path, path: Path) -> bool:
    if _ADVANCED_EXCLUDED_PARTS.intersection(relative.parts):
        return True
    if path.name in _ADVANCED_EXCLUDED_NAMES or path.suffix in _ADVANCED_EXCLUDED_SUFFIXES:
        return True
    return not path.is_file()


def build_advanced_example_bundle(*, source_dir: Path | None = None) -> BuiltBundle:
    """Mirror ``packages/anila-agent`` as the advanced example (allow-listed).

    Explicit allow-list, not ``rglob`` + denylist: symlinks outside the tree
    are refused, and local ``.env`` / private keys / caches are dropped.
    """
    root = source_dir or advanced_source_dir()
    if not root.is_dir():
        raise BundleError(f"找不到進階範例目錄：{root}；請確認 packages/anila-agent 已掛載")
    root_resolved = root.resolve()

    members: list[tuple[Path, str]] = []
    seen: set[str] = set()

    def collect(directory: Path, prefix: Path) -> None:
        for path in sorted(directory.iterdir()):
            relative = prefix / path.name
            if _ADVANCED_EXCLUDED_PARTS.intersection(relative.parts):
                continue
            if path.name in _ADVANCED_EXCLUDED_NAMES:
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(root_resolved):
                raise BundleError(f"進階範例內有逃逸 symlink：{relative} → {resolved}")
            if path.is_dir():
                collect(resolved if path.is_symlink() else path, relative)
                continue
            if path.suffix in _ADVANCED_EXCLUDED_SUFFIXES or not path.is_file():
                continue
            key = relative.as_posix()
            if key not in seen:
                seen.add(key)
                members.append((path, key))

    collect(root, Path())

    tmp_path = _make_temp_zip()
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for src, relative in sorted(members, key=lambda pair: pair[1]):
                _write_file(zf, src, f"{ADVANCED_ROOT}/{relative}", store=False)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    version = _advanced_version(root)
    return BuiltBundle(
        path=tmp_path,
        filename=f"{ADVANCED_ROOT}-{version}.zip" if version else f"{ADVANCED_ROOT}.zip",
        manifest={},
        bound=False,
    )


def _advanced_version(root: Path) -> str | None:
    """Read the example's own version; omit it rather than invent one."""
    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    match = _PYPROJECT_VERSION_RE.search(text)
    return match.group(1) if match else None
