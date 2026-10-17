from pathlib import Path

from git import Repo

from gitcontext.ingest import github


def test_github_pr_and_incident_records_are_normalized(tmp_path: Path, monkeypatch) -> None:
    repository = Repo.init(tmp_path)
    repository.create_remote("origin", "https://github.com/acme/project.git")
    pull = {
        "number": 12, "title": "Fix login", "body": "Avoid token leak",
        "user": {"login": "dev"}, "created_at": "2026-01-01",
        "updated_at": "2026-01-02", "labels": [], "html_url": "https://github.com/acme/project/pull/12",
    }
    incident = {
        "number": 4, "title": "Login outage", "body": "Auth API failed",
        "user": {"login": "oncall"}, "created_at": "2026-01-03",
        "updated_at": "2026-01-04", "labels": [{"name": "incident"}],
        "html_url": "https://github.com/acme/project/issues/4",
    }

    def fake_get(url: str, headers: dict[str, str]) -> list[dict]:
        if "/pulls?" in url:
            return [pull]
        if "/issues?" in url:
            return [pull | {"pull_request": {}}, incident]
        if "/issues/12/comments" in url:
            return [{"user": {"login": "reviewer"}, "body": "Please add a test."}]
        if "/pulls/12/comments" in url:
            return [{"user": {"login": "reviewer"}, "body": "Potential race."}]
        if "/issues/4/comments" in url:
            return [{"user": {"login": "oncall"}, "body": "Rolled back."}]
        return []

    monkeypatch.setattr(github, "_get_json", fake_get)

    pulls, incidents = github.github_documents(tmp_path, token=None)

    assert len(pulls) == 1
    assert pulls[0].pr_number == 12
    assert "Potential race." in pulls[0].content
    assert len(incidents) == 1
    assert incidents[0].source_type == "incident"
    assert "Rolled back." in incidents[0].content
