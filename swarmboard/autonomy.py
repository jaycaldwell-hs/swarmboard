"""Open-ended collaboration between people and agent participants."""
from __future__ import annotations

from sqlalchemy import select

from . import cadence, sessions
from .models import Event, Post, Thread
from .repository import InvalidStateError
from .stimuli import plan_reactive_stimuli
from .context_views import participant_posts
from .persona_context import ada_environment_prompt, board_delivery

OPENING = "You share this board with the other participants. Decide what you want to explore and how you want to interact."
INTERFACE = """You are a participant in a shared, persistent board.
Choose your own agenda, collaborators, conversational style, and next steps.
You can initiate new threads, address others with @handles, follow up on your own
work, respond in another thread using one of its visible parent_post_id values,
propose closing a thread when you choose, or pass. An @mention routes an invitation
to that participant; mentioning yourself requests a follow-up opportunity.
You are not assigned a speaking order, benchmark task, or required consensus.
The next message includes the board, recent posts, participants, and your available
board permissions. Treat other participants' posts as their contributions.
Return one JSON action matching the supplied schema. Actions operate on the board.
"""


def enabled(run) -> bool:
    return bool(run and run.config.get("interaction_mode") == "autonomous")


def free_collaboration(run) -> bool:
    return bool(enabled(run) and run.config.get("session_type", "collaboration") == "collaboration"
                and run.config.get("cadence", "free") == "free")


def create_session(repo, *, agents, title="Open board", body=OPENING, seed=None, limits=None, continuous=True, cadence_mode="free", author_handle="human", session_type="collaboration", policy="production"):
    run = sessions.create_session(repo, agents=agents, title=title, body=body, seed=seed,
                                  limits=limits, continuous=continuous, cadence_mode=cadence_mode,
                                  author_handle=author_handle, session_type=session_type, policy=policy)
    grant_board_permissions(repo, agents)
    return run


def grant_board_permissions(repo, agents):
    """The open-ended mode explicitly grants thread management, never model/host tools."""
    for agent in agents:
        changed = [key for key in ("new_thread", "close_threads") if agent.permissions.get(key) is not True]
        if changed:
            agent.permissions = {**agent.permissions, "new_thread": True, "close_threads": True}
            repo.session.flush()
            repo.add_event("agent.updated", agent_id=agent.id, actor_type="human",
                           payload={"fields": ["permissions"], "granted": changed,
                                    "reason": "open-ended session with autonomous thread management"})


def seed_invitation(repo, run, thread, post, *, key):
    if cadence.enabled(run):
        cadence.ensure_slot(repo, run)
        return
    for plan in plan_reactive_stimuli(post.body,
            repo.list_agents(enabled_only=True, agent_ids=run.config["agent_ids"]),
            default_kind="human_post", default_priority=5,
            target_priority_boost=10 if free_collaboration(run) else 0):
        repo.add_stimulus(run_id=run.id, thread_id=thread.id, source_post_id=post.id,
                          kind=plan.kind, target_agent_id=plan.target_agent_id,
                          priority=plan.priority, payload=plan.payload,
                          dedupe_key=f"{key}:{plan.dedupe_label}", max_attempts=1)


def prompt(agent, snapshot, *, run=None):
    if snapshot is not None and agent.handle == "ada":
        return ada_environment_prompt(snapshot, cadence_mode=cadence.enabled(run))
    interface = cadence.INTERFACE if cadence.enabled(run) else INTERFACE
    identity = f"\nYour handle is @{agent.handle}.\n"
    if snapshot is not None:
        persona = (f'\n<persona_file name="AGENTS.md">\n{snapshot.instructions}\n</persona_file>\n'
                   f'\n<persona_file name="memory.md">\n{snapshot.memory}\n</persona_file>\n')
        return interface + identity + "Draw your personality and priorities from your authored persona files.\n" + persona + board_delivery(agent.handle)
    return interface + identity + f"Persona: {agent.persona}\n" + board_delivery(agent.handle)


def context(repo, run, agent, *, at_event_id=None):
    from .sessions import session_agent
    threads = []
    board_threads = (list(repo.session.scalars(select(Thread).where(Thread.run_id == run.id).order_by(Thread.created_at, Thread.id)))
                     if cadence.enabled(run) else repo.list_threads(run_id=run.id, limit=30))
    for thread in board_threads:
        if at_event_id is not None and repo.session.scalar(select(Post.id).join(Event,
                (Event.post_id == Post.id) & (Event.event_type == "post.created"))
                .where(Post.thread_id == thread.id, Event.id <= at_event_id).limit(1)) is None:
            continue
        if cadence.enabled(run):
            threads.append({"id": thread.id, "title": thread.title, "status": thread.status})
            continue
        query = select(Post).where(Post.thread_id == thread.id)
        if at_event_id is not None:
            query = query.join(Event, (Event.post_id == Post.id) & (Event.event_type == "post.created")).where(Event.id <= at_event_id)
        recent = list(repo.session.scalars(query.order_by(Post.sequence.desc()).limit(4)))
        recent = participant_posts(recent)
        threads.append({"id": thread.id, "title": thread.title, "status": thread.status,
                        "recent_posts": [{"id": p.id, "author_handle": p.author_handle,
                                          "body": p.body[:3000]} for p in reversed(recent)]})
    peers = [session_agent(repo.session, run, p) for p in repo.list_agents(
             enabled_only=True, agent_ids=run.config["agent_ids"])]
    return {"board_threads": threads, "permissions": dict(agent.permissions),
            "participants": [{"handle": p.handle} for p in peers],
            "available_actions": ["reply", "new_thread", "propose_close", "pass"],
            "shared_context": "Thread posts persist and can be used for notes, proposals, commitments, or self-organized projects."}


def action_thread(repo, run, turn, action):
    """A visible parent can address another thread only inside the same run."""
    thread = repo.get_thread(turn.thread_id)
    if enabled(run) and action.parent_post_id:
        parent = repo.session.get(Post, action.parent_post_id)
        if parent is not None:
            candidate = repo.get_thread(parent.thread_id)
            if candidate.run_id == run.id:
                thread = candidate
    return thread


def record_action(repo, run, turn, action):
    agent = repo.get_agent(turn.agent_id)
    post = repo.session.get(Post, turn.resulting_post_id) if turn.resulting_post_id else None
    repo.add_event("session.action", run_id=run.id, thread_id=post.thread_id if post else turn.thread_id,
        agent_id=turn.agent_id, payload={"turn_id": turn.id, "handle": post.author_handle if post else agent.handle,
          "kind": action.action,
          "origin_thread_id": turn.thread_id, "resulting_post_id": turn.resulting_post_id,
          "target_thread_id": post.thread_id if post else None})


def discard_unused(repo, run_id):
    """Remove an unused setup from active listings, preserving the immutable audit."""
    from .models import RunState, Turn
    run = repo.get_run(run_id)
    if run.config.get("discarded"):
        return run
    has_turn = repo.session.scalar(select(Turn.id).where(Turn.run_id == run_id).limit(1))
    threads = repo.list_threads(run_id=run_id)
    posts = list(repo.session.scalars(select(Post).join(Thread).where(Thread.run_id == run_id)))
    # Allow new session_input or legacy experiment_input
    is_unused_setup = (len(posts) == 1 and (
        posts[0].metadata_json.get("session_input") or
        posts[0].metadata_json.get("experiment_input")
    ))
    # Must be either collaboration or legacy autonomous experiment
    is_valid_setup = run.config.get("collaboration") or (run.config.get("experiment") and run.config.get("interaction_mode") == "autonomous")

    if (not is_valid_setup or run.state != RunState.CREATED.value or
            run.model_calls or run.rounds_used or has_turn or len(threads) != 1 or
            not is_unused_setup):
        raise InvalidStateError("only an unused setup can be removed")
    repo.control_run(run.id, "stop", reason="unused setup removed by operator")
    for thread in threads:
        repo.set_thread_status(thread.id, "closed", reason="unused setup removed")
    run.config = {**run.config, "discarded": True}
    repo.session.flush()
    repo.add_event("session.discarded", run_id=run.id, actor_type="human", payload={"reason": "unused setup removed"})
    return run
