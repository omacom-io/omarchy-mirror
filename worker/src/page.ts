export const page = `<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Omarchy packages</title>
<style>
:root{color-scheme:light dark;font:16px system-ui;background:#111315;color:#eee}body{max-width:1100px;margin:3rem auto;padding:0 1.5rem}h1{font-size:2rem}p{color:#abb2b8;line-height:1.6}input{font:inherit;padding:.75rem;width:min(32rem,90%);border:1px solid #444;border-radius:6px;background:#1b1e21;color:inherit}table{width:100%;border-collapse:collapse;margin-top:1.5rem}th,td{text-align:left;padding:.75rem;border-bottom:1px solid #303539}th{color:#abb2b8}code{font-size:.8rem}#status{min-height:1.5rem}.scroll{overflow:auto}
</style>
<h1>Omarchy packages</h1><p>Published package versions across release rings.</p>
<label for="search">Find a package</label><p><input id="search" type="search" placeholder="Package name or repository"></p>
<p id="status" role="status">Loading published rings…</p><div class="scroll"><table><thead id="head"></thead><tbody id="rows"></tbody></table></div>
<script>
let rows=[],rings=[];
function cell(tag,text){const e=document.createElement(tag);e.textContent=text;return e}
function render(){const q=document.querySelector('#search').value.toLowerCase();const body=document.querySelector('#rows');body.replaceChildren();const found=rows.filter(r=>(r.name+' '+r.repo+' '+r.arch).toLowerCase().includes(q));for(const row of found.slice(0,300)){const tr=document.createElement('tr');tr.append(cell('td',row.name),cell('td',row.repo),cell('td',row.arch));for(const ring of rings)tr.append(cell('td',row.versions[ring]||'—'));body.append(tr)}document.querySelector('#status').textContent=found.length+' packages'+(found.length>300?' · showing the first 300; narrow your search':'')}
async function get(url){const r=await fetch(url);if(!r.ok)throw new Error('No published data is available yet.');return r.json()}
(async()=>{try{const current=await get('/api/v1/rings.json');rings=Object.keys(current.rings).sort();const head=document.createElement('tr');for(const title of ['Package','Repository','Architecture',...rings])head.append(cell('th',title));document.querySelector('#head').append(head);const map=new Map();for(const ring of rings){const data=await get('/api/v1/builds/'+current.rings[ring].build+'/packages.json');for(const p of data.packages){const key=data.arch+'/'+p.repo+'/'+p.name;const row=map.get(key)||{name:p.name,repo:p.repo,arch:data.arch,versions:{}};row.versions[ring]=p.version;map.set(key,row)}}rows=[...map.values()].sort((a,b)=>a.name.localeCompare(b.name));render();const stamp=document.createElement('p');stamp.textContent='Publication '+current.publication.slice(0,16)+' · '+new Date().toLocaleString();document.body.append(stamp)}catch(e){document.querySelector('#status').textContent=e.message}})();document.querySelector('#search').addEventListener('input',render);
</script></html>`;
