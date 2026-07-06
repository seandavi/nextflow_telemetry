from pathlib import Path

WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "add-study.yml"
).read_text()


def test_add_study_workflow_registration_comment_links_to_telemetry() -> None:
    assert "Track progress in telemetry: https://cmgd.cancerdatasci.org" in WORKFLOW


def test_preview_job_does_not_trigger_on_labeled_add_study() -> None:
    """Regression guard: the preview job must NOT include a `labeled: add-study`
    trigger condition.  When an issue is opened via the template (which pre-sets
    the `add-study` label), GitHub fires both an `opened` event and a `labeled`
    event.  If the preview job matched both, two identical bot comments would be
    posted on every new issue.  See: https://github.com/seandavi/nextflow_telemetry/issues/145
    """
    # The apply job legitimately handles the `approved` labeled event, so we only
    # want to verify that the *preview* job section doesn't contain the problematic
    # `labeled` + `add-study` combination.
    preview_section = WORKFLOW.split("apply:")[0]
    assert "labeled' && github.event.label.name == 'add-study'" not in preview_section
    assert 'labeled" && github.event.label.name == "add-study"' not in preview_section
