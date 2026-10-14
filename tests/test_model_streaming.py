import asyncio
import json
import httpx
import pytest
from brain import accounting
from brain.model import OllamaModel
from brain.repository import inspect
from brain.contracts import Proposal
from brain.validators import ProposalInvalid
from test_engine import world, with_fake_brain, git  # noqa: F401
from test_desktop_engine import tmp_path  # noqa: F401


class Stream(httpx.AsyncByteStream):
    def __init__(self, frames):
        self.frames, self.closed = frames, False
    async def __aiter__(self):
        for delay, frame in self.frames:
            await asyncio.sleep(delay)
            yield (json.dumps(frame) + '\n').encode()
    async def aclose(self):
        self.closed = True


def run_stream(frames, **settings):
    stream = Stream(frames)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                httpx.Response(200, stream=stream))) as client:
            model = OllamaModel('http://ollama', 'test', **settings)
            return await model._post(client, 'implementer', {'model': 'test', 'messages': [], 'options': {}})
    return stream, run


def test_stream_measures_output_and_preserves_final_usage_without_leaking_contents():
    stream, run = run_stream([(0, {'message': {'content':'private'}}),
        (0, {'message': {'content':' code'}, 'done':True, 'eval_count':2, 'prompt_eval_count':8})])
    events = []
    with accounting.bind(lambda kind, **data: events.append((kind, data))):
        response = asyncio.run(run())
    assert response.json()['message']['content'] == 'private code'
    assert response.json()['eval_count'] == 2 and stream.closed
    assert any(kind == 'model_progress' and data['status'] == 'RUNNING' for kind,data in events)
    assert 'private' not in str(events)


@pytest.mark.parametrize('mode', ['first', 'idle', 'total', 'incomplete'])
def test_stalled_or_incomplete_stream_is_closed_and_refused(mode):
    frames = [(.05, {'message':{'content':'x'}, 'done':True})]
    settings = {'first_token_timeout':.01}
    if mode == 'idle':
        frames = [(0, {'message':{'content':'x'}}), (.05, {'done':True})]
        settings = {'token_idle_timeout':.01}
    if mode == 'total':
        frames = [(.008, {'message':{'content':'x'}})]*10
        settings = {'timeout':.02}
    if mode == 'incomplete':
        frames, settings = [(0, {'message':{'content':'x'}})], {}
    stream, run = run_stream(frames, **settings)
    with pytest.raises((httpx.ReadTimeout, httpx.ReadError)):
        asyncio.run(run())
    assert stream.closed


def test_exact_edits_preserve_large_file_and_do_not_apply_before_approval(world, monkeypatch):
    original = '# Keep this unrelated line\n'*1200 + 'x = 1\n'
    (world['root']/'main.py').write_text(original)
    git(world['root'], 'add', 'main.py')
    git(world['root'], 'commit', '-qm', 'Large existing file')
    brain = with_fake_brain(world, monkeypatch)
    async def propose(*args, **kwargs):
        return json.dumps({'plan':'Fix x', 'changes':[], 'edits':[{'path':'main.py','old':'x = 1','new':'x = 2'}]})
    monkeypatch.setattr(brain.model, 'propose', propose)
    task = world['engine'].call('tasks.start', {'project_id':world['project']['id'],'goal':'Fix x'})
    assert task['status'] == 'proposed' and '+x = 2' in task['diff']
    assert (world['root']/'main.py').read_text() == original
    assert brain.store.get(task['id'])['proposal']['changes'][0]['content'] == original.replace('x = 1', 'x = 2')
    assert 'edits' not in brain.store.get(task['id'])['proposal']


def test_requirement_timeout_is_failed_instead_of_completed(world, monkeypatch):
    brain = with_fake_brain(world, monkeypatch)
    brain.requirement_checks = True
    async def stalled(*a, **k):
        raise httpx.ReadTimeout('No model output for 180s')
    monkeypatch.setattr(brain.model, 'propose', stalled)
    with pytest.raises(Exception, match='No model output'):
        world['engine'].call('tasks.start', {'project_id':world['project']['id'],'goal':'Fix x'})
    task = brain.store.tasks()[0]
    assert task['status'] == 'blocked'
    assert not any(e['kind'] == 'requirement_checks_skipped' for e in task.get('events',[]))


def test_source_ranges_and_search_skip_denied_files(tmp_path):
    (tmp_path/'main.py').write_text('a = 1\nb = 2\nc = 3\n')
    (tmp_path/'.env').write_text('SECRET=value')
    assert inspect(tmp_path, 'read_file', {'path':'main.py','start_line':2,'max_lines':1}) == 'b = 2\n'
    assert 'main.py:2: b = 2' in inspect(tmp_path, 'search', {'query':'b = 2'})


def test_cancellation_closes_only_the_current_stream():
    stream, run = run_stream([(0, {'message':{'content':'x'}}), (1, {'done':True})])
    async def cancel():
        task = asyncio.create_task(run())
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(cancel())
    assert stream.closed


def test_bad_edits_are_rejected_with_source_feedback_without_changing_the_proposal(world, monkeypatch):
    brain = with_fake_brain(world, monkeypatch)
    task = world['engine'].call('tasks.start', {'project_id':world['project']['id'],'goal':'Fix x'})
    stored = brain.store.get(task['id'])
    bad = Proposal(plan='Fix x', changes=[], edits=[{'path':'main.py','old':'x=1','new':'x=2'}])
    with pytest.raises(ProposalInvalid) as refused:
        brain.store_proposal(stored, brain.workspace(task['id']), bad, 'implementer')
    assert 'x = 1' in refused.value.excerpt
    assert brain.store.get(task['id'])['digest'] == task['digest']
    assert (world['root']/'main.py').read_text() == 'x = 1\n'


def test_line_edits_are_ordered_preserve_unrelated_source_and_refuse_overlap(world, monkeypatch):
    brain = with_fake_brain(world, monkeypatch)
    task = world['engine'].call('tasks.start', {'project_id':world['project']['id'],'goal':'Fix x'})
    workspace = brain.workspace(task['id'])
    (workspace/'main.py').write_text('a = 1\nb = 2\nc = 3\n')
    plan = Proposal(plan='Change two lines', changes=[], edits=[
        {'path':'main.py','start_line':1,'end_line':1,'new':'a = 4\na2 = 5'},
        {'path':'main.py','start_line':3,'end_line':3,'new':'c = 6'}])
    brain.store_proposal(brain.store.get(task['id']), workspace, plan, 'implementer')
    content = brain.store.get(task['id'])['proposal']['changes'][0]['content']
    assert content == 'a = 4\na2 = 5\nb = 2\nc = 6\n'
    assert (workspace/'main.py').read_text() == 'a = 1\nb = 2\nc = 3\n'
    overlapping = Proposal(plan='Bad ranges', changes=[], edits=[
        {'path':'main.py','start_line':1,'end_line':2,'new':'a = 4'},
        {'path':'main.py','start_line':2,'end_line':3,'new':'b = 5'}])
    with pytest.raises(ProposalInvalid, match='overlaps'):
        brain.store_proposal(brain.store.get(task['id']), workspace, overlapping, 'implementer')


def test_large_context_is_bounded_without_truncating_the_user_goal(tmp_path, monkeypatch):
    captured = []
    goal = 'Fix the requested behavior. ' + 'Keep all constraints intact. '*140
    real_client = httpx.AsyncClient
    def response(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={'done':True, 'message':{'content':json.dumps({'plan':'Read-only', 'changes':[]})}})
    monkeypatch.setattr('brain.model.httpx.AsyncClient', lambda **kwargs:
        real_client(transport=httpx.MockTransport(response), **kwargs))
    model = OllamaModel('http://ollama', 'test')
    asyncio.run(model.propose(tmp_path, goal, [], {'source':'x'*100000}))
    body = captured[0]
    user = json.loads(body['messages'][1]['content'])
    assert user['goal'] == goal and user['repository_context']['truncated']
    assert body['stream'] and body['options']['num_ctx'] == 8192
    assert sum(len(m.get('content','')) for m in body['messages']) + len(json.dumps(body['tools'])) < (8192-2048)*3
