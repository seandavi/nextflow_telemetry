"""one active version per workflow

Revision ID: f4a5b6c7
Revises: e3f4a5b6
Create Date: 2026-07-02

Enforces the Epic A identity decision (docs/study-sample-version-identity.md):
**exactly one active version per workflow_id**. This makes "the active version"
unambiguous, which the completion metrics (#116) assume.

Data cleanup first, because existing databases may already have several active
versions of the same workflow_id (prod had 2 at time of writing):
  1. For each workflow_id, keep the most-recently-created active version active
     and retire the rest.
  2. Purge the newly-retired versions' still-`pending` jobs (never-dispatched
     orphans — same rule as retiring a workflow at runtime, #114).
Then add the partial unique index that guarantees the invariant going forward.

Going forward the invariant is *maintained* by WorkflowService: registering a
new version or promoting one to active auto-retires the prior active version in
the same transaction, so this index never actually rejects an app-driven write —
it is a backstop against direct SQL / bugs.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "f4a5b6c7"
down_revision: Union[str, Sequence[str], None] = "e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# active versions that are NOT the newest for their workflow_id
_STALE_ACTIVE = """
    SELECT id FROM workflows
    WHERE status = 'active'
      AND id NOT IN (
          SELECT DISTINCT ON (workflow_id) id
          FROM workflows
          WHERE status = 'active'
          ORDER BY workflow_id, created_at DESC, id DESC
      )
"""


def upgrade() -> None:
    # 1. Purge orphaned pending jobs of the versions we're about to retire
    #    (do this while they are still 'active' so the subquery selects them).
    op.execute(
        f"DELETE FROM jobs WHERE status = 'pending' AND workflow_pk IN ({_STALE_ACTIVE})"
    )
    # 2. Retire all but the newest active version per workflow_id.
    op.execute(
        f"UPDATE workflows SET status = 'retired', updated_at = now() WHERE id IN ({_STALE_ACTIVE})"
    )
    # 3. Enforce the invariant going forward.
    op.execute(
        "CREATE UNIQUE INDEX uq_one_active_version_per_workflow "
        "ON workflows (workflow_id) WHERE status = 'active'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_one_active_version_per_workflow")
