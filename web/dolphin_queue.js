import { app } from '../../scripts/app.js';
import { api } from '../../scripts/api.js';

const clone = (value) => structuredClone(value);
const labels = { pending: '대기', queued: '시작 대기', running: '렌더 중', done: '완료', failed: '실패', uncertain: '확인 필요' };

async function request(action = '', body) {
    const response = await api.fetchApi(`/dolphin/queue${action ? `/${action}` : ''}`, body === undefined ? {} : {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `요청 실패 (${response.status})`);
    return result;
}

function element(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
}

function seedWidgets(graph) {
    const bindings = {};
    for (const node of graph._nodes ?? []) {
        bindings[String(node.id)] = (node.widgets ?? []).flatMap((widget, index) =>
            /seed/i.test(widget.name) && typeof widget.value === 'number' ? [index] : []);
    }
    bindings.subgraphs = {};
    for (const sub of graph.subgraphs?.values?.() ?? []) bindings.subgraphs[String(sub.id)] = seedWidgets(sub);
    return bindings;
}

app.registerExtension({
    name: 'Dolphin.BatchQueue',
    async setup() {
        const css = document.createElement('link');
        css.rel = 'stylesheet';
        css.href = new URL('./dolphin_queue.css', import.meta.url).href;
        document.head.append(css);
        const panel = element('section', 'dq-panel');
        panel.setAttribute('aria-label', 'Dolphin 배치 큐');
        const head = element('header', 'dq-head');
        const heading = element('div');
        heading.append(element('span', 'dq-eyebrow', 'DOLPHIN / RENDER DESK'), element('h2', '', '배치 큐'));
        head.append(heading);
        const status = element('div', 'dq-status');
        const controls = element('div', 'dq-actions');
        const list = element('div', 'dq-list');
        const notice = element('div', 'dq-notice');
        notice.setAttribute('role', 'status');
        const form = element('form', 'dq-add');
        const name = element('input');
        name.placeholder = '배치 이름 · 예: Scene 01';
        name.setAttribute('aria-label', '배치 이름');
        name.maxLength = 120;
        const count = element('input');
        count.type = 'number'; count.min = '1'; count.max = '500'; count.value = '5';
        count.setAttribute('aria-label', '렌더 개수');
        const add = element('button', 'dq-primary', '현재 워크플로우 예약');
        add.type = 'submit';
        form.append(name, count, add);
        const foot = element('p', 'dq-foot', '보류 중에는 일반 Run으로 다른 작업을 실행할 수 있습니다. 현재 렌더는 끝까지 진행됩니다.');
        panel.append(head, status, controls, form, notice, list, foot);
        const editor = element('section', 'dq-editor');
        editor.hidden = true;
        const editorLabel = element('div');
        const editorError = element('div', 'dq-editor-error');
        editorError.setAttribute('role', 'alert');
        head.after(editor);
        let state, editing = null, busy = false, signature = '', capture = null;
        const expanded = new Set();
        const originalQueue = app.queuePrompt;
        const originalApiQueue = api.queuePrompt;

        // Reuse Comfy's complete before/after queue lifecycle for seeds and subgraphs.
        // Only the explicit capture operation is diverted; ordinary Run remains native.
        app.queuePrompt = function (...args) {
            if (capture) throw new Error('배치 예약을 저장하고 있습니다. 잠시 기다려주세요.');
            return originalQueue.apply(this, args);
        };
        api.queuePrompt = async function (number, data, options) {
            if (!capture) return originalApiQueue.call(this, number, data, options);
            if (options?.partialExecutionTargets?.length) throw new Error('전체 워크플로우로 예약하세요.');
            capture.entries.push(clone(data));
            return { prompt_id: crypto.randomUUID(), number: capture.entries.length, node_errors: {} };
        };

        function message(text, error = false) {
            notice.textContent = text;
            notice.classList.toggle('dq-error', error);
            editorError.textContent = error && editing ? text : '';
        }
        async function action(fn) {
            if (busy) return;
            busy = true;
            panel.classList.add('dq-busy');
            try { await fn(); await refresh(true); }
            catch (error) { message(error.message, true); }
            finally { busy = false; panel.classList.remove('dq-busy'); }
        }
        function button(text, fn, className = '') {
            const b = element('button', className, text);
            b.type = 'button'; b.onclick = () => action(fn);
            return b;
        }
        const pause = button('Ⅱ 일시정지', async () => {
            await request('pause', {});
            message('예약을 보류했습니다. 일반 Run으로 다른 작업을 실행해도 됩니다.');
        });
        const resume = button('▶ 재개', async () => {
            if (editing) throw new Error('편집을 저장하거나 취소한 뒤 재개하세요.');
            await request('resume', {}); message('예약 순서대로 렌더를 이어갑니다.');
        }, 'dq-primary');
        const adopt = button('기존 큐 가져오기', async () => {
            const result = await request('adopt', {});
            message(`${result.imported}개를 보류 목록으로 옮겼습니다. 시드 외 설정이 같은 작업을 배치로 묶었습니다.`);
        });
        controls.append(pause, resume, adopt);

        form.onsubmit = (event) => {
            event.preventDefault();
            action(async () => {
                const total = Number(count.value);
                if (!Number.isInteger(total) || total < 1 || total > 500) throw new Error('1~500개를 입력하세요.');
                if (editing) throw new Error('배치 편집을 먼저 저장하거나 취소하세요.');
                if (app.processingQueue) throw new Error('일반 큐 등록이 끝난 뒤 다시 시도하세요.');
                const blocker = element('div', 'dq-capture', `워크플로우 ${total}개 예약 준비 중…`);
                blocker.setAttribute('role', 'status'); document.body.append(blocker);
                capture = { entries: [] };
                let entries;
                try {
                    const ok = await originalQueue.call(app, 0, total);
                    entries = capture.entries;
                    if (!ok || entries.length !== total) throw new Error('예약 준비가 완료되지 않았습니다. 배치를 추가하지 않았습니다.');
                } finally { capture = null; blocker.remove(); }
                await request('add', { name: name.value.trim() || `워크플로우 ${(state?.batches.length ?? 0) + 1}`,
                    entries, client_id: api.clientId });
                message(`${total}개 예약 완료 · 각 항목의 시드를 저장했습니다.`);
                name.value = '';
            });
        };

        async function openBatch(batch) {
            if (editing) throw new Error('현재 배치 편집을 먼저 저장하거나 취소하세요.');
            await request('pause', {});
            const saved = await request(`batch/${batch.id}`);
            const backup = clone((await app.graphToPrompt()).workflow);
            const workflow = clone(saved.workflow);
            const token = crypto.randomUUID();
            workflow.extra ??= {};
            workflow.extra.dolphin_queue_edit = token;
            await app.loadGraphData(workflow, true, true);
            editing = { ...saved, backup, token };
            editorLabel.replaceChildren(element('span', 'dq-eyebrow', 'BATCH EDIT'), element('strong', '', saved.name),
                element('small', '', '대기 렌더에 적용 · 시드와 순서 유지'));
            editor.hidden = false; form.hidden = true;
        }
        async function finishEdit(save) {
            if (!editing) return;
            if (save) {
                const data = await app.graphToPrompt();
                if (data.workflow.extra?.dolphin_queue_edit !== editing.token) {
                    throw new Error('다른 워크플로우가 열려 있습니다. 편집 중이던 캔버스로 돌아오세요.');
                }
                delete data.workflow.extra.dolphin_queue_edit;
                const result = await request('edit', { batch_id: editing.id, revision: editing.revision,
                    ...data, seed_widgets: seedWidgets(app.rootGraph) });
                message(`${result.updated}개에 수정 내용을 저장했습니다. 재개하면 적용됩니다.`);
            }
            const backup = editing.backup;
            await app.loadGraphData(backup, true, true);
            editing = null; editor.hidden = true; form.hidden = false;
        }
        editor.append(editorLabel, button('배치에 저장', () => finishEdit(true), 'dq-primary'),
            button('편집 취소', () => finishEdit(false)), editorError);

        function render() {
            const items = state.items;
            const done = items.filter(i => i.state === 'done').length;
            const waiting = items.filter(i => i.state === 'pending').length;
            status.replaceChildren(element('span', `dq-dot ${state.paused ? '' : 'active'}`),
                element('strong', '', state.paused ? '예약 보류 중' : '예약 실행 중'),
                element('span', '', `${done} 완료 / ${waiting} 대기 / ${items.length} 전체`));
            pause.disabled = state.paused;
            resume.disabled = !state.paused || !!editing || !waiting;
            list.replaceChildren();
            if (!state.batches.length) {
                const empty = element('div', 'dq-empty');
                empty.append(element('span', '', '◷'), element('h3', '', '밤새 돌릴 작업을, 여기에.'),
                    element('p', '', '워크플로우를 열고 개수를 정해 예약하세요. 나중에 배치를 열어 남은 작업을 한 번에 고칠 수 있습니다.'));
                list.append(empty);
            }
            for (const [index, batch] of state.batches.entries()) {
                const jobs = items.filter(i => i.batch_id === batch.id);
                const pending = jobs.filter(i => i.state === 'pending').length;
                const completed = jobs.filter(i => i.state === 'done').length;
                const running = jobs.some(i => ['running', 'queued'].includes(i.state));
                const card = element('article', `dq-card${running ? ' dq-running' : ''}`);
                const top = element('div', 'dq-card-head');
                top.append(element('span', 'dq-number', String(index + 1).padStart(2, '0')),
                    element('strong', '', batch.name), element('span', 'dq-revision', `v${batch.revision}`));
                const progress = element('progress'); progress.max = jobs.length || 1; progress.value = completed;
                const meta = element('div', 'dq-meta', `${completed}/${jobs.length} 완료 · ${pending} 대기${running ? ' · 렌더 중' : ''}`);
                const actions = element('div', 'dq-card-actions');
                const edit = button('워크플로우 수정', () => openBatch(batch)); edit.disabled = !pending || !!editing;
                const details = element('details'); details.open = expanded.has(batch.id);
                details.ontoggle = () => details.open ? expanded.add(batch.id) : expanded.delete(batch.id);
                details.append(element('summary', '', '항목 · 시드 보기'));
                for (const job of jobs) {
                    const row = element('div', 'dq-job');
                    row.append(element('span', `dq-state dq-${job.state}`, `${items.indexOf(job) + 1}. ${labels[job.state]}`),
                        element('code', '', Object.entries(job.seeds).map(([key, value]) => `${key}: ${value}`).join(' / ') || '시드 입력 없음'));
                    if (job.error) row.append(element('small', 'dq-error', job.error));
                    if (['failed', 'uncertain'].includes(job.state)) row.append(button('출력 확인 후 재시도', async () => {
                        await request('retry', { id: job.id }); message('기존 시드로 대기 목록에 복원했습니다.');
                    }));
                    details.append(row);
                }
                const remove = button('삭제', async () => {
                    if (!window.confirm(`“${batch.name}” 배치와 남은 예약을 삭제할까요?`)) return;
                    await request('remove', { batch_id: batch.id });
                }, 'dq-quiet'); remove.disabled = running || !!editing;
                actions.append(edit, remove);
                card.append(top, progress, meta, actions, details); list.append(card);
            }
            if (state.error) message(state.error, true);
        }
        async function refresh(force = false) {
            const next = await request();
            state = next;
            const nextSignature = JSON.stringify(next);
            if (force || nextSignature !== signature) { signature = nextSignature; render(); }
        }
        app.extensionManager.registerSidebarTab({
            id: 'dolphin-batch-queue',
            icon: 'pi pi-clock',
            title: '배치 큐',
            tooltip: 'Dolphin · 배치 큐',
            type: 'custom',
            render: (container) => {
                container.style.height = '100%';
                container.style.minWidth = '0';
                container.replaceChildren(panel);
                action(() => refresh(true));
            },
        });
        const poll = async () => {
            try { if (!busy) await refresh(); }
            catch (error) { message(`연결 대기 중 · ${error.message}`, true); }
            setTimeout(poll, 2000);
        };
        poll();
    },
});
