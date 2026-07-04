from pathlib import Path


def test_add_study_workflow_registration_comment_links_to_telemetry() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "add-study.yml"
    ).read_text()

    assert "Track progress in telemetry: https://cmgd.cancerdatasci.org" in workflow
