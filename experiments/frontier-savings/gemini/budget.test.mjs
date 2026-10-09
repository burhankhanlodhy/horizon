// Zero network calls, no real credentials, disposable ledgers only.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';

export async function runTests() {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'contextshrink-gemini-budget-'));
  const source = await fs.readFile(path.join(path.dirname(fileURLToPath(import.meta.url)), 'run.mjs'), 'utf8');
  const originalFetch = globalThis.fetch;
  let calls=0;
  globalThis.fetch = async () => {
    calls++;
    return {status:200,json:async()=>({model:'gemini-3.8-flash',usage:{prompt_tokens:10,completion_tokens:1000},
      choices:[{finish_reason:'tool_calls',message:{tool_calls:[]}}]})};
  };
  async function isolated(name, prior=1.9) {
    const dir=path.join(root,name,'gemini'); await fs.mkdir(dir,{recursive:true});
    const cohort=path.join(root,name,'cohort'); await fs.mkdir(cohort);
    await fs.writeFile(path.join(cohort,'live_results.json'),JSON.stringify({shared_upper_usd:prior}));
    await fs.writeFile(path.join(dir,'run.mjs'),source);
    return {module:await import(pathToFileURL(path.join(dir,'run.mjs')).href), dir};
  }
  const payload={messages:[{role:'user',content:'test'}],max_tokens:64};
  try {
    const cap=await isolated('reported-token-cap');
    const result=await cap.module.request({key:'test-only',model:'gemini-3.8-flash',payload,id:'cap',phase:'smoke'});
    assert.equal(result.reported_cap_violation,true);
    assert.equal((await cap.module.initialize()).stopped,true);
    await assert.rejects(cap.module.request({key:'test-only',model:'gemini-3.8-flash',payload,id:'blocked'}),/study stopped/);
    assert.equal(calls,1);

    const missing=await isolated('interrupted-ledger');
    const saved=await missing.module.initialize();
    saved.new_upper_usd=.01; saved.rows.push({id:'uncertain',status:'reserved',cost_upper_usd:.01});
    await fs.writeFile(path.join(missing.dir,'results.json'),JSON.stringify(saved));
    assert.equal((await missing.module.initialize()).stopped,true);
    assert.equal((await missing.module.initialize()).new_upper_usd,.01);

    const exhausted=await isolated('budget-exhausted',3);
    const stop=await exhausted.module.request({key:'test-only',model:'gemini-3.8-flash',payload,id:'cannot-start'});
    assert.equal(stop.budget_stop,true); assert.equal(calls,1);
    return {passed:3,external_model_calls:0,real_credentials_used:false};
  } finally { globalThis.fetch=originalFetch; }
}
