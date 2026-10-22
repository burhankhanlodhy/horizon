// Local retrieval comparison. Does not invoke a model or transmit source.
const fs = require('node:fs');
const path = require('node:path');
const core = require('../../desktop/npx-codex/lib/graph-core.cjs');
const { Tiktoken } = require('../../desktop/npx-codex/node_modules/js-tiktoken/dist/lite.cjs');
const ranks = require('../../desktop/npx-codex/node_modules/js-tiktoken/dist/ranks/o200k_base.cjs');
const { tools } = require('../../desktop/npx-codex/lib/graph-mcp.cjs');
const estimates = require('../../desktop/npx-codex/lib/graph-estimates.cjs');
const encoder = new Tiktoken(ranks);
const tokens = text => encoder.encode(text, [], []).length;
const root = path.resolve(__dirname, '../..');
const tasks = [
  {query:'flash_openai',expected:'_maybe_flash_openai',file:'horizon/proxy/handlers/openai.py'},
  {query:'pricing',expected:'get_model_pricing',file:'horizon/pricing/litellm_pricing.py'},
  {query:'migrate',expected:'migrate',file:'desktop/npx-codex/lib/chats.js'},
];
async function main() {
  const project = core.project(root);
  if (!core.status(root).fresh) await core.refresh(root);
  const snapshot = core.snapshot(project);
  const results = [], proof = [];
  for (const task of tasks) {
    const matches = [], definitions = [];
    for (const file of snapshot.files) {
      const text = fs.readFileSync(path.join(root,file.path),'utf8');
      if(text.includes('\0'))continue;
      text.split(/\r?\n/).forEach((line,i)=>{
        if(!line.toLowerCase().includes(task.query))return;
        const output = `${file.path}:${i+1}:${line}`;
        matches.push(output);
        if(/^\s*(?:export\s+)?(?:async\s+)?(?:def|function|class|const|let)\s/.test(line))definitions.push(output);
      });
    }
    const args={query:task.query,intent:'discovery',limit:5,budget_bytes:2500};
    const plainGraph = await core.query(root,'search',args);
    const graph = estimates.attach(root,args,plainGraph);
    const found = graph.data.results.find(n=>n.name===task.expected&&n.file_path===task.file);
    if(!found)throw Error(`Missing expected symbol ${task.expected}`);
    const lines = fs.readFileSync(path.join(root,found.file_path),'utf8').split(/\r?\n/);
    const evidence = lines.slice(found.start_line-1,Math.min(found.start_line+8,found.end_line)).join('\n');
    if(!evidence.includes(task.expected))throw Error('Source citation is incorrect');
    const schemaTokens=tokens(JSON.stringify(tools));
    const call={type:'function_call',call_id:`graph-${results.length}`,name:'mcp__contextshrink_graph__graph_search',arguments:JSON.stringify(args)};
    const invocationTokens=tokens(JSON.stringify(call));
    const graphTokens=tokens(JSON.stringify(graph))+invocationTokens+estimates.OVERHEAD;
    const broadTokens=Math.min(4000,tokens(matches.join('\n')))+tokens(`rg -n -i -F ${task.query}`);
    const focusedTokens=Math.min(4000,tokens(definitions.join('\n')))+tokens(`rg -n definition ${task.query}`);
    results.push({query:task.query,expected:task.expected,citation:`${task.file}:${found.start_line}`,citationVerified:true,
      broadSearchTokens:broadTokens,focusedSearchTokens:focusedTokens,graphTokens,graphSchemaTokens:schemaTokens,
      graphOverheadAllowance:estimates.OVERHEAD,estimateBaselineTokens:graph.contextshrink_graph_estimate?.baseline_tokens,
      focusedSearchContainsExpectedSymbol:definitions.join('\n').includes(task.expected),
      identicalVerificationTokens:tokens(evidence),broadTokensAvoided:broadTokens-graphTokens,
      focusedTokensAvoided:focusedTokens-graphTokens});
    proof.push({query:task.query,text:JSON.stringify(graph),call,invocationTokens,outputTokens:tokens(JSON.stringify(graph))});
  }
  const report={at:new Date().toISOString(),snapshot:core.json(project.state).fingerprint,tokenizer:'o200k_base',scope:'deterministic retrieval, not a full live-chat A/B',
    baselineOutputCapTokens:4000,graphSchemaCharged:'1024-token allowance per query for full schemas and guidance; invocation and metadata counted explicitly',
    sourceVerification:'same nine-line excerpt in both arms; not claimed as saved',tasks:results};
  fs.writeFileSync(path.join(__dirname,'results.json'),JSON.stringify(report,null,2)+'\n');
  fs.writeFileSync(path.join(root,'desktop/dist-windows/npx-test/graph-ledger-proof.json'),JSON.stringify(proof));
  console.log(JSON.stringify(report,null,2));
}
main().catch(e=>{console.error(e.message);process.exitCode=1;});
