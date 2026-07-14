from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class AnilaLmBrowserStorageContractTests(unittest.TestCase):
    def test_auth_and_artifact_stores_are_memory_only(self) -> None:
        cases = {
            "auth.ts": "anilalm:auth",
            "artifacts.ts": "anilalm:artifacts",
        }
        store_root = ROOT / "apps" / "anilalm" / "src" / "store"

        for filename, legacy_key in cases.items():
            with self.subTest(filename=filename):
                source = (store_root / filename).read_text(encoding="utf-8")
                self.assertNotIn("zustand/middleware", source)
                self.assertNotIn("persist(", source)
                self.assertNotIn("localStorage.setItem", source)
                self.assertIn(f"localStorage.removeItem('{legacy_key}')", source)

        csp_cache = (store_root / "cspArtifacts.ts").read_text(encoding="utf-8")
        self.assertNotIn("zustand/middleware", csp_cache)
        self.assertNotIn("persist(", csp_cache)
        self.assertNotIn("localStorage", csp_cache)

    def test_outputs_are_loaded_from_csp_and_cleared_on_logout(self) -> None:
        src_root = ROOT / "apps" / "anilalm" / "src"
        api = (src_root / "api" / "artifacts.ts").read_text(encoding="utf-8")
        outputs = (src_root / "routes" / "OutputsPage.tsx").read_text(
            encoding="utf-8"
        )
        auth = (src_root / "store" / "auth.ts").read_text(encoding="utf-8")

        self.assertIn("export async function listCspArtifacts", api)
        self.assertIn("'/api/artifacts'", api)
        self.assertIn("listCspArtifacts()", outputs)
        self.assertNotIn("useArtifactStore", outputs)
        self.assertIn("useCspArtifactStore.getState().clear()", auth)

    def test_mindmap_viewer_never_downloads_from_studio(self) -> None:
        src_root = ROOT / "apps" / "anilalm" / "src"
        studio_api = (src_root / "api" / "studio.ts").read_text(encoding="utf-8")
        viewer = (src_root / "workspace" / "ArtifactViewer.tsx").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("fetchMindmapTree", studio_api)
        self.assertNotIn("/download/json", studio_api)
        self.assertNotIn("fetchMindmapTree", viewer)
        self.assertIn("downloadCspArtifact", viewer)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
