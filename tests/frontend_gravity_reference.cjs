const {chromium}=require("./playwright.cjs");
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {execFileSync}=require('node:child_process');
process.chdir(path.resolve(__dirname,'..'));
// Run after caching the exact public source/atlas in .runtime/gravity-source-inspection.
// Original assets are QA-only and never enter frontend/dist.
const results=[];
const moduleBase='/@fs/'+process.cwd().replaceAll('\\','/')+'/experiments/subtitle-cyberspace/src';
const out=path.resolve('.runtime/gravity-reference-parity');fs.mkdirSync(out,{recursive:true});
(async()=>{
 const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
 const errors=[];
 try {
 for(const mode of ['original','port','subtitles']){
  const page=await browser.newPage({viewport:{width:1440,height:900},deviceScaleFactor:1});
  page.on('pageerror',e=>errors.push(mode+': '+e.message));page.on('console',m=>{if(m.type()==='error')errors.push(mode+': '+m.text());});
  await page.route('**/qa-reference.js',r=>r.fulfill({contentType:'text/javascript',body:fs.readFileSync('.runtime/gravity-source-inspection/scene.js','utf8')}));
  await page.route('**/assets/math-atlas-B7Dmj3_L.svg',r=>r.fulfill({contentType:'image/svg+xml',body:fs.readFileSync('.runtime/gravity-source-inspection/math-atlas.svg')}));
  await page.route('**/qa-parity',r=>r.fulfill({contentType:'text/html',body:`<!doctype html><style>*{margin:0}html,body,#stage{width:100%;height:100%;overflow:hidden}canvas{display:block;width:100%;height:100%}</style><div id="stage"></div><script>
window.clock=0;window.queue=[];performance.now=()=>clock;window.requestAnimationFrame=fn=>(queue.push(fn),queue.length);window.cancelAnimationFrame=()=>{};
</script><script type="module">
const stage=document.querySelector('#stage');
${mode==='original'?`
const {createGravityScene,prepareGravityScene}=await import('/qa-reference.js');await prepareGravityScene();
window.cleanup=createGravityScene(stage,()=>{},()=>{throw Error('Reference failed')});
window.step=()=>{clock+=1000/60;const q=queue.splice(0);q.forEach(fn=>fn(clock))};
`:`
const THREE=await import('/node_modules/.vite/deps/three.js');
const {GravitySubtitleScene}=await import('${moduleBase}/gravity-scene.js');
let texture=null;
if('${mode}'==='port'){
const image=new Image();await new Promise((resolve,reject)=>{image.onload=resolve;image.onerror=reject;image.src='/assets/math-atlas-B7Dmj3_L.svg'});
const canvas=document.createElement('canvas');canvas.width=4096;canvas.height=3072;canvas.getContext('2d').drawImage(image,0,0);
texture=new THREE.CanvasTexture(canvas);texture.wrapS=texture.wrapT=THREE.RepeatWrapping;texture.minFilter=THREE.LinearMipmapLinearFilter;texture.magFilter=THREE.LinearFilter;
}
const renderer=new THREE.WebGLRenderer({antialias:false,powerPreference:'high-performance',alpha:false});renderer.setSize(1440,900);stage.appendChild(renderer.domElement);
const scene=new GravitySubtitleScene({referenceTexture:texture,mobile:false});window.scene=scene;
const {mapMarkers}=await import('${moduleBase}/core.js');
const {fixture}=await import('${moduleBase}/fixture.js');
window.frame={active:true,aspect:1.6,data:fixture,mappings:[],hits:new Map()};
window.step=()=>{clock+=1000/60;scene.render(renderer,{...frame,ambient:clock/1000})};
window.showHits=()=>{frame.mappings=mapMarkers({subtitles:fixture.subtitles,markers:fixture.markers.slice(0,1)});frame.hits=new Map(frame.mappings.flatMap(m=>m.keys.map(k=>[k,1])));};
`}
window.ready=true;
</script>`}));
  await page.goto('http://127.0.0.1:5175/qa-parity');
  await page.waitForFunction(()=>window.ready,{},{timeout:30000,polling:100}).catch(e=>{console.log(errors);throw e});
  for(let i=0;i<180;i++){
   await page.evaluate(()=>step());
   if(i===29 || i===179) await page.screenshot({path:path.join(out,mode+(i===29?'':'-3s')+'.png')});
  }
  console.log(mode,await page.evaluate(()=>window.scene?{camera:scene.camera.position.toArray(),time:scene.uniforms.uTime.value,phrases:scene.atlas.entries.length}:document.querySelector('#stage').dataset));
  if(mode==='subtitles'){
   const behavior=await page.evaluate(()=>{
    showHits();step();const rect={left:0,top:0,width:1440,height:900},id=frame.mappings[0].id;
    const point=scene.pointForMarker(id,rect);const selected=point?scene.pick(point.x,point.y,rect):null;
    frame.active=false;const time=scene.uniforms.uTime.value;step();step();
    return {id,point,selected,hits:scene.atlas.hitCount,frozen:time===scene.uniforms.uTime.value};
   });
   assert.ok(behavior.point,'Real shader-displaced fragment has a visible hit target');assert.equal(behavior.selected,behavior.id);
   assert.ok(behavior.hits>0);assert.equal(behavior.frozen,true);results.push(behavior);
   await page.screenshot({path:path.join(out,'subtitles-hits.png')});
  }
  await page.close();
 }
 } finally { await browser.close(); }
 const diff=JSON.parse(execFileSync(path.resolve('.venv/Scripts/python.exe'),['-c',`
from PIL import Image
import numpy as np,json,sys
from pathlib import Path
p=Path(sys.argv[1]); rows=[]
for suffix in ('','-3s'):
 a=np.array(Image.open(p/('original'+suffix+'.png')).convert('RGB')).astype(float)
 b=np.array(Image.open(p/('port'+suffix+'.png')).convert('RGB')).astype(float)
 d=np.abs(a-b)
 rows.append(dict(frame=suffix or '-0.5s',mae=float(d.mean()),maximum=float(d.max()),exact_fraction=float(np.all(d==0,axis=2).mean())))
 Image.fromarray(np.minimum(d*10,255).astype('uint8')).save(p/('difference-x10'+suffix+'.png'))
print(json.dumps(rows))
 `,out],{windowsHide:true,encoding:'utf8'}));
 for(const frame of diff){assert.ok(frame.mae<.1,'Reference parity mean error below 0.1/255');assert.ok(frame.exact_fraction>.98);}
 const report={out,diff,results,errors};fs.writeFileSync(path.join(out,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
 if(errors.length)process.exitCode=1;
})().catch(e=>{console.error(e);process.exitCode=1});
