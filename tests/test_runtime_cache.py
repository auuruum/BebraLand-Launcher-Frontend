from pathlib import Path
from tempfile import TemporaryDirectory

from bebraland_frontend.runtime import apply_cleanup_tasks, version_is_installed


def write_version(root: Path, version_id: str, metadata: str = "{}") -> Path:
    version_dir = root / "versions" / version_id
    version_dir.mkdir(parents=True)
    (version_dir / f"{version_id}.json").write_text(metadata, encoding="utf-8")
    return version_dir


def test_inherited_version_requires_parent_jar() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_version(root, "neoforge-21.1.233", '{"inheritsFrom":"1.21.1"}')
        parent_dir = write_version(root, "1.21.1")

        assert not version_is_installed(root, "neoforge-21.1.233")

        (parent_dir / "1.21.1.jar").write_bytes(b"jar")
        assert version_is_installed(root, "neoforge-21.1.233")


def test_cleanup_task_runs_once_per_id() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / ".voxy"
        target.mkdir()

        task = {"id": "cleanup-1", "paths": [".voxy"]}
        assert apply_cleanup_tasks(root, [task], lambda _: None) == 1
        assert not target.exists()

        target.mkdir()
        assert apply_cleanup_tasks(root, [task], lambda _: None) == 0
        assert target.exists()


if __name__ == "__main__":
    test_inherited_version_requires_parent_jar()
    test_cleanup_task_runs_once_per_id()
