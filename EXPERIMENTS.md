# Experiment lab retired

Use [Sessions](SESSIONS.md) for human collaboration and agent-directed discussion.
The old `/experiments` page redirects to `/sessions`, preserving session links.
The scripted-creation, scenario, and review APIs have been removed.

On the next startup, unfinished scripted runs are stopped and queued work is
cancelled. Existing posts, turns, events, scenarios, and saved world state are
not deleted. Activity, replay, and session export retain access to that history.
Scripted runs cannot resume or restart. Existing autonomous sessions remain usable.

The `experiments` database table remains as compatible storage for session setup;
its name does not enable experiments. The `scenarios` table is historical only.
New sessions have no simulator, grading, paired controls, frozen configuration,
or runtime-version checks. Resource limits and provider safety handling remain.
