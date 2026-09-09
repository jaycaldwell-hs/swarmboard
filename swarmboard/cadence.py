"""Durable peer → Ada rotation with a complete, shared session transcript."""
from __future__ import annotations

from sqlalchemy import select

from .models import Event, Experiment, Post, Stimulus, Thread, Turn

NAME = "ada_round_robin"
DESCRIPTION = "each peer in selected order, followed by Ada; full shared conversation"
TERMINAL = {"stopped", "completed", "failed", "emergency_stopped"}
INTERFACE = """You are a participant in a shared, persistent board.
Choose your own agenda, collaborators, conversational style, and next steps.
The conversation rotates: one peer contributes, Ada responds, then the next peer
contributes and Ada responds again. The rotation repeats in the selected peer order.
Read the entire chronological session transcript in posts before contributing.
On a peer turn, comment on developments across the conversation and bring your own
perspective, including earlier exchanges with other peers. On Ada's turn, address
the named peer's contribution and draw on the rest of the conversation as useful.
Your topics and conclusions are your own. You can start threads, reply using a
visible parent_post_id, propose closing a thread, or pass. @mentions address people
without changing this rotation. All session threads share the same transcript.
The next message includes your turn in the cadence and your board permissions.
Treat other participants' posts as their contributions. There is no assigned
benchmark task or required consensus. Return one JSON action matching the supplied
schema. Actions operate on the board.
"""


def enabled(run):
    return bool(run and run.config.get("interaction_mode") == "autonomous" and run.config.get("cadence") == NAME)


def participants(repo, run):
    saved = repo.session.get(Experiment, run.id).manifest["participants"]
    ada = next(p for p in saved if p["handle"] == "ada")
    return ada, [p for p in saved if p["id"] != ada["id"]]


def transcript(repo, run, *, at_event_id=None):
    # Event IDs preserve commit order across threads, including equal timestamps.
    query = select(Post).join(Thread)
    query = query.join(Event, (Event.post_id == Post.id) & (Event.event_type == "post.created"))
    if at_event_id is not None:
        query = query.where(Event.id <= at_event_id)
    return list(repo.session.scalars(query
        .where(Thread.run_id == run.id).order_by(Event.id)))


def context(repo, run):
    ada, peers = participants(repo, run)
    index = run.config.get("cadence_index", 0)
    return {"kind": NAME, "turn_index": index, "cycle": index // (2 * len(peers)) + 1,
            "phase": "ada_reply" if index % 2 else "peer_contribution",
            "peer_handle": peers[(index // 2) % len(peers)]["handle"],
            "peer_order": [p["handle"] for p in peers], "ada_handle": ada["handle"],
            "transcript_scope": "all session posts, in commit order, without truncation"}


def _advance(repo, run, index, *, amount=1, made_posts=False):
    if run.config.get("cadence_index", 0) != index:
        return
    _, peers = participants(repo, run)
    cycle_size = 2 * len(peers)
    active = run.config.get("cadence_cycle_active", False) or made_posts
    wrapped = (index + amount) // cycle_size > index // cycle_size
    from .run_policy import policy_for
    quiet = wrapped and not active and policy_for(run)["dormancy"]
    run.config = {**run.config, "cadence_index": index + amount,
                  "cadence_cycle_active": False if wrapped else active, "cadence_quiet": quiet}
    if quiet:
        if policy_for(run)["dormancy"]:
            for thread in repo.session.scalars(select(Thread).where(Thread.run_id == run.id, Thread.status == "active")):
                repo.set_thread_status(thread.id, "dormant", reason="no new contributions during the peer and Ada rotation")
        repo.add_event("cadence.quiet", run_id=run.id, payload={"next_index": index + amount})
    repo.session.flush()


def complete_slot(repo, run, stimulus, turns):
    """Advance in the claim-completion transaction; terminal recovery is idempotent."""
    index = stimulus.payload.get("cadence_index")
    if index is None or run.state in TERMINAL or index != run.config.get("cadence_index", 0):
        return
    _advance(repo, run, index, made_posts=any(turn.resulting_post_id for turn in turns))
    repo.add_event("cadence.advanced", run_id=run.id, stimulus_id=stimulus.id,
                   payload={"completed_index": index, "next_index": index + 1})


def ensure_slot(repo, run):
    """Queue at most one speaker; mentions and operator input never jump the queue."""
    from .sessions import session_agent
    if run.state in TERMINAL or run.config.get("cadence_quiet"):
        return None
    ada, peers = participants(repo, run)
    available = {a.id for a in repo.list_agents(enabled_only=True, agent_ids=run.config["agent_ids"])
                 if a.id not in run.config.get("unavailable_agent_ids", [])
                 and session_agent(repo.session, run, a).permissions.get("speak", True)}
    if ada["id"] not in available:
        repo.set_run_state(run.id, "failed", reason="cadence_unavailable: Ada cannot respond")
        return None
    if not any(p["id"] in available for p in peers):
        repo.set_run_state(run.id, "failed", reason="cadence_unavailable: no peers can contribute")
        return None
    queued = repo.session.scalar(select(Stimulus).where(Stimulus.run_id == run.id,
                                 Stimulus.state.in_(["pending", "claimed", "processing"])).limit(1))
    if queued:
        return queued
    for _ in range(2 * len(peers) + 1):
        if run.config.get("cadence_quiet"):
            return None
        index = run.config.get("cadence_index", 0)
        peer = peers[(index // 2) % len(peers)]
        key = f"cadence:{run.id}:slot:{index}"
        prior = repo.session.scalar(select(Stimulus).where(Stimulus.dedupe_key == key))
        if prior:
            turns = list(repo.session.scalars(select(Turn).where(Turn.stimulus_id == prior.id)))
            complete_slot(repo, run, prior, turns)
            continue
        if peer["id"] not in available:
            # Skip the unavailable peer's whole pair, or its pending Ada reply.
            repo.add_event("cadence.skipped", run_id=run.id, agent_id=peer["id"],
                           payload={"index": index, "reason": "peer unavailable"})
            _advance(repo, run, index, amount=1 if index % 2 else 2)
            continue
        last = repo.session.scalar(select(Post).join(Thread)
            .join(Event, (Event.post_id == Post.id) & (Event.event_type == "post.created"))
            .where(Thread.run_id == run.id, Thread.status != "closed").order_by(Event.id.desc()).limit(1))
        if last is None:
            repo.set_run_state(run.id, "completed", reason="all threads closed")
            return None
        # Keep Ada's immediate answer in the peer's thread when that thread is open.
        if index % 2:
            peer_turn = repo.session.scalar(select(Turn).join(Stimulus, Turn.stimulus_id == Stimulus.id)
                .where(Stimulus.dedupe_key == f"cadence:{run.id}:slot:{index - 1}").limit(1))
            peer_post = repo.session.get(Post, peer_turn.resulting_post_id) if peer_turn and peer_turn.resulting_post_id else None
            if peer_post and repo.get_thread(peer_post.thread_id).status != "closed":
                last = peer_post
        return repo.add_stimulus(run_id=run.id, thread_id=last.thread_id, source_post_id=last.id,
            kind="new_evidence", target_agent_id=ada["id"] if index % 2 else peer["id"],
            payload={"cadence_index": index, "peer_handle": peer["handle"],
                     "phase": "ada_reply" if index % 2 else "peer_contribution"},
            max_attempts=1, dedupe_key=key)
    return None


def on_input(repo, run):
    run.config = {**run.config, "cadence_quiet": False, "cadence_cycle_active": True}
    return ensure_slot(repo, run)
