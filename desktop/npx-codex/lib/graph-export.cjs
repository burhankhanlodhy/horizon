"use strict";
const fs = require("node:fs");
const path = require("node:path");
const core = require("./graph-core.cjs");
function render(data) {
  const encoded = JSON.stringify(data).replace(
    /[<>&\u2028\u2029]/g,
    (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"),
  );
  return `<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'">
<title>ContextShrink · Code graph</title><style>
*{box-sizing:border-box}body{margin:0;background:#101820;color:#e3edf5;font:15px system-ui,sans-serif}header{padding:22px 26px;border-bottom:1px solid #30414e}h1{font-size:23px;margin:0 0 8px}p{color:#b9c8d3;margin:8px 0}main{display:grid;grid-template-columns:minmax(0,1fr) 320px;min-height:650px}section{padding:20px}aside{padding:22px;background:#15222c;border-left:1px solid #30414e}input,button{font:inherit;border:1px solid #425766;border-radius:7px;background:#1b303e;color:#e3edf5;padding:9px}input{width:min(500px,100%)}button{cursor:pointer}button:hover,button:focus{border-color:#72dfbd}canvas{display:block;width:100%;height:520px;touch-action:none;border:1px solid #30414e;border-radius:12px;margin-top:14px}#detail{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.6}#matches{max-height:225px;overflow:auto;display:flex;flex-direction:column;gap:6px;margin-top:14px}#matches button{text-align:left;font-size:13px}small{color:#a8bdcb}#counts{margin:14px 0}a{color:#72dfbd}@media(max-width:800px){main{grid-template-columns:1fr}aside{border-left:0;border-top:1px solid #30414e}canvas{height:420px}}
</style><header><h1>ContextShrink · Code graph</h1><p id="project"></p><small id="stamp"></small></header>
<main><section><label for="search">Find a symbol or source file</label><p><input id="search" placeholder="Search names and file paths…" autocomplete="off"> <button id="reset">Reset view</button></p><p id="counts"></p><canvas id="map" role="img" aria-label="Interactive code graph. Drag to pan, scroll to zoom, click a node for its source and relationships."></canvas><p><small>Drag to pan · Scroll to zoom · Click a node to inspect it. This map is a bounded preview; Codex graph queries use the full index.</small></p></section><aside><h2>Source and relationships</h2><div id="detail">Choose a node in the map or search results. Read current source before making edits.</div><div id="matches" aria-label="Matching symbols"></div></aside></main>
<script id="graph-data" type="application/json">${encoded}</script><script>
const graph=JSON.parse(document.getElementById('graph-data').textContent),canvas=document.getElementById('map'),ctx=canvas.getContext('2d');
document.getElementById('project').textContent=graph.project;document.getElementById('stamp').textContent='Indexed '+graph.indexedAt+' · Local static graph · '+graph.totalNodes+' total nodes';
const nodes=graph.nodes.map((n,i)=>({...n,x:Math.cos(i*2.39996)*Math.sqrt(i+1)*22,y:Math.sin(i*2.39996)*Math.sqrt(i+1)*22})),byId=new Map(nodes.map(n=>[n.id,n]));
let selected=null,filter='',zoom=1,pan={x:0,y:0},drag=null,moved=false;
const colors={Function:'#72dfbd',Class:'#b7a0ff',File:'#75bfff',Module:'#75bfff',Project:'#f2cb7c'};
const visible=n=>!filter||(n.name+' '+n.file+' '+n.kind).toLowerCase().includes(filter);
function choose(n){selected=n;const edges=graph.edges.filter(e=>e.source===n.id||e.target===n.id);document.getElementById('detail').textContent=n.name+'\\n'+n.kind+'\\n'+(n.file?(n.file+(n.line?':'+n.line:'')):'Project or module')+'\\n\\n'+edges.map(e=>{const other=byId.get(e.source===n.id?e.target:e.source);return(e.source===n.id?'→ ':'← ')+e.kind+' · '+(other?.name||'outside preview')}).join('\\n');draw();}
function draw(){const r=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1;canvas.width=r.width*dpr;canvas.height=r.height*dpr;ctx.scale(dpr,dpr);ctx.clearRect(0,0,r.width,r.height);ctx.save();ctx.translate(r.width/2+pan.x,r.height/2+pan.y);ctx.scale(zoom,zoom);
for(const e of graph.edges){const a=byId.get(e.source),b=byId.get(e.target);if(!a||!b||!visible(a)||!visible(b))continue;ctx.strokeStyle=selected&&(a.id===selected.id||b.id===selected.id)?'#7bdcbc':'#2b4655';ctx.lineWidth=1/zoom;ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();}
for(const n of nodes){if(!visible(n))continue;ctx.fillStyle=colors[n.kind]||'#a7bac8';ctx.beginPath();ctx.arc(n.x,n.y,selected===n?8:4.5,0,Math.PI*2);ctx.fill();if(selected===n||filter||nodes.length<35){ctx.font='12px system-ui';ctx.fillStyle='#e3edf5';ctx.fillText(n.name.slice(0,40),n.x+9,n.y+4);}}ctx.restore();document.getElementById('counts').textContent=nodes.filter(visible).length+' shown nodes · '+graph.edges.length+' preview relationships';}
function matches(){const box=document.getElementById('matches');box.replaceChildren();for(const n of nodes.filter(visible).slice(0,40)){const b=document.createElement('button');b.textContent=n.name+(n.file?' · '+n.file:'');b.onclick=()=>choose(n);box.append(b);}}
document.getElementById('search').oninput=e=>{filter=e.target.value.toLowerCase();matches();draw()};document.getElementById('reset').onclick=()=>{zoom=1;pan={x:0,y:0};draw()};
canvas.onpointerdown=e=>{drag={x:e.clientX,y:e.clientY};moved=false;canvas.setPointerCapture(e.pointerId)};canvas.onpointermove=e=>{if(!drag)return;const dx=e.clientX-drag.x,dy=e.clientY-drag.y;pan.x+=dx;pan.y+=dy;if(Math.abs(dx)+Math.abs(dy)>2)moved=true;drag={x:e.clientX,y:e.clientY};draw()};canvas.onpointerup=e=>{if(!moved){const r=canvas.getBoundingClientRect(),x=(e.clientX-r.left-r.width/2-pan.x)/zoom,y=(e.clientY-r.top-r.height/2-pan.y)/zoom;let best=null,dist=18/zoom;for(const n of nodes){if(!visible(n))continue;const d=Math.hypot(n.x-x,n.y-y);if(d<dist){best=n;dist=d}}if(best)choose(best)}drag=null};canvas.onwheel=e=>{e.preventDefault();zoom=Math.max(.2,Math.min(5,zoom*(e.deltaY>0?.9:1.1)));draw()};window.onresize=draw;matches();draw();
</script></html>`;
}
async function exportMap(root, file, env = process.env, options = {}) {
  const p = core.project(root, env);
  if (!core.status(root, env).indexed)
    throw new core.GraphError(
      "Run graph init or graph index before exporting.",
    );
  if (!core.status(root, env).fresh) await core.refresh(root, env, options);
  return core.withLock(p, async () => {
    const state = core.json(p.state),
      binary = await core.installEngine(p, options);
    const rawEdges = core.runEngine(p, binary, "query_graph", {
      project: state.project,
      query:
        "MATCH (a)-[r]->(b) RETURN a.qualified_name,a.name,a.label,a.file_path,a.start_line,type(r),b.qualified_name,b.name,b.label,b.file_path,b.start_line LIMIT 1000",
    });
    const byId = new Map(),
      edges = [];
    const node = (row, offset) => ({
      id: row[offset],
      name: row[offset + 1],
      kind: row[offset + 2],
      file: row[offset + 3] === "{}" ? "" : row[offset + 3],
      line: Number(row[offset + 4]) || 0,
    });
    for (const row of rawEdges.rows || []) {
      const a = node(row, 0),
        b = node(row, 6),
        needed =
          Number(!byId.has(a.id)) + Number(a.id !== b.id && !byId.has(b.id));
      if (byId.size + needed > 200) continue;
      byId.set(a.id, a);
      byId.set(b.id, b);
      edges.push({ source: a.id, kind: row[5], target: b.id });
    }
    if (byId.size < 200) {
      const rawNodes = core.runEngine(p, binary, "query_graph", {
        project: state.project,
        query:
          "MATCH (n) RETURN n.qualified_name,n.name,n.label,n.file_path,n.start_line LIMIT 200",
      });
      for (const row of rawNodes.rows || []) {
        const n = node(row, 0);
        if (byId.size < 200 && !byId.has(n.id)) byId.set(n.id, n);
      }
    }
    const nodes = [...byId.values()];
    if (core.snapshot(p).fingerprint !== state.fingerprint)
      throw new core.GraphError(
        "Code changed while exporting. Retry for a fresh map.",
      );
    const output = file ? path.resolve(file) : path.join(p.dir, "map.html");
    if (path.extname(output).toLowerCase() !== ".html")
      throw new core.GraphError("Choose an .html output path.");
    const html = render({
      project: p.root,
      indexedAt: state.indexedAt,
      totalNodes: state.nodes,
      nodes,
      edges,
    });
    if (file) {
      try {
        fs.writeFileSync(output, html, { flag: "wx", mode: 0o600 });
      } catch (e) {
        throw new core.GraphError(
          e.code === "EEXIST"
            ? "Output file already exists. Choose a new path."
            : "Could not write the HTML map. Check the output folder.",
        );
      }
    } else core.atomic(output, html);
    return {
      project: p.root,
      file: output,
      shownNodes: nodes.length,
      shownEdges: edges.length,
      totalNodes: state.nodes,
      offline: true,
    };
  });
}
module.exports = { exportMap, render };
