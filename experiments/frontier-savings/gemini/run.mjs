// Credential stays in the caller's memory. No secret appears in files or logs.
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const resultsPath = path.join(here, 'results.json');
const base = 'https://api.oneprovider.dev/v1';
const rates = {'gemini-3.8-flash': [.75, 3.75], 'gemini-3.1-pro': [2, 12], 'gemini-3.1-pro-preview': [2, 12]};

async function readJson(file) { return JSON.parse(await fs.readFile(file, 'utf8')); }
async function persist(state) {
  await fs.writeFile(resultsPath + '.tmp', JSON.stringify(state, null, 2) + '\n');
  await fs.rename(resultsPath + '.tmp', resultsPath);
}

export async function initialize() {
  try {
    const existing = await readJson(resultsPath);
    if(existing.rows.some(row => row.status === 'reserved')) {
      existing.stopped = true;
      existing.stop_reason = 'interrupted request has unknown usage; reservation retained';
      await persist(existing);
    }
    return existing;
  } catch(e) { if(e.code !== 'ENOENT') throw e; }
  const prior = await readJson(path.join(here, '../cohort/live_results.json'));
  const state = {provider:base, pricing_source:'https://oneprovider.dev/pricing', pricing_checked:'2026-10-09',
    prior_shared_upper_usd:prior.shared_upper_usd, budget_usd:3, new_upper_usd:0, new_study_cap_usd:.85,
    accounting:'Published catalog rates; no cache discount; 2x usage envelope for the shared stop budget. Not an invoice.',
    production_qualified:false, full_workflow:false, stopped:false, rows:[]};
  await persist(state);
  return state;
}

export async function request({key, model, payload, id, phase, count, arm, repetition}) {
  const state = await initialize();
  if(state.stopped) throw Error('study stopped; do not reset ledger');
  if(state.rows.some(row => row.id === id)) throw Error('duplicate experiment id');
  if(!rates[model]) throw Error('unverified model');
  payload = {...payload, model};
  const [inputRate, outputRate] = rates[model];
  const encoded = JSON.stringify(payload);
  if(Buffer.byteLength(encoded) > 100000 || !Number.isInteger(payload.max_tokens) || payload.max_tokens > 4096) throw Error('request envelope exceeds bounds');
  // This fixture is text/tools only: byte count exceeds tokenizer count.
  const reserve = 2 * (Buffer.byteLength(encoded) * inputRate + payload.max_tokens * outputRate) / 1e6;
  if(state.prior_shared_upper_usd + state.new_upper_usd + reserve > state.budget_usd || state.new_upper_usd + reserve > state.new_study_cap_usd) return {id, budget_stop:true};
  const row = {id, model, phase, count, arm, repetition, status:'reserved', cost_upper_usd:reserve};
  row.requested_max_tokens = payload.max_tokens;
  state.rows.push(row); state.new_upper_usd += reserve;
  await persist(state);
  const started = Date.now();
  try {
    const reply = await fetch(base + '/chat/completions', {method:'POST', headers:{Authorization:'Bearer '+key, 'Content-Type':'application/json'},
      body:encoded, redirect:'error', signal:AbortSignal.timeout(180000)});
    row.status = reply.status; row.seconds = (Date.now()-started)/1000;
    if(reply.status !== 200) { row.error='provider_rejected'; state.stopped=true; if(reply.status>=400 && reply.status<500){state.new_upper_usd-=reserve; row.cost_upper_usd=0;} await persist(state); return row; }
    const body = await reply.json();
    const usage = body.usage;
    if(!usage || !Number.isInteger(usage.prompt_tokens) || !Number.isInteger(usage.completion_tokens) || usage.prompt_tokens<0 || usage.completion_tokens<0 || body.model !== model) throw Error('unknown usage or route');
    const cost = (usage.prompt_tokens*inputRate + usage.completion_tokens*outputRate)/1e6;
    row.usage = usage; row.catalog_priced_usd = cost; row.cost_upper_usd = 2*cost; row.returned_model = body.model;
    state.new_upper_usd += row.cost_upper_usd-reserve;
    const work = path.join(here, 'runs', id); await fs.mkdir(work, {recursive:true});
    await fs.writeFile(path.join(work, 'response.json'), JSON.stringify(body));
    row.finish_reason = body.choices?.[0]?.finish_reason;
    row.tools = (body.choices?.[0]?.message?.tool_calls || []).map(call=>call.function?.name);
    if(usage.completion_tokens > payload.max_tokens) {
      row.reported_cap_violation=true;
      state.stopped=true;
      state.stop_reason='reported completion tokens exceed requested max_tokens; prospective spend envelope is unqualified';
      state.prospective_budget_bounds_valid=false;
    }
    await persist(state);
    return row;
  } catch(e) { state.stopped=true; row.error='ambiguous_or_invalid_reply'; row.error_class=e.name; await persist(state); return row; }
}

export async function acknowledgeInterruptedBounds(note) {
  // Explicit operator action: preserve every unknown call's full envelope.
  // Never replay that id, reduce its reserve or reset prior spend.
  const state = await initialize();
  if(state.prospective_budget_bounds_valid===false) throw Error('cannot resume with unqualified billable-token bounds');
  if(typeof note !== 'string' || !note.trim() || !state.rows.some(row=>row.status==='reserved')) throw Error('no bounded interrupted request to reconcile');
  for(const row of state.rows) if(row.status==='reserved') {
    row.status='interrupted'; row.usage_unknown=true; row.full_reservation_retained=true;
  }
  state.resumptions = [...(state.resumptions || []), {note, reserved_upper_usd:state.new_upper_usd}];
  state.stopped=false;
  await persist(state);
  return {new_upper_usd:state.new_upper_usd, shared_upper_usd:state.prior_shared_upper_usd+state.new_upper_usd};
}

export async function fixture({key, model, count=4, arm, repetition=1, outputLimit=2048}) {
  const specs = await readJson(path.join(here, 'runs/specs.json'));
  return request({key, model, payload:{...specs[`${count}:${arm}`], max_tokens:outputLimit}, id:`${model}-${count}-${arm}-${repetition}`, phase:'fixture', count, arm, repetition});
}
