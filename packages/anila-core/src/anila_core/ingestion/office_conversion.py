"""Bounded, local conversion of legacy Office files using an existing binary."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .errors import ParseError


def convert_legacy(file_path: str) -> Any:
    """Convert in a private profile, then parse through the shared registry."""
    from .parser_registry import ParserRegistry

    source = Path(file_path).resolve()
    target = {".doc": "docx", ".rtf": "docx", ".ppt": "pptx"}[source.suffix.lower()]
    binary = shutil.which("libreoffice") or shutil.which("soffice")
    if binary is None:
        raise ParseError.bad_config(
            "此檔案需要院內離線轉檔工具 LibreOffice，目前未提供。請通知管理員，或另存為 DOCX／PPTX 後上傳。",
            details={"reason": "office_converter_missing", "format": source.suffix},
        )
    with tempfile.TemporaryDirectory(prefix="anila-office-") as work:
        root = Path(work)
        profile = root / "profile"
        user = profile / "user"
        user.mkdir(parents=True)
        (user / "registrymodifications.xcu").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item>'
            '<item oor:path="/org.openoffice.Office.Calc/Content/Update">'
            '<prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item>'
            "</oor:items>",
            encoding="utf-8",
        )
        # Fixed local name prevents option injection and strips untrusted path text.
        local = root / ("source" + source.suffix.lower())
        shutil.copyfile(source, local)
        output = root / "output"
        output.mkdir()
        runtime = root / "runtime"
        runtime.mkdir(mode=0o700)
        task_env = {
            **os.environ,
            "GSETTINGS_BACKEND": "memory",
            "SAL_USE_VCLPLUGIN": "svp",
            "XDG_RUNTIME_DIR": str(runtime),
            "XDG_CACHE_HOME": str(root / "cache"),
        }
        try:
            result = subprocess.run(
                [
                    binary,
                    "-env:UserInstallation=" + profile.as_uri(),
                    "--headless",
                    "--nologo",
                    "--nodefault",
                    "--nofirststartwizard",
                    "--norestore",
                    "--convert-to",
                    target,
                    "--outdir",
                    str(output),
                    str(local),
                ],
                capture_output=True,
                timeout=60,
                check=False,
                env=task_env,
            )
        except subprocess.TimeoutExpired as exc:
            raise ParseError.bad_config(
                "離線轉檔超過時間限制，請通知管理員或另存為 DOCX／PPTX 後上傳。",
                details={"reason": "office_converter_timeout"},
            ) from exc
        except OSError as exc:
            raise ParseError.bad_config(
                "離線轉檔工具目前無法執行，請通知管理員。",
                details={"reason": "office_converter_unavailable"},
            ) from exc
        converted = output / ("source." + target)
        if result.returncode != 0 or not converted.is_file() or not converted.stat().st_size:
            raise ParseError.corrupt(
                "此檔案無法離線轉檔，可能已損毀或受到密碼保護。請確認可正常開啟，或另存為 DOCX／PPTX 後上傳。",
                details={"reason": "office_conversion_failed", "exit_code": result.returncode},
            )
        parsed = ParserRegistry.parse(str(converted))
        if parsed.metadata.get("title") == "source":
            parsed.metadata["title"] = source.stem
        parsed.source_path = file_path
        parsed.format = source.suffix.lstrip(".").lower()
        parsed.metadata.update({"converted_from": parsed.format, "conversion": "libreoffice-local"})
        return parsed
