"""Editable, durable batches; only one managed render enters Comfy's queue."""
import asyncio
import copy
import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path

from aiohttp import web


def is_seed(name, value):
    return 'seed' in name.lower() and isinstance(value, int) and not isinstance(value, bool)


def merge_edit(base, edited, original):
    """Apply changed fields only, preserving each render's unchanged values."""
    if base == edited:
        return copy.deepcopy(original)
    if all(isinstance(v, dict) for v in (base, edited, original)):
        return {k: merge_edit(base[k], v, original.get(k, base[k]))
                if k in base else copy.deepcopy(v) for k, v in edited.items()}
    if all(isinstance(v, list) for v in (base, edited, original)):
        if all(all(isinstance(x, dict) and 'id' in x for x in v) for v in (base, edited, original)):
            b, o = ({str(x['id']): x for x in v} for v in (base, original))
            return [merge_edit(b[str(x['id'])], x, o.get(str(x['id']), b[str(x['id'])]))
                    if str(x['id']) in b else copy.deepcopy(x) for x in edited]
        if len(base) == len(edited) == len(original):
            return [merge_edit(b, e, o) for b, e, o in zip(base, edited, original)]
    return copy.deepcopy(edited)


def edit_prompt(base, edited, original):
    result = merge_edit(base, edited, original)
    for node_id, node in original.items():
        for key, value in node.get('inputs', {}).items():
            if not is_seed(key, value):
                continue
            target = result.get(node_id)
            if (not target or target['class_type'] != node['class_type']
                    or not is_seed(key, target.get('inputs', {}).get(key))):
                raise ValueError(f'시드 보존 불가: 노드 {node_id} / {key}. 해당 노드와 시드 입력을 유지하세요.')
            target['inputs'][key] = value
    return result


class BatchQueue:
    def __init__(self, server, path, validator):
        self.server, self.path, self.validator = server, Path(path), validator
        self.lock = asyncio.Lock()
        self.task = None
        self.data = {'paused': True, 'batches': [], 'items': [], 'error': ''}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding='utf-8'))
            self.data['paused'] = True
            for item in self.data['items']:
                if item['state'] in ('queued', 'running'):
                    item['state'] = 'uncertain'
                    item['error'] = '서버가 재시작되었습니다. 출력 확인 후 재시도하세요.'

    def persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        with temp.open('w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, self.path)

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.run())

    def batch(self, batch_id):
        return next(b for b in self.data['batches'] if b['id'] == batch_id)

    async def prepare(self, prompt, extra, prompt_id):
        prompt, extra = copy.deepcopy((prompt, extra))
        for key in ('auth_token_comfy_org', 'api_key_comfy_org'):
            if extra.pop(key, None):
                raise ValueError('Comfy 인증이 필요한 작업은 기본 큐에서 실행하세요.')
        self.server.node_replace_manager.apply_replacements(prompt)
        valid = await self.validator(prompt_id, prompt, None)
        if not valid[0]:
            raise ValueError(json.dumps({'error': valid[1], 'node_errors': valid[3]}, ensure_ascii=False))
        return prompt, extra, valid[2]

    async def add(self, body):
        entries = body['entries']
        if not isinstance(entries, list) or not 1 <= len(entries) <= 500:
            raise ValueError('배치당 1~500개를 예약할 수 있습니다.')
        batch_id = str(uuid.uuid4())
        batch = {'id': batch_id, 'name': str(body.get('name') or 'Untitled')[:120],
                 'revision': 1, 'workflow': entries[0]['workflow'], 'prompt': entries[0]['output']}
        items = []
        for entry in entries:
            prompt_id = str(uuid.uuid4())
            extra = {'extra_pnginfo': {'workflow': entry['workflow']},
                     'client_id': body.get('client_id', '')}
            body_with_hooks = self.server.trigger_on_prompt(copy.deepcopy({'prompt': entry['output'], 'extra_data': extra}))
            prompt, extra, _ = await self.prepare(body_with_hooks['prompt'], body_with_hooks.get('extra_data', {}), prompt_id)
            items.append({'id': prompt_id, 'batch_id': batch_id, 'state': 'pending',
                          'prompt': prompt, 'extra': extra, 'revision': 1, 'error': ''})
        self.data['batches'].append(batch)
        self.data['items'].extend(items)
        self.persist()
        return {'id': batch_id}

    def pause(self):
        q = self.server.prompt_queue
        with q.mutex:
            self.data['paused'] = True
            # The queue mutex closes the gap between pause and the worker taking a job.
            for item in self.data['items']:
                if item['state'] == 'queued':
                    if q.delete_queue_item(lambda x: x[1] == item['id']):
                        item['state'] = 'pending'
            self.persist()

    def adopt(self):
        q = self.server.prompt_queue
        with q.mutex:
            self.pause()
            pending = sorted(q.get_current_queue()[1], key=lambda x: x[0])
            groups = {}
            for entry in pending:
                if ((len(entry) > 5 and any(entry[5].values()))
                        or any(entry[3].get(k) for k in ('auth_token_comfy_org', 'api_key_comfy_org'))):
                    raise ValueError('인증 정보가 포함된 작업은 가져올 수 없습니다. 일반 큐에서 실행하세요.')
                workflow = entry[3].get('extra_pnginfo', {}).get('workflow')
                if not workflow:
                    raise ValueError('편집용 워크플로우 정보가 없는 작업이 있습니다.')
            before = copy.deepcopy(self.data)
            try:
                for entry in pending:
                    prompt, extra = copy.deepcopy(entry[2:4])
                    normalized = copy.deepcopy(prompt)
                    for node in normalized.values():
                        for key, value in node.get('inputs', {}).items():
                            if is_seed(key, value):
                                node['inputs'][key] = '<seed>'
                    workflow = extra['extra_pnginfo']['workflow']
                    key = (workflow.get('id'), hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest())
                    if key not in groups:
                        batch = {'id': str(uuid.uuid4()), 'name': f'가져온 워크플로우 {len(groups) + 1}',
                                 'revision': 1, 'workflow': workflow, 'prompt': prompt}
                        self.data['batches'].append(batch)
                        groups[key] = batch
                    batch = groups[key]
                    self.data['items'].append({'id': entry[1], 'batch_id': batch['id'],
                                              'state': 'pending', 'prompt': prompt, 'extra': extra,
                                              'revision': 1, 'error': ''})
                self.persist()
            except Exception:
                self.data = before
                raise
            for entry in pending:
                q.delete_queue_item(lambda x: x[1] == entry[1])
        return {'imported': len(pending)}

    async def edit(self, batch_id, body):
        if not self.data['paused']:
            raise ValueError('먼저 큐를 일시정지하세요.')
        batch = self.batch(batch_id)
        if body['revision'] != batch['revision']:
            raise ValueError('다른 창에서 수정되었습니다. 배치를 다시 열어주세요.')
        changes = []
        for item in self.data['items']:
            if item['batch_id'] != batch_id or item['state'] != 'pending':
                continue
            prompt = edit_prompt(batch['prompt'], body['output'], item['prompt'])
            extra = copy.deepcopy(item['extra'])
            workflow = merge_edit(batch['workflow'], body['workflow'], extra['extra_pnginfo']['workflow'])
            # Keep seed widgets in the saved graph as well as in the executable prompt.
            restore_seed_widgets(workflow, extra['extra_pnginfo']['workflow'], body.get('seed_widgets', {}))
            extra['extra_pnginfo']['workflow'] = workflow
            await self.prepare(prompt, extra, item['id'])
            changes.append((item, prompt, extra))
        if not changes:
            raise ValueError('이 배치에 수정할 대기 항목이 없습니다.')
        for item, prompt, extra in changes:
            item.update(prompt=prompt, extra=extra, revision=batch['revision'] + 1)
        batch.update(prompt=body['output'], workflow=body['workflow'], revision=batch['revision'] + 1)
        self.persist()
        return {'updated': len(changes)}

    async def tick(self):
        q = self.server.prompt_queue
        with q.mutex:
            running, queued = q.get_current_queue_volatile()
            running_ids, queued_ids = ({x[1] for x in v} for v in (running, queued))
            changed = False
            for item in self.data['items']:
                if item['state'] not in ('queued', 'running'):
                    continue
                previous = item['state']
                if item['id'] in running_ids:
                    item['state'] = 'running'
                elif item['id'] not in queued_ids:
                    history = q.get_history(item['id']).get(item['id'])
                    status = (history or {}).get('status') or {}
                    item['state'] = 'done' if status.get('status_str') == 'success' else 'failed'
                    if item['state'] == 'failed':
                        item['error'] = '실행 실패 또는 큐에서 제거됨. 출력 확인 후 재시도하세요.'
                        self.data['paused'] = True
                changed |= item['state'] != previous
            if changed:
                self.persist()
        if self.data['paused'] or running or queued:
            return
        item = next((i for i in self.data['items'] if i['state'] == 'pending'), None)
        if item is None:
            return
        prompt, extra, outputs = await self.prepare(item['prompt'], item['extra'], item['id'])
        with q.mutex:
            # A normal Run may have arrived while validation awaited.
            running, queued = q.get_current_queue_volatile()
            if running or queued:
                return
            item['state'] = 'queued'
            self.persist()  # On a crash, never silently render this item twice.
            extra['create_time'] = int(time.time() * 1000)
            number = self.server.number
            self.server.number += 1
            q.put((number, item['id'], prompt, extra, outputs, {}))

    async def run(self):
        while True:
            try:
                async with self.lock:
                    await self.tick()
            except Exception as error:
                self.data['paused'] = True
                self.data['error'] = str(error)
                logging.exception('[Dolphin Queue] Paused after error')
            await asyncio.sleep(0.5)

    def summary(self):
        return {'paused': self.data['paused'], 'error': self.data['error'],
                'batches': [{k: b[k] for k in ('id', 'name', 'revision')} for b in self.data['batches']],
                'items': [{**{k: i[k] for k in ('id', 'batch_id', 'state', 'revision', 'error')},
                           'seeds': {f'{n}.{k}': str(v) for n, node in i['prompt'].items()
                                     for k, v in node.get('inputs', {}).items() if is_seed(k, v)}}
                          for i in self.data['items']]}


def restore_seed_widgets(workflow, original, bindings):
    old = {str(n['id']): n for n in original.get('nodes', [])}
    for node in workflow.get('nodes', []):
        previous = old.get(str(node['id']))
        if previous and previous.get('type') == node.get('type'):
            for index in bindings.get(str(node['id']), []):
                if index < len(previous.get('widgets_values', [])) and index < len(node.get('widgets_values', [])):
                    node['widgets_values'][index] = previous['widgets_values'][index]
    old_sub = {str(s['id']): s for s in original.get('definitions', {}).get('subgraphs', [])}
    for sub in workflow.get('definitions', {}).get('subgraphs', []):
        if str(sub['id']) in old_sub:
            restore_seed_widgets(sub, old_sub[str(sub['id'])], bindings.get('subgraphs', {}).get(str(sub['id']), {}))


def register():
    from server import PromptServer
    import execution

    server = PromptServer.instance
    manager = BatchQueue(server, Path(__file__).parent / '.dolphin_queue' / 'batches.json', execution.validate_prompt)

    @server.routes.get('/dolphin/queue')
    async def state(request):
        manager.start()
        async with manager.lock:
            return web.json_response(manager.summary())

    @server.routes.get('/dolphin/queue/batch/{batch_id}')
    async def get_batch(request):
        async with manager.lock:
            try:
                return web.json_response(manager.batch(request.match_info['batch_id']))
            except StopIteration:
                raise web.HTTPNotFound()

    @server.routes.post('/dolphin/queue/{action}')
    async def action(request):
        manager.start()
        async with manager.lock:
            before = copy.deepcopy(manager.data)
            try:
                name = request.match_info['action']
                body = await request.json()
                result = {}
                if name == 'add':
                    result = await manager.add(body)
                elif name == 'pause':
                    manager.pause()
                elif name == 'resume':
                    manager.data.update(paused=False, error='')
                    manager.persist()
                elif name == 'adopt':
                    result = manager.adopt()
                elif name == 'edit':
                    result = await manager.edit(body['batch_id'], body)
                elif name == 'retry':
                    if not manager.data['paused']:
                        raise ValueError('먼저 큐를 일시정지하세요.')
                    item = next(i for i in manager.data['items'] if i['id'] == body['id'])
                    if item['state'] not in ('failed', 'uncertain'):
                        raise ValueError('실패하거나 확인이 필요한 항목만 재시도할 수 있습니다.')
                    item.update(id=str(uuid.uuid4()), state='pending', error='')
                    manager.persist()
                elif name == 'remove':
                    ids = {i['id'] for i in manager.data['items'] if i['batch_id'] == body['batch_id']}
                    if any(i['id'] in ids and i['state'] in ('queued', 'running') for i in manager.data['items']):
                        raise ValueError('실행 중인 배치는 삭제할 수 없습니다.')
                    manager.data['items'] = [i for i in manager.data['items'] if i['id'] not in ids]
                    manager.data['batches'] = [b for b in manager.data['batches'] if b['id'] != body['batch_id']]
                    manager.persist()
                else:
                    raise ValueError('알 수 없는 작업입니다.')
                return web.json_response(result)
            except (ValueError, KeyError, StopIteration, TypeError) as error:
                if name not in ('pause', 'adopt'):
                    manager.data = before
                return web.json_response({'error': str(error)}, status=400)
            except OSError as error:
                if name not in ('pause', 'adopt'):
                    manager.data = before
                manager.data['paused'] = True
                return web.json_response({'error': f'저장 실패: {error}'}, status=500)
