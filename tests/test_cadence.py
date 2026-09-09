from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from swarmboard import autonomy, sessions, cadence
from swarmboard.app import create_app, _add_post_stimuli
from swarmboard.engine import EngineConfig, SwarmEngine
from swarmboard.gateways import AgentAction, GatewayError
from swarmboard.models import Agent, Event, Post, Run, Stimulus, Thread, Turn, utc_now
from swarmboard.repository import Repository
from swarmboard.persona_context import delivery_prompt_version
from .test_engine_acceptance import ScriptedGateway, BlockingGateway, make_database


def seed(factory, *, peer_count=3):
    with factory.begin() as session:
        repo = Repository(session)
        ada = repo.create_agent(handle='ada', provider='codex', model='gpt-6-astra', persona='Original Ada persona.')
        peers = [repo.create_agent(handle=f'peer{i}', provider='openai_compatible', model=f'open-model-{i}',
                                   persona=f'Original peer {i}.') for i in range(1, peer_count+1)]
        run = autonomy.create_session(repo, agents=[ada, *peers], body='@ada @peer3 Choose your own topic.',
            cadence_mode=cadence.NAME, continuous=False,
            limits={'max_rounds':100, 'max_posts':200, 'max_tokens':1_000_000, 'max_duration_seconds':3600})
        thread = session.scalar(select(Thread).where(Thread.run_id == run.id))
        return run.id, thread.id, ada.id, [p.id for p in peers]


def reply(body='A contribution to the shared discussion.'):
    return AgentAction(action='reply', body=body, intent='support')


@pytest.mark.asyncio
async def test_peer_ada_rotation_receives_full_transcript_across_threads_and_context_limits():
    db, factory = make_database()
    rid, tid, ada, peers = seed(factory)
    with factory.begin() as session:
        repo = Repository(session)
        side = repo.create_thread(title='Shared side thread', run_id=rid)
        for i in range(45):
            repo.create_human_post(tid if i % 2 else side.id, f'Earlier message {i}: '+('x'*3500)+f' tail-{i}')
        foreign = repo.create_thread(title='Another session')
        repo.create_human_post(foreign.id, 'This must stay outside the shared transcript.')
    seen = []
    def respond(agent, messages):
        seen.append(agent.handle)
        context = json.loads(messages[-1].content.split('\n',1)[1])
        with factory() as session:
            expected = cadence.transcript(Repository(session), session.get(Run, rid))
            assert [p['id'] for p in context['posts']] == [p.id for p in expected]
            assert [p['body'] for p in context['posts']] == [p.body for p in expected]
        assert len(context['posts']) == 45 + len(seen)
        assert 'tail-0' in context['posts'][1]['body']
        assert context['cadence']['peer_handle'] == f'peer{((len(seen)-1)//2)%3+1}'
        assert 'entire chronological session transcript' in messages[0].content
        assert 'Original Ada persona.' in messages[0].content if agent.handle=='ada' else 'Original peer' in messages[0].content
        if len(seen) == 1:
            return AgentAction(action='new_thread', title='Peer chooses a direction', body='@peer3 @peer1 A new idea.', intent='clarify')
        return reply(f'@peer3 @peer3 Whole-conversation contribution {len(seen)}')
    gateway = ScriptedGateway(*([respond]*12))
    engine = SwarmEngine(factory, gateway=gateway, config=EngineConfig(context_post_limit=2))
    for _ in range(12):
        await engine.step(rid)
    assert seen == ['peer1','ada','peer2','ada','peer3','ada']*2
    with factory() as session:
        turns = list(session.scalars(select(Turn).where(Turn.run_id==rid).order_by(Turn.started_at,Turn.id)))
        assert all(t.prompt_version == delivery_prompt_version('ada-cadence-v1', handle='ada' if t.agent_id == ada else 'peer')
                   and len(t.context_post_ids)>=46 for t in turns)
        assert turns[1].thread_id == session.get(Post, turns[0].resulting_post_id).thread_id
        assert not list(session.scalars(select(Stimulus).where(Stimulus.run_id==rid, Stimulus.kind=='mention')))
        report = sessions.activity(Repository(session), rid)
        assert report['cadence']['peer_order'] == ['peer1', 'peer2', 'peer3']
    await engine.shutdown(); db.dispose()


@pytest.mark.asyncio
async def test_everyone_can_pass_and_operator_input_resumes_without_changing_rotation():
    db, factory = make_database()
    rid, tid, ada, peers = seed(factory, peer_count=2)
    gateway = ScriptedGateway(*([AgentAction(action='pass')]*5))
    engine = SwarmEngine(factory, gateway=gateway)
    for _ in range(7):
        await engine.step(rid)
    assert [c['agent_id'] for c in gateway.calls] == [peers[0],ada,peers[1],ada]
    with factory.begin() as session:
        repo=Repository(session);run=repo.get_run(rid)
        assert run.config['cadence_quiet'] and run.config['cadence_index']==4
        assert repo.get_thread(tid).status=='dormant'
        write=repo.create_human_post(tid, '@peer2 @ada More context to discuss.')
        _add_post_stimuli(repo,run_id=rid,thread_id=tid,post=write.post,triggering_event_id=write.event.id,
                          default_kind='human_post',default_priority=5)
        # Repeated wake requests still produce one invitation, preserving the cursor.
        cadence.on_input(repo,run)
        pending=list(session.scalars(select(Stimulus).where(Stimulus.run_id==rid,Stimulus.state=='pending')))
        assert len(pending)==1 and pending[0].target_agent_id==peers[0]
    await engine.step(rid)
    assert gateway.calls[-1]['agent_id']==peers[0]
    await engine.shutdown();db.dispose()


@pytest.mark.asyncio
async def test_peer_failure_skips_pair_and_disabled_peer_is_skipped():
    db,factory=make_database();rid,tid,ada,peers=seed(factory)
    gateway=ScriptedGateway(GatewayError('offline',category='connection'),reply(),reply(),reply())
    engine=SwarmEngine(factory,gateway=gateway)
    await engine.step(rid)
    # A disabled third peer cannot be reintroduced by the captured roster.
    with factory.begin() as session: session.get(Agent,peers[2]).enabled=False
    for _ in range(3):await engine.step(rid)
    assert [c['agent_id'] for c in gateway.calls]==[peers[0],peers[1],ada,peers[1]]
    with factory() as session:
        assert session.get(Run,rid).config['unavailable_agent_ids']==[peers[0]]
        assert list(session.scalars(select(Event).where(Event.run_id==rid,Event.event_type=='cadence.skipped')))
    await engine.shutdown();db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_at,category',[(1,'connection'),(0,'safety_block'),(1,'safety_block')])
async def test_ada_unavailability_or_provider_safety_block_stops_cadence(failure_at,category):
    db,factory=make_database();rid,tid,ada,peers=seed(factory)
    gateway=ScriptedGateway(*([reply()]*failure_at),GatewayError('blocked or unavailable',category=category))
    engine=SwarmEngine(factory,gateway=gateway)
    for _ in range(4):await engine.step(rid)
    assert len(gateway.calls)==failure_at+1
    with factory() as session: assert session.get(Run,rid).state=='failed'
    await engine.shutdown();db.dispose()


@pytest.mark.asyncio
async def test_pause_during_peer_call_then_restart_and_rerun_preserve_cadence(tmp_path):
    db,factory=make_database(f"sqlite:///{tmp_path/'cadence.db'}")
    rid,tid,ada,peers=seed(factory,peer_count=2)
    gateway=BlockingGateway(reply(),reply(),reply(),reply())
    engine=SwarmEngine(factory,gateway=gateway)
    pending=asyncio.create_task(engine.step(rid))
    await asyncio.wait_for(gateway.entered.wait(),timeout=2)
    await engine.pause(rid)
    gateway.release.set();await pending
    with factory() as session:
        assert session.get(Run,rid).state=='paused' and session.get(Run,rid).config['cadence_index']==1
    await engine.shutdown()
    engine=SwarmEngine(factory,gateway=gateway)
    await engine.recover()
    await engine.step(rid);await engine.step(rid)
    clone=await engine.rerun(rid,continuous=False)
    await engine.step(clone)
    assert [c['agent_id'] for c in gateway.calls]==[peers[0],ada,peers[1],peers[0]]
    clone_context=json.loads(gateway.calls[-1]['messages'][-1].content.split('\n',1)[1])
    assert len(clone_context['posts'])==1 and clone_context['cadence']['turn_index']==0
    await engine.shutdown();db.dispose()


@pytest.mark.asyncio
async def test_recovery_after_committed_post_advances_once_without_recalling_peer(monkeypatch):
    db,factory=make_database();rid,tid,ada,peers=seed(factory,peer_count=2)
    gateway=ScriptedGateway(reply(),reply())
    engine=SwarmEngine(factory,gateway=gateway)
    complete=engine._complete_claim
    async def interrupted(*args,**kwargs): pass
    monkeypatch.setattr(engine,'_complete_claim',interrupted)
    await engine.step(rid)
    with factory.begin() as session:
        stimulus=session.scalar(select(Stimulus).where(Stimulus.run_id==rid))
        stimulus.claimed_at=utc_now()-timedelta(hours=1)
    monkeypatch.setattr(engine,'_complete_claim',complete)
    await engine.recover()
    await engine.step(rid)
    assert [c['agent_id'] for c in gateway.calls]==[peers[0],ada]
    with factory() as session:
        assert session.get(Run,rid).config['cadence_index']==2
        assert len(list(session.scalars(select(Event).where(Event.run_id==rid,Event.event_type=='cadence.advanced'))))==2
    await engine.shutdown();db.dispose()


@pytest.mark.asyncio
async def test_api_defaults_to_free_and_keeps_cadence_mode_available(tmp_path):
    app=create_app(database_url=f"sqlite:///{tmp_path/'api.db'}",gateway=ScriptedGateway())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            with app.state.session_factory.begin() as s:
                repo = Repository(s)
                ada = repo.create_agent(handle='ada', provider='codex', model='gpt-6', persona='Ada')
            agents=(await client.get('/api/state')).json()['agents']
            peer_ids=[a['id'] for a in agents if a['handle']!='ada'][:2][::-1]
            ada_id = ada.id
            payload={'agent_ids': [*peer_ids, ada_id], 'continuous':False, 'idempotency_key':'free-default'}
            created=await client.post('/api/sessions',json=payload)
            assert created.status_code==201,created.text
            assert (await client.post('/api/sessions',json=payload)).json()==created.json()
            report=(await client.get('/api/sessions/'+created.json()['run_id'])).json()
            assert report['cadence'] is None

            payload.update(cadence=cadence.NAME, idempotency_key='cadence-explicit')
            cad=await client.post('/api/sessions',json=payload)
            report_cad=(await client.get('/api/sessions/'+cad.json()['run_id'])).json()
            assert report_cad['cadence'] is not None

            payload.update(agent_ids=peer_ids, idempotency_key='cadence-invalid')
            invalid = await client.post('/api/sessions', json=payload)
            assert invalid.status_code == 409
            assert 'requires Ada and at least one peer' in invalid.text
