from pathlib import Path


def test_windows_build_specs_do_not_use_upx() -> None:
    root = Path(__file__).resolve().parents[1]
    for spec in ("BebraLandLauncher.spec", "BebraLandUpdater.spec", "BebraLandLauncherSetup.spec"):
        text = (root / spec).read_text(encoding="utf-8")
        assert "upx=True" not in text


if __name__ == "__main__":
    test_windows_build_specs_do_not_use_upx()
