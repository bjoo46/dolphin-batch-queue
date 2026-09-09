import asyncio
import copy
import heapq
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import types
import unittest

spec = importlib.util.spec_from_file_location('dolphin_queue', Path(__file__).parents[1] / 'dolphin_queue.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Queue:
    def __init__(self):
        self.mutex = threading.RLock()
        self.queue, self.running, self.history = [], [], {}

    def get_current_queue(self):
        return copy.deepcopy((self.running, self.queue))

    get_current_queue_volatile = get_current_queue

    def delete_queue_item(self, predicate):
        for item in self.queue:
            if predicate(item):
                self.queue.remove(item)
                heapq.heapify(self.queue)
                return True
        return False

    def put(self, item):
        heapq.heappush(self.queue, item)

    def get_history(self, prompt_id):
        return {prompt_id: self.history[prompt_id]} if prompt_id in self.history else {}

    def take(self):
        self.running.append(heapq.heappop(self.queue))

    def finish(self, success=True):
        item = self.running.pop()
        self.history[item[1]] = {'status': {'status_str': 'success' if success else 'error'}}


def server():
    return types.SimpleNamespace(prompt_queue=Queue(), number=0,
                                 trigger_on_prompt=lambda x: x,
                                 node_replace_manager=types.SimpleNamespace(apply_replacements=lambda x: None))


async def validate(prompt_id, prompt, targets):
    if any(n['inputs'].get('text') == 'INVALID' for n in prompt.values()):
        return False, {'message': 'invalid'}, [], {'1': 'invalid'}
    return True, None, ['1'], {}


def entry(seed, text='original'):
    return {'output': {'1': {'class_type': 'Sampler', 'inputs': {'seed': seed, 'text': text, 'strength': 1}}},
            'workflow': {'id': 'workflow-A', 'nodes': [{'id': 1, 'type': 'Sampler', 'widgets_values': [seed, text, 1]}]}}


class BatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = server()
        self.manager = module.BatchQueue(self.server, Path(self.temp.name) / 'state.json', validate)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def add(self, seeds=(11, 22, 33, 44, 55), name='A'):
        return (await self.manager.add({'name': name, 'entries': [entry(s) for s in seeds]}))['id']

    async def edit(self, batch_id, text='changed'):
        e = entry(999, text)
        return await self.manager.edit(batch_id, {'revision': self.manager.batch(batch_id)['revision'],
                                                **e, 'seed_widgets': {'1': [0]}})

    async def test_six_batches_edit_preserves_remaining_order_seeds_and_completed(self):
        ids = [await self.add(name=str(n)) for n in range(6)]
        m, q = self.manager, self.server.prompt_queue
        original_ids = [i['id'] for i in m.data['items']]
        m.data['paused'] = False
        for _ in range(5):
            await m.tick()
            q.take()
            q.finish()
        await m.tick()  # sixth is submitted, pause withdraws it before execution
        m.pause()
        self.assertFalse(q.queue)
        for batch_id in ids[1:]:
            self.assertEqual((await self.edit(batch_id))['updated'], 5)
        self.assertEqual([i['id'] for i in m.data['items']], original_ids)
        self.assertEqual([i['prompt']['1']['inputs']['seed'] for i in m.data['items']], [11,22,33,44,55]*6)
        self.assertTrue(all(i['prompt']['1']['inputs']['text'] == 'original' for i in m.data['items'][:5]))
        self.assertTrue(all(i['prompt']['1']['inputs']['text'] == 'changed' for i in m.data['items'][5:]))
        self.assertEqual([i['extra']['extra_pnginfo']['workflow']['nodes'][0]['widgets_values'][0]
                          for i in m.data['items']], [11,22,33,44,55]*6)

    async def test_pause_allows_ordinary_work_and_resumes_same_item(self):
        await self.add()
        m, q = self.manager, self.server.prompt_queue
        m.data['paused'] = False
        await m.tick()
        q.take()
        m.pause()
        q.finish()
        await m.tick()
        self.assertFalse(q.queue)
        q.put((100, 'ordinary', {}, {}, [], {}))
        m.data['paused'] = False
        await m.tick()
        self.assertEqual(len(q.queue), 1)
        q.take(); q.finish()
        await m.tick()
        self.assertEqual(q.queue[0][2]['1']['inputs']['seed'], 22)

    async def test_invalid_edit_and_seed_node_deletion_are_atomic(self):
        batch_id = await self.add()
        before = copy.deepcopy(self.manager.data)
        with self.assertRaises(ValueError):
            await self.edit(batch_id, 'INVALID')
        self.assertEqual(before, self.manager.data)
        with self.assertRaises(ValueError):
            await self.manager.edit(batch_id, {'revision': 1, 'output': {}, 'workflow': {}})
        self.assertEqual(before, self.manager.data)

    async def test_restart_preserves_pending_marks_inflight_uncertain(self):
        await self.add()
        self.manager.data['paused'] = False
        await self.manager.tick()
        restored = module.BatchQueue(server(), self.manager.path, validate)
        self.assertTrue(restored.data['paused'])
        self.assertEqual([i['state'] for i in restored.data['items']], ['uncertain'] + ['pending'] * 4)

    async def test_adopt_groups_by_workflow_and_preserves_interleaved_order(self):
        q = self.server.prompt_queue
        for n in range(30):
            e = entry(n, f'workflow-{n % 6}')
            q.put((n, str(n), e['output'], {'extra_pnginfo': {'workflow': e['workflow']}}, ['1'], {}))
        self.assertEqual(self.manager.adopt()['imported'], 30)
        self.assertFalse(q.queue)
        self.assertEqual(len(self.manager.data['batches']), 6)
        self.assertEqual([i['id'] for i in self.manager.data['items']], list(map(str, range(30))))

    async def test_failed_render_pauses_remaining(self):
        await self.add()
        self.manager.data['paused'] = False
        await self.manager.tick()
        self.server.prompt_queue.take()
        self.server.prompt_queue.finish(False)
        await self.manager.tick()
        self.assertTrue(self.manager.data['paused'])
        self.assertEqual(self.manager.data['items'][0]['state'], 'failed')
        self.assertFalse(self.server.prompt_queue.queue)

    async def test_stale_revision_rejected_and_item_differences_preserved(self):
        batch_id = (await self.manager.add({'entries': [entry(1, 'first'), entry(2, 'second')]}))['id']
        e = entry(1, 'first')
        e['output']['1']['inputs']['strength'] = .5
        await self.manager.edit(batch_id, {**e, 'revision': 1})
        self.assertEqual([i['prompt']['1']['inputs']['text'] for i in self.manager.data['items']], ['first', 'second'])
        with self.assertRaises(ValueError):
            await self.manager.edit(batch_id, {**e, 'revision': 1})

    async def test_add_failure_does_not_keep_partial_batch(self):
        with self.assertRaises(ValueError):
            await self.manager.add({'entries': [entry(1), entry(2, 'INVALID')]})
        self.assertFalse(self.manager.data['items'])
        self.assertFalse(self.manager.data['batches'])

    async def test_external_job_arrives_during_validation(self):
        await self.add()
        async def racing_validator(*args):
            self.server.prompt_queue.put((0, 'ordinary', {}, {}, [], {}))
            return await validate(*args)
        self.manager.validator = racing_validator
        self.manager.data['paused'] = False
        await self.manager.tick()
        self.assertEqual(len(self.server.prompt_queue.queue), 1)
        self.assertEqual(self.manager.data['items'][0]['state'], 'pending')

    async def test_running_item_is_not_edited(self):
        batch_id = await self.add()
        self.manager.data['paused'] = False
        await self.manager.tick()
        self.server.prompt_queue.take()
        self.manager.pause()
        result = await self.edit(batch_id)
        self.assertEqual(result['updated'], 4)
        self.assertEqual(self.manager.data['items'][0]['prompt']['1']['inputs']['text'], 'original')
        self.assertEqual(self.server.prompt_queue.running[0][2]['1']['inputs']['text'], 'original')

    async def test_invalid_last_item_does_not_partially_edit(self):
        batch_id = await self.add()
        before = copy.deepcopy(self.manager.data)
        async def fail_last(prompt_id, prompt, targets):
            if prompt['1']['inputs']['seed'] == 55:
                return False, {'message': 'last item invalid'}, [], {}
            return await validate(prompt_id, prompt, targets)
        self.manager.validator = fail_last
        with self.assertRaises(ValueError):
            await self.edit(batch_id)
        self.assertEqual(self.manager.data, before)

    async def test_adopt_save_failure_leaves_native_jobs_intact(self):
        e = entry(7)
        q = self.server.prompt_queue
        q.put((0, 'native', e['output'], {'extra_pnginfo': {'workflow': e['workflow']}}, ['1'], {}))
        def fail():
            raise OSError('disk full')
        self.manager.persist = fail
        with self.assertRaises(OSError):
            self.manager.adopt()
        self.assertEqual(q.queue[0][1], 'native')
        self.assertTrue(self.manager.data['paused'])

    async def test_nested_workflow_seed_metadata(self):
        old = {'definitions': {'subgraphs': [{'id': 'sub', 'nodes': [{'id': 9, 'type': 'Sampler', 'widgets_values': [77, 'old']}]}]}}
        edited = copy.deepcopy(old)
        edited['definitions']['subgraphs'][0]['nodes'][0]['widgets_values'] = [999, 'new']
        module.restore_seed_widgets(edited, old, {'subgraphs': {'sub': {'9': [0]}}})
        self.assertEqual(edited['definitions']['subgraphs'][0]['nodes'][0]['widgets_values'], [77, 'new'])


if __name__ == '__main__':
    unittest.main()
