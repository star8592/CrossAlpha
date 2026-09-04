from pathlib import Path

from crossalpha.doctor import storage_report


def test_storage_report_accepts_external_data_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    repo.mkdir()

    report = storage_report(data, repo)

    assert report["ok"] is True
    assert report["inside_repo"] is False
    assert (data / "raw").is_dir()
    assert (data / "manifests").is_dir()


def test_storage_report_rejects_data_inside_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    data = repo / "data"
    repo.mkdir()

    report = storage_report(data, repo)

    assert report["ok"] is False
    assert report["inside_repo"] is True
    assert "data root is inside the Git working tree" in report["warnings"]
