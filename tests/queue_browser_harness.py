"""Local UI fixture: python tests/queue_browser_harness.py (no GPU or real queue)."""
import sys
import tempfile
import types
from pathlib import Path
from aiohttp import web
sys.path.insert(0, str(Path(__file__).parent))
from test_dolphin_queue import module, server, validate

fixture_server = server()
fixture_server.routes = web.RouteTableDef()
sys.modules['server'] = types.SimpleNamespace(PromptServer=types.SimpleNamespace(instance=fixture_server))
sys.modules['execution'] = types.SimpleNamespace(validate_prompt=validate)
original_init = module.BatchQueue.__init__
temporary = tempfile.TemporaryDirectory()


def fixture_init(self, server, path, validator):
    original_init(self, server, Path(temporary.name) / 'batches.json', validator)


module.BatchQueue.__init__ = fixture_init
module.register()
routes = fixture_server.routes


@routes.get('/')
async def index(request):
    return web.Response(content_type='text/html', text='''<!doctype html><html><meta charset="utf-8">
<title>Dolphin Queue · UI Test</title><style>body{margin:0;background:#10151c;color:#dae4ed;font:14px Segoe UI}
body{display:flex;height:100vh;overflow:hidden}nav{width:48px;flex-shrink:0;background:#202832}nav button{padding:14px 8px;background:none;color:white;border:0;cursor:pointer}aside{width:360px;flex-shrink:0;height:100%;resize:horizontal;overflow:auto}aside[hidden]{display:none}main{padding:50px;flex:1;min-width:0}label{display:block;margin:20px 0}input{display:block;background:#24303e;border:1px solid #566477;border-radius:8px;color:white;padding:12px;width:300px;margin-top:8px}h1{font-weight:500}</style>
<nav></nav><aside hidden></aside><main><p>DOLPHIN / TEST CANVAS</p><h1>워크플로우 편집</h1><label>프롬프트<input id="text" value="original"></label>
<label>시드<input id="seed" type="number" value="100"></label><p>샘플러 → 이미지 저장</p></main>
<script type="module" src="/extensions/dolphin_nodes/dolphin_queue.js"></script></html>''')


@routes.get('/scripts/api.js')
async def api_js(request):
    return web.Response(content_type='text/javascript', text='''export const api = {
clientId:'ui-test', fetchApi: (url, options) => fetch(url, options),
queuePrompt: async () => { throw new Error('Unexpected real submission'); }
};''')


@routes.get('/scripts/app.js')
async def app_js(request):
    return web.Response(content_type='text/javascript', text='''import {api} from './api.js';
let extra = {}, workflowId = 'fixture';
const text = document.querySelector('#text'), seed = document.querySelector('#seed');
export const app = {
rootGraph: {_nodes:[{id:1,widgets:[{name:'seed',get value(){return Number(seed.value)}},{name:'text',get value(){return text.value}}]}]},
registerExtension: extension => extension.setup(),
extensionManager: {registerSidebarTab(tab){const button=document.createElement('button');button.textContent='◷';button.setAttribute('aria-label',tab.title);document.querySelector('nav').append(button);button.onclick=()=>{const host=document.querySelector('aside');host.hidden=!host.hidden;if(!host.hidden)tab.render(host)}}},
graphToPrompt: async () => ({workflow:{id:workflowId,extra,nodes:[{id:1,type:'Sampler',widgets_values:[Number(seed.value),text.value]}]},
 output:{'1':{class_type:'Sampler',inputs:{seed:Number(seed.value),text:text.value}}}}),
loadGraphData: async workflow => {extra=workflow.extra||{};workflowId=workflow.id;seed.value=workflow.nodes[0].widgets_values[0];text.value=workflow.nodes[0].widgets_values[1]},
queuePrompt: async function(number,count){this.processingQueue=true;try{for(let i=0;i<count;i++){await api.queuePrompt(number,await this.graphToPrompt());seed.value=Number(seed.value)+1}return true}finally{this.processingQueue=false}}
};''')


routes.static('/extensions/dolphin_nodes', Path(__file__).parents[1] / 'web')
app = web.Application()
app.add_routes(routes)
web.run_app(app, host='127.0.0.1', port=8199, print=None)
