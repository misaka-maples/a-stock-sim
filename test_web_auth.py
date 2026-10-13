"""Execute the inline UI JavaScript with an offline DOM/fetch harness."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest


def test_ui_login_gates_requests_and_401_stops_polling(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the offline JavaScript UI check")
    html = (Path(__file__).parent / "web" / "index.html").read_text()
    scripts = re.findall(r"<script[^>]*>([\s\S]*?)</script>", html)
    assert len(scripts) == 1
    source = scripts[0].replace("    initializeAuth();", "")
    source_file = tmp_path / "ui.js"
    source_file.write_text(source)
    harness = tmp_path / "check.js"
    harness.write_text(r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const elements = new Map(), calls = [], timers = new Set();
let authenticated = false, failNext = false, timerCounter = 0;
function element(id) {
  if (!elements.has(id)) elements.set(id, {style:{}, value:'', dataset:{}, disabled:false,
    textContent:'', innerText:'', innerHTML:'', classList:{add(){},remove(){}},
    addEventListener(){}, focus(){}, appendChild(){}, remove(){}});
  return elements.get(id);
}
const response = (status, value) => ({status, ok:status===200, json:async()=>value});
const fetch = async (url, options={}) => {
  calls.push([url, options]);
  assert.equal(options.credentials, 'same-origin');
  if (url === '/api/auth/status') return response(200, {auth_required:true, authenticated});
  if (url === '/api/auth/login') {
    assert.deepEqual(JSON.parse(options.body), {password:'offline-password'});
    authenticated = true;
    return response(200, {success:true});
  }
  if (url === '/api/auth/logout') {authenticated=false;return response(200,{success:true});}
  if (failNext) {failNext=false;return response(401,{detail:'session expired'});}
  assert(authenticated, 'protected fetch before login');
  return response(200, {default_end:'2026-09-21'});
};
const sandbox = {window:{fetch}, document:{getElementById:element, querySelectorAll:()=>[],
  addEventListener(){},createElement:()=>element('created'),body:{appendChild(){}}},
  console, URL, setTimeout:()=>1, clearTimeout(){},
  setInterval(){const id=++timerCounter;timers.add(id);return id;},
  clearInterval(id){timers.delete(id);}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox);
vm.runInContext('initChartInteractions=()=>{};initBacktestChartInteractions=()=>{};fetchData=async()=>{};fetchNotifyStatus=async()=>{};',sandbox);
(async()=>{
  await sandbox.initializeAuth();
  assert.equal(element('authOverlay').style.display,'flex');
  assert.equal(calls.length,1);
  await assert.rejects(sandbox.apiFetch('/api/summary'));
  assert.equal(calls.length,1,'blocked requests must never reach fetch');
  element('authPassword').value='offline-password';
  await sandbox.submitLogin({preventDefault(){}});
  assert.equal(element('authOverlay').style.display,'none');
  assert.equal(element('authPassword').value,'');
  assert.equal(timers.size,1);
  await sandbox.apiFetch('/api/summary');
  for (const id of ['notifyEnabled','notifyOnBuy','notifyOnSell','notifyOnDaily','notifyOnRisk']) element(id).checked=true;
  element('wxAppToken').dataset.saved='true';
  element('wxSpt').dataset.saved='true';
  element('serverchanSendkey').dataset.saved='true';
  await sandbox.saveNotifyConfig();
  let updates=calls.filter(([url,opt])=>url==='/api/notification/config' && opt.method==='POST');
  const untouched=JSON.parse(updates.at(-1)[1].body);
  for (const key of ['wxpusher_app_token','wxpusher_spt','serverchan_sendkey']) assert(!(key in untouched),'unchanged secret must not be resubmitted');
  element('wxAppToken').value='edited-offline-token';
  await sandbox.saveNotifyConfig();
  updates=calls.filter(([url,opt])=>url==='/api/notification/config' && opt.method==='POST');
  assert.equal(JSON.parse(updates.at(-1)[1].body).wxpusher_app_token,'edited-offline-token');
  failNext=true;
  await assert.rejects(sandbox.apiFetch('/api/summary'));
  assert.equal(element('authOverlay').style.display,'flex');
  assert.equal(timers.size,0,'401 must stop dashboard polling');
  await sandbox.logoutSession();
  assert.equal(authenticated,false);
  assert.equal(element('authOverlay').style.display,'flex');
})().catch(error=>{console.error(error);process.exitCode=1;});
''')
    completed = subprocess.run([node, str(harness), str(source_file)], capture_output=True,
                               text=True, timeout=15)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "localStorage" not in source and "sessionStorage" not in source
