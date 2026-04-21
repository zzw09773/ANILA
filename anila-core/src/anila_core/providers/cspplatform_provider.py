"""CSP Platform provider — routes all LLM/agent calls through myCSPPlatform.

Router and agents should use this instead of OpenAICompatProvider to ensure
all traffic goes through the CSP data plane for usage accounting.
"""

from __future__ import annotations

from typing import Optional

from .openai_compat import OpenAICompatProvider


class CSPPlatformProvider(OpenAICompatProvider):
    """OpenAI-compatible provider that targets myCSPPlatform as base URL.

    Identical to OpenAICompatProvider but uses the CSP API Key as the
    Bearer token, so usage is attributed to the correct tenant.
    """

    def __init__(
        self,
        csp_base_url: str,
        csp_api_key: str,
        timeout: float = 120.0,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> None:
        super().__init__(
            base_url=f"{csp_base_url.rstrip('/')}/v1",
            api_key=csp_api_key,
            timeout=timeout,
            extra_headers=extra_headers,
        )
