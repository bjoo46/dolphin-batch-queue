# Dolphin Batch Queue

Editable workflow batches in the ComfyUI sidebar. Queue six different workflows five times each, pause the remaining renders, revise a batch, and resume with the original per-item seeds and order.

[한국어 사용법](README.ko.md)

## Features

- A native sidebar tab, with batch cards and per-item seed lists.
- Pause managed jobs while the current render finishes. Use normal Run for urgent work, then resume the saved queue.
- Edit a batch on the canvas and apply changes to all pending items after validation.
- Preserve each item's seeds and unchanged input differences. Completed and running items keep their original settings.
- Import pending jobs from the native queue without changing their execution order.
- Persist batches locally. After a server restart, recover paused; mark in-flight items for review rather than automatically running them twice.

## Install

Version **0.1.0** is published on [Comfy Registry](https://registry.comfy.org/publishers/vzoo/nodes/dolphin-batch-queue) under publisher **aidispo (@vzoo)**.

```sh
comfy node install dolphin-batch-queue
```

Alternatively, clone into `ComfyUI/custom_nodes`:

```sh
git clone https://github.com/bjoo46/dolphin-batch-queue.git
```

Restart ComfyUI and refresh the browser. Open **배치 큐** (clock icon) in the native sidebar. This is a UI extension, so it does not add canvas nodes. The current interface is Korean.

Requires a recent ComfyUI with async prompt validation, `node_replace_manager`, and a frontend providing `extensionManager.registerSidebarTab` and `app.rootGraph`. Development used ComfyUI 0.34.2; earlier versions have not been verified.

## Use

1. Open a workflow, enter a batch name and count, then click **현재 워크플로우 예약** (reserve current workflow).
2. Repeat for other workflows. Click **재개** (resume) to execute the saved order.
3. Click **일시정지** (pause) to hold pending managed jobs. Ordinary ComfyUI jobs remain available.
4. Click **워크플로우 수정** (edit workflow), make changes on the canvas, then **배치에 저장** (save batch). The prior canvas is restored. Resume explicitly when ready.

At reservation time, native fixed/increment/randomize controls determine the seeds. Once reserved, every item retains those seeds, even if you change seed widgets while editing. Changing inputs upstream of a node can invalidate its cache; pausing alone does not clear caches.

## Limits and storage

- Intended for a trusted local, single-user server. Queue state is shared across clients.
- Only pending items are edited. Restore failed items to pending before editing them.
- Removing or replacing a seeded node, or turning its seed into a connection, is rejected to avoid silently losing per-item seeds.
- Import groups by workflow ID and executable inputs except numeric seed inputs; native queue entries do not encode the original batch boundaries.
- Partner-node Comfy authentication tokens are not stored or renewed. Use the native queue for those jobs.
- Batches are stored in `.dolphin_queue/batches.json` inside this extension. Back up that folder before uninstalling. Workflow inputs are stored as provided; do not share the state file.
- Do not install alongside the original `dolphin_nodes` bundled Batch Queue: both register the same routes and frontend extension. Use one copy. For migration, stop ComfyUI and back up the existing queue state first.

## Validation

13 automated queue tests and an isolated browser fixture cover batch editing, seed/order preservation, pause/resume, restart recovery and failure handling. Full GPU rendering and broad frontend compatibility have not yet been verified.

```sh
python -m unittest discover -s tests -p test_dolphin_queue.py -v
node --check web/dolphin_queue.js
```

For the GPU-free UI fixture, run `python tests/queue_browser_harness.py` and open `http://127.0.0.1:8199`.

[Publishing instructions](PUBLISHING.md) · [MIT license](LICENSE)
