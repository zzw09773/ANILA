# Card and identity material audit

Generated: `2026-07-11T01:37:21.254204+00:00`

This report is redacted by construction: matched values are represented only by SHA-256 prefixes and byte lengths.

## Summary

- Current-tree blocking candidates: **0**
- Current-tree total candidates: **0**
- Reachable-history candidates: **565**
- Reachable-history unique redacted values: **25**
- Reachable-history affected blobs: **142**
- Reachable-history critical blobs: **2**
- Local/remote/tag refs scanned: **30**
- Live remote comparison (`origin`): **synchronized** (24 refs)

## Current tree

No candidate personal card fixture or serialized private key was found.

## Allowed public trust material

The production CSPKI bundle contains public CA certificates only. It remains the production trust anchor and is not a private-key fixture.

| Scope | Kind | Path | Value hash |
|---|---|---|---|
| current | public_ca_trust_anchor | `services/csp/app/services/cspki_ca_bundle.pem` | `f1a1bfaa0f6bfb35` |
| history | public_ca_trust_anchor | `services/csp/app/services/cspki_ca_bundle.pem` | `1ab9ec965dd39bc2` |

## Reachable Git history

History findings are evidence only. This scanner does not rewrite objects, delete refs, force-push, revoke cards, or rotate keys.

| Severity | Kind | Path | Blob | Refs | Value hash |
|---|---|---|---|---:|---|
| critical | serialized_private_key | `myCSPPlatform/docker/certs/server.key` | `cb87dc45d043` | 30 | `7732abdb2784d7ef` |
| critical | serialized_private_key | `myCSPPlatform/docker/certs/server.key.bak` | `79eed6cabcc9` | 30 | `f0912301946ece86` |
| high | employee_id_in_home_path | `ANILALM/README.en.md` | `7785089cdda5` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `ANILALM/README.en.md` | `7785089cdda5` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `ANILALM/README.en.md` | `7785089cdda5` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `ANILALM/README.en.md` | `7785089cdda5` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `ANILALM/README.en.md` | `7785089cdda5` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `ANILALM/README.md` | `960c1d10ae62` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `ANILALM/README.md` | `42738a7e7cf5` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `ANILALM/README.md` | `42738a7e7cf5` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `ANILALM/README.md` | `960c1d10ae62` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `ANILALM/README.md` | `42738a7e7cf5` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `ANILALM/README.md` | `960c1d10ae62` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `ANILALM/README.md` | `960c1d10ae62` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `ANILALM/README.md` | `960c1d10ae62` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `CLAUDE.md` | `539388f6aba3` | 17 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `CLAUDE.md` | `dd6548813228` | 27 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `CLAUDE.md` | `ec6c2d843967` | 16 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `CLAUDE.md` | `3f5858b0c5d6` | 5 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `README.md` | `291ed32e21d9` | 1 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `README.md` | `0edea77c127a` | 1 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `README.md` | `05c0f6b30851` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `README.md` | `3e9e8da3dcc8` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `README.md` | `44eac379cda8` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `README.md` | `5732f4b68573` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `README.md` | `1673b871e691` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `README.md` | `49edf08eb1db` | 22 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `README.md` | `a96e23006bc8` | 22 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `README.md` | `35e785421fb1` | 22 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `README.md` | `25d46b3aef92` | 22 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `README.md` | `cd8928089a39` | 21 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `README.md` | `05c0f6b30851` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `README.md` | `5732f4b68573` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `README.md` | `44eac379cda8` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `README.md` | `3e9e8da3dcc8` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `README.md` | `1673b871e691` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `README.md` | `a96e23006bc8` | 22 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `README.md` | `49edf08eb1db` | 22 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `README.md` | `35e785421fb1` | 22 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `README.md` | `25d46b3aef92` | 22 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `README.md` | `cd8928089a39` | 21 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `anila_plan.md` | `372694eaa2e1` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `anila_plan.md` | `9078c66ccc5c` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `anila_plan.md` | `44ef024222ce` | 22 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `anila_plan.md` | `cc5090be7a2e` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `anila_plan.md` | `7f5007c6d4bb` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `apps/anilalm/_design/README.md` | `b916b5007057` | 17 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `apps/anilalm/_design/README.md` | `43d916958035` | 29 | `1ce2b3a02a7c2977` |
| high | certificate_subject_person_name | `apps/csp-governance-ui/src/api/caAuth.js` | `a7ccd881fc83` | 22 | `8e252154bc6c2040` |
| high | employee_id | `apps/csp-governance-ui/src/api/caAuth.js` | `a7ccd881fc83` | 22 | `9a49ae9ae3db7e4b` |
| high | card_serial | `cht/app.py` | `d0f915719726` | 22 | `5bb9661aacafb3ba` |
| high | embedded_certificate | `cht/app.py` | `d0f915719726` | 22 | `c5a49f942fe4a174` |
| high | embedded_cms_signature | `cht/app.py` | `d0f915719726` | 22 | `e9bbd821cef45d0b` |
| high | card_serial | `cht/app.py` | `d0f915719726` | 22 | `5bb9661aacafb3ba` |
| high | certificate_subject_person_name | `cht/app.py` | `d0f915719726` | 22 | `8e252154bc6c2040` |
| high | embedded_certificate | `cht/app.py` | `d0f915719726` | 22 | `092993f8422f0418` |
| high | embedded_certificate | `cht/app.py` | `d0f915719726` | 22 | `29e1d198fbd139c2` |
| high | embedded_certificate | `cht/app.py` | `d0f915719726` | 22 | `c5a49f942fe4a174` |
| high | embedded_certificate | `cht/app.py` | `d0f915719726` | 22 | `86354ca43a6e3ed3` |
| high | embedded_certificate | `cht/app.py` | `d0f915719726` | 22 | `4bd2d4c5445754c4` |
| high | embedded_certificate | `cht/app.py` | `d0f915719726` | 22 | `fa92c7799e8a2ec0` |
| high | employee_id | `cht/app.py` | `d0f915719726` | 22 | `9a49ae9ae3db7e4b` |
| high | institutional_email | `cht/app.py` | `d0f915719726` | 22 | `4dc32c9f30662dc6` |
| high | employee_id_in_home_path | `docs/agent-framework/openai-agents-python-deep-dive.md` | `06f501840820` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/agent-framework/openai-agents-python-deep-dive.md` | `06f501840820` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id | `docs/anila-redesign-docs/07-registered-gui-service-platform.md` | `eae781ab567e` | 17 | `8d969eef6ecad3c2` |
| high | employee_id | `docs/anila-redesign-docs/07-registered-gui-service-platform.md` | `eae781ab567e` | 17 | `8d969eef6ecad3c2` |
| high | employee_id_in_home_path | `docs/architecture/ingestion-platform-design.md` | `295192a0f34e` | 22 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/anila-full-audit-2026-06-02.md` | `22beeef3509c` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/audits/openwebui-gap-analysis-2026-06-11.md` | `4844d01a25da` | 28 | `1ce2b3a02a7c2977` |
| high | employee_id | `docs/history/03-intranet-v1.2.0.md` | `f6f00f2334c3` | 17 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/ingestion-platform-design.md` | `3ece56853be7` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/ingestion-platform-design.md` | `d412b245c882` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/ingestion-platform-design.md` | `94f383361026` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/ingestion-platform-design.md` | `d450322fdadd` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/ingestion-platform-design.md` | `170cec2ce246` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/ingestion-platform-design.md` | `35bb29ceb0bb` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/ingestion/ingestion-platform-design.md` | `f8b9b209beff` | 1 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/ingestion/ingestion-platform-design.md` | `703b75cad056` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `e1a59df1bbf0` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `8f51d17d858f` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `f964b1252915` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `52fe171963d0` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `725895f38cd7` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `9e316a0b3fd2` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `29dce9c5cefb` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `e293d18eb262` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `fae8d306c9f7` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `24380815189f` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `9aebcaa451fa` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `e01766ae2d00` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `726ca8dcae6c` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `f964b1252915` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `52fe171963d0` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `726ca8dcae6c` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `e01766ae2d00` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `e1a59df1bbf0` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `8f51d17d858f` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `9aebcaa451fa` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `24380815189f` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `e293d18eb262` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `fae8d306c9f7` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `726ca8dcae6c` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `29dce9c5cefb` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `9e316a0b3fd2` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `725895f38cd7` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `f964b1252915` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `52fe171963d0` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `e01766ae2d00` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `e1a59df1bbf0` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `8f51d17d858f` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `9aebcaa451fa` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `24380815189f` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `e293d18eb262` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `fae8d306c9f7` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `29dce9c5cefb` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `9e316a0b3fd2` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `725895f38cd7` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `f964b1252915` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `52fe171963d0` | 30 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `e1a59df1bbf0` | 29 | `1ce2b3a02a7c2977` |
| high | employee_id_in_home_path | `docs/multi-service-integration-plan.md` | `8f51d17d858f` | 30 | `1ce2b3a02a7c2977` |

Only the first 250 rows are rendered here; the JSON report contains all 565 candidates.

### Refs scanned

- `refs/heads/claude/design-system-foundation-6fa09f`
- `refs/heads/claude/design-system-foundation-c4fa21`
- `refs/heads/claude/musing-hugle-dbc204`
- `refs/heads/fix/gate0-security-hardening`
- `refs/heads/main`
- `refs/remotes/origin/HEAD`
- `refs/remotes/origin/anila-redesign`
- `refs/remotes/origin/dev-military`
- `refs/remotes/origin/dev-public`
- `refs/remotes/origin/feature/backend-adapter`
- `refs/remotes/origin/main`
- `refs/remotes/origin/prod-intranet-card`
- `refs/remotes/origin/prod-military-passwd`
- `refs/remotes/origin/prod-public-passwd`
- `refs/remotes/origin/trial-military`
- `refs/tags/archive/feat-stage3-typed-terminal`
- `refs/tags/pre-branch-restructure-2026-05-26`
- `refs/tags/pre-converge/dev-military`
- `refs/tags/pre-converge/dev-public`
- `refs/tags/pre-converge/main`
- `refs/tags/pre-converge/prod-intranet-card`
- `refs/tags/pre-converge/prod-military-passwd`
- `refs/tags/pre-converge/prod-public-passwd`
- `refs/tags/pre-converge/trial-military`
- `refs/tags/pre-onyx-filter-repo-2026-04-27`
- `refs/tags/v1.0.0`
- `refs/tags/v1.1.0`
- `refs/tags/v1.2.0`
- `refs/tags/v2.0.0`
- `refs/tags/v2.0.1`

## Separately authorized incident / rewrite steps

1. Security/data owners validate each redacted candidate and open an incident record before destructive cleanup.
2. If any private key or active credential is confirmed, revoke/rotate it first; history cleanup does not make a leaked credential safe.
3. Obtain explicit approval for a coordinated `git filter-repo` rewrite of every affected branch and tag, followed by protected force-pushes.
4. Expire or replace old clones, forks, mirrors, CI caches, release archives, and backups that retain the old object IDs; notify all developers to reclone.
5. Re-run this scanner against the rewritten authoritative remote and preserve the redacted before/after reports as incident evidence.
