"""プロジェクトの土台が動くことを確認するスモークテスト（Phase 0）。"""

import importlib


def test_package_is_importable() -> None:
    module = importlib.import_module("aidigest")
    assert module.__name__ == "aidigest"
