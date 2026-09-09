from __future__ import annotations
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from swarmboard import autonomy, sessions
from swarmboard.app import create_app
from swarmboard.engine import SwarmEngine
from swarmboard.gateways import AgentAction, GatewayError
from swarmboard.models import Agent, Event, Experiment, Post, Run, Stimulus, Thread, Turn
from swarmboard.repository import Repository, InvalidStateError
from .test_engine_acceptance import ScriptedGateway, make_database


def seed_open(factory, **kwargs):
    with factory.begin() as session:
        repo=Repository(session)
        ada=repo.create_agent(handle='ada',provider='codex',model='gpt-6-astra',persona='Ada persona stays intact.',
                             settings={'sampling':{'reasoning_effort':'medium'}},permissions={'speak':True,'new_thread':False})
        peer=repo.create_agent(handle='peer',provider='openai_compatible',model='qwen/qwen3.8-27b',persona='Peer persona stays intact.',permissions={'speak':True})
        run=autonomy.create_session(repo,agents=[ada,peer],body='@ada, choose what to explore.',**kwargs)
        return run.id,ada.id,peer.id


@pytest.mark.asyncio
async def test_agents_route_self_replies_create_threads_and_change_threads():
    db,factory=make_database();rid,ada,peer=seed_open(factory)
    seen=[];parent_id=None
    def first(a,m):
        seen.append(a.handle)
        context=json.loads(m[-1].content.split('\n',1)[1])
        assert 'experiment' not in context and 'environment' in context
        assert 'sim_action' not in m[0].content and 'verified_successes' not in m[-1].content
        assert context['environment']['permissions']['new_thread']
        assert context['environment']['permissions']['close_threads']
        assert 'Ada persona stays intact.' in m[0].content
        return AgentAction(action='new_thread',title='Our own project',body='@peer pick your direction.',intent='clarify')
    def follow_self(a,m):
        nonlocal parent_id
        seen.append(a.handle)
        context=json.loads(m[-1].content.split('\n',1)[1]);parent_id=context['posts'][-1]['id']
        return AgentAction(action='reply',body='@peer continue this thought.',intent='support')
    def repeat_self(a,m):
        seen.append(a.handle);context=json.loads(m[-1].content.split('\n',1)[1])
        return AgentAction(action='reply',body='@peer continue this thought.',parent_post_id=context['posts'][-1]['id'],intent='support')
    def new_topic(a,m):
        seen.append(a.handle)
        return AgentAction(action='new_thread',title='Another direction',body='@ada there is another idea.',intent='clarify')
    def cross_thread(a,m):
        nonlocal parent_id
        seen.append(a.handle)
        context=json.loads(m[-1].content.split('\n',1)[1])
        assert len(context['environment']['board_threads'])==3
        other_thread = next(t for t in context['environment']['board_threads'] if t['id'] != context['thread']['id'])
        parent_id = other_thread['recent_posts'][-1]['id']
        return AgentAction(action='reply',parent_post_id=parent_id,body='@peer returning to the earlier project.',intent='support')
    gateway=ScriptedGateway(first,follow_self,repeat_self,new_topic,cross_thread)
    engine=SwarmEngine(factory,gateway=gateway)
    for _ in range(5):await engine.step(rid)
    assert seen==['ada','peer','peer','ada','peer']
    with factory() as s:
        repo=Repository(s);report=sessions.activity(repo,rid)
        assert report['metrics']['new_thread']==2 and report['metrics']['failed_turns']==0
        assert report['metrics']['turns_used']==5
        turns=list(s.scalars(select(Turn).where(Turn.run_id==rid).order_by(Turn.started_at)))
        result=s.get(Post,turns[-1].resulting_post_id)
        assert result.thread_id!=turns[-1].thread_id
        assert result.thread_id==s.get(Post,parent_id).thread_id
        assert engine._scheduler_for(repo.get_run(rid)).config.role_injection_probability==0
    await engine.shutdown();db.dispose()


@pytest.mark.asyncio
async def test_connection_failure_yields_to_other_peer_but_safety_block_stops():
    for category in ('connection','safety_block'):
        db,factory=make_database();rid,ada,peer=seed_open(factory)
        gateway=ScriptedGateway(GatewayError('failed',category=category),AgentAction(action='pass'))
        engine=SwarmEngine(factory,gateway=gateway)
        await engine.step(rid);await engine.step(rid)
        with factory() as s:
            run=s.get(Run,rid)
            if category=='connection':
                assert run.state=='paused' and len(gateway.calls)==2
                assert run.config['unavailable_agent_ids']==[ada]
                assert sessions.activity(Repository(s),rid)['metrics']['pass']==1
            else:
                assert run.state=='failed' and len(gateway.calls)==1
                with pytest.raises(InvalidStateError):await engine.rerun(rid)
        await engine.shutdown();db.dispose()


@pytest.mark.asyncio
async def test_all_passes_go_quiet_without_manufacturing_new_slots():
    db,factory=make_database();rid,_,_=seed_open(factory)
    gateway=ScriptedGateway(AgentAction(action='pass'),AgentAction(action='pass'))
    engine=SwarmEngine(factory,gateway=gateway)
    for _ in range(4):await engine.step(rid)
    with factory() as s:
        assert len(gateway.calls)==2
        assert not list(s.scalars(select(Stimulus).where(Stimulus.run_id==rid,Stimulus.state=='pending')))
        assert s.scalar(select(Thread).where(Thread.run_id==rid)).status=='dormant'
        assert s.get(Run,rid).state=='paused'
    await engine.shutdown();db.dispose()


@pytest.mark.asyncio
async def test_participant_can_close_thread_and_live_permission_revocation_holds():
    db,factory=make_database();rid,ada,peer=seed_open(factory)
    gateway=ScriptedGateway(AgentAction(action='propose_close',body='This thread is finished.',intent='synthesize'))
    engine=SwarmEngine(factory,gateway=gateway)
    await engine.step(rid);await engine.step(rid)
    with factory() as s:
        assert s.get(Run,rid).state=='completed'
        assert s.scalar(select(Thread).where(Thread.run_id==rid)).status=='closed'
        agent = sessions.session_agent(s,s.get(Run,rid),s.get(Agent,ada))
        assert agent.permissions['new_thread']
        s.get(Agent,ada).permissions={'speak':False,'new_thread':False,'close_threads':False}
        agent = sessions.session_agent(s,s.get(Run,rid),s.get(Agent,ada))
        assert not any(agent.permissions.values())
    await engine.shutdown();db.dispose()


@pytest.mark.asyncio
async def test_discard_unused_setup_removes_listings_but_preserves_audit():
    db,factory=make_database();rid,_,_=seed_open(factory)
    with factory.begin() as s:
        repo=Repository(s);old_events=list(s.scalars(select(Event.id)))
        autonomy.discard_unused(repo,rid)
        autonomy.discard_unused(repo,rid)
        assert repo.list_runs()==[] and repo.list_threads()==[]
        assert s.get(Run,rid).state=='stopped'
        assert set(old_events)<=set(s.scalars(select(Event.id)))
        assert len(list(s.scalars(select(Event).where(Event.event_type=='session.discarded'))))==1
    engine=SwarmEngine(factory,gateway=ScriptedGateway())
    with pytest.raises(InvalidStateError,match='removed'):await engine.rerun(rid)
    await engine.shutdown();db.dispose()


@pytest.mark.asyncio
async def test_used_session_cannot_be_discarded():
    db,factory=make_database();rid,_,_=seed_open(factory)
    engine=SwarmEngine(factory,gateway=ScriptedGateway(AgentAction(action='pass')))
    await engine.step(rid)
    with factory.begin() as s:
        with pytest.raises(InvalidStateError,match='unused'):autonomy.discard_unused(Repository(s),rid)
    await engine.shutdown();db.dispose()


@pytest.mark.asyncio
async def test_api_open_ended_default_and_discard_without_prepared_runs(tmp_path):
    app=create_app(database_url=f"sqlite:///{tmp_path/'autonomy.db'}",gateway=ScriptedGateway(AgentAction(action='pass')))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            state=(await client.get('/api/state')).json();assert state['runs']==[]
            page=(await client.get('/sessions')).text
            assert 'Start session' in page and 'scripted-fields' not in page
            payload={'agent_ids':[state['agents'][0]['id']],'continuous':False,'idempotency_key':'manual-open'}
            created=await client.post('/api/sessions',json=payload)
            assert created.status_code==201,created.text
            rid=created.json()['run_id'];tid=created.json()['thread_id']
            assert (await client.post('/api/sessions',json=payload)).json()==created.json()
            report=(await client.get(f'/api/sessions/{rid}')).json()
            assert not report['archived']
            assert report['metrics']['turns_used']==0
            assert (await client.post(f'/api/sessions/{rid}/discard')).json()['discarded']
            state=(await client.get(f'/api/state?thread_id={tid}')).json()
            assert state['selected_thread'] is None and state['runs']==[] and state['threads']==[]
            assert (await client.post('/api/sessions',json=payload)).status_code==409
