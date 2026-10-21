// Extracted from Gravity Journey scene-CZLwjmEL.js (2026-10-18).
// Preserve reference geometry, shader equations and constants. Application lifecycle
// and subtitle coloring live in gravity-scene.js / gravity-atlas.js.
// Original bundle SHA-256: 1b616a8f5c706fb8e9d6bbadcd644be9991b5eb17c3bcc9acbc6ecb97035c17c
import {
 Vector3 as J, Vector2 as q, Quaternion as wt, Euler as nn, Matrix4 as Kt,
 Color as Z, Group as xn, BufferGeometry as Cr, BufferAttribute as cr,
 Float32BufferAttribute as dr, InstancedBufferAttribute as Xr,
 InstancedBufferGeometry as ra, PlaneGeometry as vi, SphereGeometry as yi,
 Sphere as hr, Mesh as Kr, Points as ui, ShaderMaterial as Oi,
 DynamicDrawUsage as Re,
} from 'three';

const al = (e17) => Math.max(0, Math.min(1, e17)), ol = (e17, t2, n2) => t2 <= n2 ? 0 : al(e17 / (t2 - n2)), sl = [0, 0.15, 0.4, 0.65, 0.9, 1], cl = [-0.55, -0.48, 0.95, 2.05, 2.92, 3.18], ll = [0.18, 0.25, 0.92, 0.055, 0.24, 0.34], ul = [18.8, 18, 27.5, 21, 12.4, 13.5];
function dl(e17, t2) {
  let n2 = 0;
  for (; n2 < sl.length - 2 && t2 > sl[n2 + 1]; ) n2++;
  let r3 = (t3) => (e17[t3 + 1] - e17[t3]) / (sl[t3 + 1] - sl[t3]), i2 = (e18) => {
    if (e18 === 0 || e18 === sl.length - 1) return 0;
    let t3 = r3(e18 - 1), n3 = r3(e18);
    return t3 * n3 <= 0 ? 0 : 2 * t3 * n3 / (t3 + n3);
  }, a2 = sl[n2 + 1] - sl[n2], o2 = (t2 - sl[n2]) / a2;
  return (2 * o2 ** 3 - 3 * o2 ** 2 + 1) * e17[n2] + (o2 ** 3 - 2 * o2 ** 2 + o2) * a2 * i2(n2) + (-2 * o2 ** 3 + 3 * o2 ** 2) * e17[n2 + 1] + (o2 ** 3 - o2 ** 2) * a2 * i2(n2 + 1);
}
function fl(e17, t2 = false) {
  let n2 = al(e17), r3 = dl(cl, n2), i2 = dl(ll, n2), a2 = dl(ul, n2), o2 = [Math.sin(r3) * Math.cos(i2) * a2, Math.sin(i2) * a2, Math.cos(r3) * Math.cos(i2) * a2], s2 = -2.8 * Math.sin(Math.PI * al((n2 - 0.65) / 0.35)) ** 2;
  return { theta: r3, elevation: i2, radius: a2, position: o2, target: [Math.cos(r3) * s2, t2 ? 0.04 : 0.1, -Math.sin(r3) * s2] };
}
function pl(e17, t2, n2) {
  if (e17 >= 7) {
    let r4 = (t3) => {
      let r5 = fl(0.63 + t3 * 0.24), i4 = fl(0.631 + t3 * 0.24), a4 = i4.position[0] - r5.position[0], o4 = i4.position[2] - r5.position[2], s4 = Math.hypot(a4, o4), c4 = (e17 === 7 ? -1 : 1) * (2.8 + 0.45 * Math.sin(t3 * Math.PI)), l4 = al(t3 / 0.55), u3 = 5 * (1 - l4 * l4 * (3 - 2 * l4));
      return [r5.position[0] + o4 / s4 * c4, r5.position[1] + (n2 - 0.5) * 5.7 + Math.sin(t3 * Math.PI * 2) * 0.45 - u3, r5.position[2] - a4 / s4 * c4];
    };
    if (t2 <= 0.62) return r4(t2 / 0.68);
    let i3 = r4(1), a3 = (t2 - 0.68) / 0.32, o3 = Math.hypot(i3[0], i3[2]), s3 = Math.atan2(i3[2], i3[0]) + a3 * a3 * 2.7, c3 = 1.4 + (o3 - 1.4) * (1 - a3), l3 = [Math.cos(s3) * c3, i3[1] * (1 - a3) ** 4 - 3 * Math.sin(Math.PI * a3) - 0.4 * a3, Math.sin(s3) * c3];
    if (t2 >= 0.75) return l3;
    let u2 = al((t2 - 0.62) / 0.13), d2 = u2 * u2 * (3 - 2 * u2);
    return r4(t2 / 0.68).map((e18, t3) => e18 + (l3[t3] - e18) * d2);
  }
  let r3 = e17 * 0.898 + 0.78 - t2 * (2.85 + 0.28 * Math.sin(e17 * 2.1)), i2 = 0.8 + (21.3 + e17 * 0.8) * t2 ** 0.85, a2 = (1.4 + i2 * 0.32) * (e17 % 2 == 0 || e17 === 3 ? 1 : 0.48) * Math.min(1, t2 * 5), o2 = (e17 === 3 ? 1.3 : e17 === 4 ? 1.12 : e17 === 6 ? 0.7 : 0.22) * Math.sin(Math.min(1, t2 * 1.8) * Math.PI * 0.5), s2 = i2 + (n2 - 0.5) * a2 * Math.cos(o2), c2 = -0.55 * Math.min(1, t2 * 5) - t2 * (1.5 + e17 * 0.18) + Math.sin(t2 * Math.PI * 1.6 + e17) * t2 * 0.7 + (n2 - 0.5) * a2 * Math.sin(o2) + (e17 === 3 ? 0.55 * Math.sin(t2 * Math.PI) : 0), l2 = al((i2 - 1.94) / 3.06);
  return [Math.cos(r3) * s2, c2 * l2 * l2 * (3 - 2 * l2), Math.sin(r3) * s2];
}
var xl = `
uniform vec2 uPointer,uClickPointer;
uniform float uPointerActive,uClickAge,uAspect,uInteraction;
float pointerHover(vec2 screen){
 vec2 delta=(screen-uPointer)*vec2(uAspect,1.);
 return exp(-dot(delta,delta)*24.)*uPointerActive*uInteraction;
}
float clickWave(vec2 screen){
 float distanceToClick=length((screen-uClickPointer)*vec2(uAspect,1.));
 float ring=(distanceToClick-uClickAge*1.15)*12.;
 return exp(-ring*ring)*exp(-uClickAge*1.3)*uInteraction;
}
`, Sl = `
uniform vec3 uHole;
vec3 gravityPosition(vec3 p){
  if(dot(uHole,uHole)<.000001)return p;
  // Integrate a compact velocity field along the sink's displacement. Each
  // small step preserves orientation, unlike one large weighted translation
  // whose Jacobian can turn negative and fold a sheet into a sharp connector.
  vec3 q=p,delta=uHole/16.;
  for(int i=0;i<16;i++){
    vec3 center=delta*float(i);
    float influence=1.-smoothstep(2.,15.,length(q-center));
    q+=delta*influence;
  }
  return q;
}`;
function Cl(e17, t2, n2 = [0, 0, 0]) {
  let r3 = (e18, t3, n3) => {
    let r4 = Math.max(0, Math.min(1, (n3 - e18) / (t3 - e18)));
    return r4 * r4 * (3 - 2 * r4);
  };
  if (Math.hypot(...t2) < 1e-3) return n2[0] = e17[0], n2[1] = e17[1], n2[2] = e17[2], n2;
  let i2 = e17[0], a2 = e17[1], o2 = e17[2], s2 = t2[0] / 16, c2 = t2[1] / 16, l2 = t2[2] / 16;
  for (let e18 = 0; e18 < 16; e18++) {
    let t3 = 1 - r3(2, 15, Math.hypot(i2 - s2 * e18, a2 - c2 * e18, o2 - l2 * e18));
    i2 += s2 * t3, a2 += c2 * t3, o2 += l2 * t3;
  }
  return n2[0] = i2, n2[1] = a2, n2[2] = o2, n2;
}
var wl = `
${xl}
${Sl}
uniform float uTime;
attribute vec2 aFlowUv;
varying vec2 vUv;
varying vec2 vFlowUv;
varying vec3 vWorld;
varying vec4 vClip;
varying vec3 vHorizon;
varying vec3 vCoreWorld;
void main() {
  vUv = uv;
  vFlowUv = aFlowUv;
  vec3 p = gravityPosition(position);
  p.y += sin(p.x*.35 + p.z*.24 + uTime*.15) * .075;
  vec4 clip=projectionMatrix*modelViewMatrix*vec4(p,1.);
  vec2 screen=clip.xy/max(.01,clip.w);
  float hover=pointerHover(screen)*step(0.,clip.w);
  float wave=clickWave(screen)*step(0.,clip.w);
  float outsideCore=smoothstep(1.94,5.,length(p-uHole));
  float cameraClearance=smoothstep(1.3,3.,length((modelViewMatrix*vec4(p,1.)).xyz));
  p+=normal*(hover*.38*sin(uTime*2.+screen.x*4.)+wave*.62)*outsideCore*cameraClearance;
  vWorld = (modelMatrix * vec4(p,1.0)).xyz;
  gl_Position = projectionMatrix * viewMatrix * vec4(vWorld,1.0);
  vClip=gl_Position;
  vec4 holeClip=projectionMatrix*modelViewMatrix*vec4(uHole,1.);
  vHorizon=vec3(holeClip.xy/holeClip.w,1.94*projectionMatrix[1][1]/holeClip.w);
  vCoreWorld=(modelMatrix*vec4(uHole,1.)).xyz;
}`, Tl = `
${xl}
uniform sampler2D uAtlas;
uniform vec3 uColor;
uniform float uTime;
uniform vec3 uHover;
uniform float uHoverStrength;
uniform float uPulse;
uniform vec2 uRepeat;
uniform vec2 uAtlasOffset;
uniform float uFlow;
uniform float uVisibility;
varying vec2 vUv;
varying vec2 vFlowUv;
varying vec3 vWorld;
varying vec4 vClip;
varying vec3 vHorizon;
varying vec3 vCoreWorld;
void main() {
  // Physical arc length and transverse distance preserve glyph proportions on
  // wide folds. Each sheet starts on different rows of the formula library.
  vec2 lettering=vFlowUv*uRepeat+uAtlasOffset+vec2(uTime*uFlow,0.);
  float text = texture2D(uAtlas, lettering).r;
  // Resolve the actual bent surface normal, including local gravity warping.
  vec3 surfaceNormal=normalize(cross(dFdx(vWorld),dFdy(vWorld)));
  vec3 toEye=normalize(cameraPosition-vWorld),toCore=normalize(vCoreWorld-vWorld);
  float grazing=pow(1.-abs(dot(surfaceNormal,toEye)),3.);
  float coreLight=exp(-length(vCoreWorld-vWorld)*.045);
  float backlight=abs(dot(surfaceNormal,toCore));
  float fineInk=texture2D(uAtlas,lettering*vec2(3.,4.)+vec2(.137,.319)).r;
  float microInk=texture2D(uAtlas,lettering*vec2(9.,11.)+vec2(.593,.721)).r;
  float glint=pow(.5+.5*sin(vFlowUv.x*1.7-vFlowUv.y*3.1-uTime*.6),18.);
  // Sparse, readable lead lettering over a much finer field of equation ink.
  float rowAccent=.24+.76*pow(.5+.5*sin(vFlowUv.y*1.2+uAtlasOffset.y*41.),6.);
  float pulse = .91 + .09*sin(vUv.x*29.0+uTime*.21);
  vec2 screen=vClip.xy/vClip.w;
  float hover=pointerHover(screen),wave=clickWave(screen);
  float edgeDistance=min(vUv.y,1.-vUv.y);
  float edge=(exp(-edgeDistance*350.)*.28+exp(-edgeDistance*65.)*.035)*(.6+grazing*1.8)*(.5+coreLight);
  float sheet=(.0015+.014*grazing*coreLight)*sin(vUv.y*3.14159);
  // Lettering can cross the foreground hemisphere, but decorative membrane
  // edges must not cut a solid-looking strip across the black hole silhouette.
  float membrane=smoothstep(vHorizon.z*.96,vHorizon.z*1.3,length((screen-vHorizon.xy)*vec2(uAspect,1.)));
  edge*=membrane;sheet*=membrane;
  float primary=text*(.95*rowAccent+hover*3.5+wave*4.)*pulse;
  float filigree=fineInk*(.10+coreLight*.16)+microInk*(.025+glint*.35*coreLight);
  float luminosity=(primary+filigree+edge*(1.+wave*2.)+sheet)*uVisibility;
  vec3 tint=mix(uColor*vec3(1.,.78,.48),vec3(1.,.91,.75),clamp(grazing*.45+backlight*coreLight*.3+hover*.3,0.,1.));
  gl_FragColor = vec4(tint * luminosity, 1.0);
}`, El = `
varying vec2 vScreen;
void main(){ vScreen=position.xy; gl_Position=vec4(position.xy,1.,1.); }
`, Dl = `
${xl}
uniform vec3 uEye, uRight, uUp, uForward;
uniform float uTanFov,uTime,uPulse,uPixelHeight;
uniform sampler2D uAtlas;
varying vec2 vScreen;
float equationInk(vec2 uv,float repeatAngle,float repeatRadius,vec3 p){
 // Implicit mip derivatives are undefined in divergent ray-march branches.
 // Estimate the footprint explicitly so D3D/NVIDIA and software agree.
 float footprint=2.*length(p-uEye)*uTanFov/max(1.,uPixelHeight);
 float texelDensity=max(ATLAS_W*repeatAngle/(6.283185*max(1.,length(p.xz))),ATLAS_H*repeatRadius);
 float lod=clamp(log2(max(1.,footprint*texelDensity))-.25,0.,8.);
 return texture2DLodEXT(uAtlas,fract(uv),lod).r;
}
vec3 emission(vec3 p){
 float r=length(p.xz), a=atan(p.z,p.x);
 float inner=smoothstep(1.28,1.55,r),outer=1.-smoothstep(8.,14.,r);
 // Every luminous sample is equation ink. There is no separate gas, beam,
 // line or photon-ring layer. Bloom is produced by these glyphs alone.
 float spin=a-uTime*.34-1.15*log(max(r,1.));
 float radial=r+uTime*.34;
 float heat=exp(-max(0.,r-1.8)*1.3);
 float mantle=exp(-max(0.,r-2.)*.35);
 float bend=.06*sin(spin*2.+r*.35);
 float ink=equationInk(vec2(spin/6.283185*1.4,(radial*.18+bend)/3.),1.4,.06,p);
 float fine=equationInk(vec2(spin/6.283185*7.+.27,(radial*.85+bend)/3.+.37),7.,.85/3.,p);
 float micro=equationInk(vec2(spin/6.283185*23.+.61,(radial*2.3+bend)/3.+.71),23.,2.3/3.,p);
 float dense=1.-smoothstep(5.,12.,r);
 float arc=.75+.25*cos(spin*3.+radial*.5);
 float luminosity=ink*(.08+heat*2.2)+fine*(.035+heat*2.4+mantle*.25)+micro*dense*(.025+heat*1.8);
 // Area-integrated subpixel lettering supplies the smooth inner radiance;
 // resolved outer letters retain their sharp strokes.
 float integrated=texture2DLodEXT(uAtlas,vec2(.5),12.).r;
 luminosity+=integrated*(heat*15.+mantle*.4);
 // Compensate for subpixel glyph compression in the thin side view.
 luminosity*=1.+(mix(4.,1.,smoothstep(.05,.22,abs(uEye.y)/length(uEye)))-1.)*(1.-smoothstep(4.,8.,r));
 vec3 gold=mix(vec3(1.,.60,.25),vec3(1.,.90,.69),pow(heat,2.4));
 return gold*luminosity*arc*inner*outer*(1.+uPulse*.18);
}
void main(){
 vec3 rd=normalize(uForward+uRight*vScreen.x*uAspect*uTanFov+uUp*vScreen.y*uTanFov);
 float projection=dot(uEye,rd),b2=max(0.,dot(uEye,uEye)-projection*projection);
 if(b2>1296.||projection>0.){gl_FragColor=vec4(0.);return;}
 // The visible body uses the same sphere as the scene's depth occluder. Its
 // normals are reconstructed in 3D, rather than shading a flat screen circle.
 float sphereDiscriminant=1.94*1.94-b2;
 float sphereDistance=-projection-sqrt(max(0.,sphereDiscriminant));
 float sphereHit=step(0.,sphereDiscriminant)*step(0.,sphereDistance);
 vec3 sphereNormal=normalize(uEye+rd*sphereDistance);
 vec3 viewDirection=normalize(-uEye);
 vec3 diskUp=normalize(vec3(0.,1.,0.)-viewDirection*viewDirection.y);
 float upperImage=smoothstep(-.1,.2,dot(rd,diskUp)*length(uEye));
 float facing=clamp(dot(sphereNormal,-rd),0.,1.);
 float key=max(0.,dot(sphereNormal,normalize(vec3(-.8,.6,.9))));
 vec3 bodyLight=vec3(.035,.028,.019)*pow(1.-facing,2.2)*(.12+.88*pow(key,1.4));
 bodyLight+=vec3(.0025,.0022,.0018)*(.2+.8*key);
 vec3 p=uEye+rd*max(0.,-projection-sqrt(max(0.,1296.-b2)));
 vec3 velocity=rd;
 float angular=dot(cross(p,velocity),cross(p,velocity));
 vec3 light=vec3(0.); float opacity=0.; float captured=0.;
 for(int i=0;i<TRACE_STEPS;i++){
   float r=length(p);
   if(r<.76){captured=1.;break;}
   if(r>36.2&&dot(p,velocity)>0.)break;
   float stepSize=clamp(r*.085,.035,2.0);
   float r2=r*r;
   vec3 acceleration=-1.14*angular*p/(r2*r2*r);
   vec3 next=p+velocity*stepSize+acceleration*stepSize*stepSize*.5;
   float nextR=length(next),n2=nextR*nextR;
   vec3 nextA=-1.14*angular*next/max(.02,n2*n2*nextR);
   velocity+=(acceleration+nextA)*stepSize*.5;
   if(p.y*next.y<0.){
     vec3 crossing=mix(p,next,p.y/(p.y-next.y));
     // A back-side disk crossing cannot paint over the front hemisphere.
     float inFront=1.-sphereHit*step(sphereDistance,dot(crossing-uEye,rd));
     float nearImage=1.-step(-projection,dot(crossing-uEye,rd));
     vec3 e=emission(crossing)*inFront*mix(.3,1.,max(upperImage,nearImage));
     light+=e*(1.-opacity);
     opacity+=(1.-opacity)*clamp(max(e.r,max(e.g,e.b))*.6,0.,.96);
   }
   p=next;
 }
 light+=bodyLight*sphereHit*(1.-opacity);
 captured=max(captured,sphereHit);
 light*=1.+pointerHover(vScreen)*1.6+clickWave(vScreen)*3.;
 light=max(light,vec3(0.));
 light/=1.+max(light.r,max(light.g,light.b))/3.;
 gl_FragColor=vec4(light,max(opacity,captured));
}`;
function Ol(e17, t2, n2) {
  let r3 = new Oi({ vertexShader: El, fragmentShader: Dl, defines: { TRACE_STEPS: e17 ? 112 : 144, ATLAS_W: Number(n2.image?.width || 4096).toFixed(1), ATLAS_H: Number(n2.image?.height || 3072).toFixed(1) }, uniforms: { uEye: { value: new J() }, uRight: { value: new J() }, uUp: { value: new J() }, uForward: { value: new J() }, uAspect: { value: 1 }, uTanFov: { value: 0.5 }, uTime: t2, uAtlas: { value: n2 }, uPulse: { value: 0 }, uPointer: { value: new q() }, uPointerActive: { value: 0 }, uClickPointer: { value: new q() }, uClickAge: { value: 100 }, uInteraction: { value: 1 }, uPixelHeight: { value: 1080 } }, transparent: true, depthTest: false, depthWrite: false, blending: 5, blendSrc: 201, blendDst: 205 }), i2 = new Kr(new vi(2, 2), r3);
  return i2.frustumCulled = false, i2.renderOrder = -1, i2;
}
var kl = `
${Sl}
attribute float aSeed;
uniform float uTime,uPointerActive,uPulse,uPixelHeight,uTanFov;
uniform vec3 uRight,uUp;
uniform vec2 uPointer,uClickPointer;
varying vec2 vAtlas;
varying float vFade,vHover,vRadius,vPulse;
varying vec3 vColor;
void main(){
 float age=fract(aSeed*.61803398875+uTime/DURATION);
 float lane=mod(aSeed,7.),q=pow(1.-age,.92);
 float angle=lane*.898+.78-q*(2.85+.28*sin(lane*2.1));
 float baseRadius=.8+(21.3+lane*.8)*pow(q,.85);
 float bandWidth=(1.4+baseRadius*.32)*(mod(lane,2.)<.5 || lane==3.?1.:.48)*min(1.,q*5.);
 float across=(fract(aSeed*.3234)-.5)*.6;
 float bank=(lane==3.?1.3:lane==4.?1.12:lane==6.?.7:.22)*sin(min(1.,q*1.8)*3.14159*.5);
 float r=baseRadius+across*bandWidth*cos(bank);
 float y=-.55*min(1.,q*5.)-q*(1.5+lane*.18)+sin(q*3.14159*1.6+lane)*q*.7;
 y=(y+across*bandWidth*sin(bank)+(lane==3.?.55*sin(q*3.14159):0.))*smoothstep(1.94,5.,baseRadius)+.18*smoothstep(1.3,5.,r);
 vec3 center=vec3(cos(angle)*r,y,sin(angle)*r);
 center=gravityPosition(center);
 r=length(center-uHole);
 // Close fragments leave the sheet on different inclined approaches, revealing
 // the front/back hemispheres instead of all orbiting on one flat plane.
 float shell=sin(3.14159*smoothstep(1.35,5.,r));
 center.y+=sin(aSeed*1.713+angle)*shell*.9;
 r=length(center-uHole);
 vec4 clip=projectionMatrix*modelViewMatrix*vec4(center,1.);
 vec2 screen=clip.xy/max(.01,clip.w);
 float distanceToMouse=length(screen-uPointer);
 vHover=exp(-distanceToMouse*distanceToMouse*25.)*uPointerActive*step(0.,clip.w);
 float distanceToClick=length(screen-uClickPointer);
 vPulse=uPulse*exp(-distanceToClick*distanceToClick*2.5)*step(0.,clip.w);
 vec2 away=screen-uPointer;
 away/=max(.01,length(away));
 vec2 clickAway=screen-uClickPointer;
 clickAway/=max(.01,length(clickAway));
 center+=((uRight*away.x+uUp*away.y)*vHover*.65+(uRight*clickAway.x+uUp*clickAway.y)*vPulse*1.4)*smoothstep(1.94,5.,r);
 float shrink=.18+.82*smoothstep(1.3,5.,r);
 float hero=step(.76,fract(aSeed*.713));
 float baseWidth=.7+fract(aSeed*.517)*.65+hero*.8;
 // Give distant readable accents a modest screen-size floor. Compression is
 // still allowed at the horizon so consumption remains continuous.
 float minimumWidth=(24.+hero*30.)*2.*max(1.,clip.w)*uTanFov/max(1.,uPixelHeight);
 float width=max(baseWidth,min(baseWidth*1.6,minimumWidth))*shrink;
 vec3 tangent=normalize(vec3(-sin(angle),.08,cos(angle)));
 vec3 right=normalize(uRight*.92+tangent*.08);
 vec3 p=center+right*position.x*width+uUp*position.y*width*.25;
 p+=normalize(cross(right,uUp))*position.x*position.x*width*.1;
 gl_Position=projectionMatrix*modelViewMatrix*vec4(p,1.);
 float cycle=floor(aSeed*.61803398875+uTime/DURATION);
 float index=mod(aSeed*17.+cycle*13.,FORMULA_COUNT);
 vAtlas=(vec2(mod(index,ATLAS_COLS),floor(index/ATLAS_COLS))+uv)/vec2(ATLAS_COLS,ATLAS_ROWS);
 vFade=smoothstep(0.,.08,age);
 vRadius=r;
 vColor=mix(vec3(1.,.78,.44),vec3(.82,.72,.52),smoothstep(5.,19.,r));
}`, Al = `
uniform sampler2D uAtlas;
varying vec2 vAtlas;
varying float vFade,vHover,vRadius,vPulse;
varying vec3 vColor;
void main(){
 float ink=texture2D(uAtlas,vAtlas).r;
 float fade=vFade;
 gl_FragColor=vec4(vColor*ink*(.65+vHover*2.3+vPulse*.8)*fade,1.);
}`;
function jl(e17, t2, n2) {
  let r3 = new vi(1, 1, 8, 1), i2 = new ra();
  i2.index = r3.index, i2.attributes = r3.attributes, i2.instanceCount = e17, i2.setAttribute(`aSeed`, new Xr(Float32Array.from({ length: e17 }, (e18, t3) => t3), 1));
  let a2 = new Kr(i2, new Oi({ vertexShader: kl, fragmentShader: Al, defines: { DURATION: 28 .toFixed(1), FORMULA_COUNT: 48 .toFixed(1), ATLAS_COLS: 4 .toFixed(1), ATLAS_ROWS: 12 .toFixed(1) }, uniforms: { uTime: t2, uHole: { value: new J() }, uRight: { value: new J(1, 0, 0) }, uUp: { value: new J(0, 1, 0) }, uAtlas: { value: n2 }, uPointer: { value: new q(5, 5) }, uClickPointer: { value: new q() }, uPointerActive: { value: 0 }, uPulse: { value: 0 }, uPixelHeight: { value: 1080 }, uTanFov: { value: 0.4245 } }, transparent: true, blending: 2, depthWrite: false, side: 2, forceSinglePass: true }));
  return a2.frustumCulled = false, { mesh: a2, atlas: n2 };
}
function Ml(e17, t2, n2) {
  let r3 = new xn(), i2 = 90125, a2 = () => (i2 = Math.imul(i2, 1664525) + 1013904223 | 0, (i2 >>> 0) / 4294967296), o2 = [], s2 = [], c2 = [], l2 = [];
  for (let t3 = 0; t3 < (e17 ? 24e3 : 65e3); t3++) {
    let e18 = a2() * Math.PI * 2, n3 = t3 % 3 == 0 ? (a2() - 0.5) * 0.42 + 0.22 * Math.sin(e18 * 1.7) : a2() * 2 - 1, r4 = 100 + a2() * 85, i3 = Math.sqrt(1 - n3 * n3);
    o2.push(Math.cos(e18) * i3 * r4, n3 * r4, Math.sin(e18) * i3 * r4);
    let u3 = a2(), d3 = u3 > 0.996, f3 = new Z(d3 && t3 % 3 == 0 ? `#b2d5f3` : u3 > 0.91 ? `#edd9b8` : `#a8b0ba`);
    f3.multiplyScalar(d3 ? 3.2 : 0.3 + u3 * 0.8), s2.push(f3.r, f3.g, f3.b), c2.push(d3 ? 5 + a2() * 2 : 0.65 + u3 ** 3 * 1.5), l2.push(a2());
  }
  let u2 = new Cr();
  u2.setAttribute(`position`, new dr(o2, 3)), u2.setAttribute(`color`, new dr(s2, 3)), u2.setAttribute(`aSize`, new dr(c2, 1)), u2.setAttribute(`aSeed`, new dr(l2, 1));
  let d2 = new ui(u2, new Oi({ uniforms: { uTime: t2, uPixelRatio: { value: n2 } }, vertexColors: true, vertexShader: `attribute float aSize,aSeed; uniform float uTime,uPixelRatio;
      varying vec3 vColor; varying float vHero;
      void main(){gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.);
      gl_PointSize=max(1.,aSize*uPixelRatio);vHero=step(4.,aSize);
      vColor=color*(.88+.12*sin(uTime*.17+aSeed*80.));}`, fragmentShader: `varying vec3 vColor; varying float vHero;
      void main(){vec2 p=gl_PointCoord-.5;float d=dot(p,p);
      float core=exp(-d*42.);float halo=exp(-d*12.)*.2;
      float rays=(exp(-abs(p.x)*65.)+exp(-abs(p.y)*65.))*exp(-d*10.)*.1*vHero;
      gl_FragColor=vec4(vColor*(core+halo+rays),1.);}`, transparent: true, blending: 2, depthWrite: false, depthTest: false }));
  d2.renderOrder = -3, d2.frustumCulled = false, r3.add(d2);
  let f2 = new Kr(new yi(205, 32, 16), new Oi({ vertexShader: `varying vec3 vDirection;
      void main(){vDirection=position;gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.);}`, fragmentShader: `varying vec3 vDirection;
      float hash(vec3 p){p=fract(p*.3183099+.1);p*=17.;return fract(p.x*p.y*p.z*(p.x+p.y+p.z));}
      float noise(vec3 p){vec3 i=floor(p),f=fract(p);f=f*f*(3.-2.*f);
      return mix(mix(mix(hash(i),hash(i+vec3(1,0,0)),f.x),mix(hash(i+vec3(0,1,0)),hash(i+vec3(1,1,0)),f.x),f.y),
      mix(mix(hash(i+vec3(0,0,1)),hash(i+vec3(1,0,1)),f.x),mix(hash(i+vec3(0,1,1)),hash(i+vec3(1,1,1)),f.x),f.y),f.z);}
      void main(){vec3 d=normalize(vDirection);float n=noise(d*4.)*.6+noise(d*11.)*.28+noise(d*29.)*.12;
      float belt=exp(-pow((d.y+.13*sin(d.x*5.)-.12*d.z)*5.,2.));
      float dust=pow(max(0.,n-.27),2.)*belt;
      vec3 shade=mix(vec3(.028,.034,.042),vec3(.047,.04,.029),noise(d*3.));
      gl_FragColor=vec4(shade*dust*1.65,1.);}`, side: 1, depthWrite: false, depthTest: false, transparent: true, blending: 2 }));
  f2.renderOrder = -4, f2.frustumCulled = false, r3.add(f2);
  let p2 = new wt(), m2 = new wt(), h2 = new wt(), g2 = new wt(), _2 = new nn(0, 0, 0, `YXZ`), v2 = false;
  return { group: r3, update(e18, t3, n3, i3) {
    v2 ||= (p2.copy(e18.quaternion), m2.copy(p2), true), _2.set((n3 - 0.18) * 0.12, (t3 + 0.55) * 0.2, 0), g2.setFromEuler(_2), h2.copy(p2).multiply(g2), m2.slerp(h2, 1 - Math.exp(-i3 * 2.5)), r3.quaternion.copy(e18.quaternion).multiply(g2.copy(m2).invert()), r3.position.copy(e18.position).multiplyScalar(0.97);
  } };
}
function Nl(e17, t2, n2 = false) {
  let r3 = n2 ? 128 : t2 ? 180 : 300, i2 = n2 ? 8 : t2 ? 12 : 20, a2 = n2 ? 24 : 36, o2 = i2 + 1, s2 = (r3 + 1 + a2) * o2, c2 = new Float32Array(s2 * 3), l2 = new Float32Array(s2 * 2), u2 = new Float32Array(s2 * 2), d2 = [], f2 = 0, p2 = pl(e17, 0, 0.5);
  for (let t3 = 0; t3 <= r3; t3++) {
    let n3 = pl(e17, t3 / r3, 0.5);
    f2 += Math.hypot(...n3.map((e18, t4) => e18 - p2[t4])), p2 = n3;
    let a3 = pl(e17, t3 / r3, 0), s3 = pl(e17, t3 / r3, 1), d3 = Math.hypot(...s3.map((e18, t4) => e18 - a3[t4]));
    for (let n4 = 0; n4 <= i2; n4++) {
      let a4 = t3 * o2 + n4;
      c2.set(pl(e17, t3 / r3, n4 / i2), a4 * 3), l2.set([t3 / r3, n4 / i2], a4 * 2), u2.set([f2, (n4 / i2 - 0.5) * d3], a4 * 2);
    }
  }
  let m2 = e17 >= 7 ? 0 : r3, h2 = e17 >= 7 ? 0 : f2, g2 = e17 >= 7 ? -1 : 1, _2 = (e18, t3, n3) => {
    for (let r4 = 0; r4 < i2; r4++) n3 ? d2.push(e18 + r4, t3 + r4, e18 + r4 + 1, t3 + r4, t3 + r4 + 1, e18 + r4 + 1) : d2.push(e18 + r4, e18 + r4 + 1, t3 + r4, t3 + r4, e18 + r4 + 1, t3 + r4 + 1);
  };
  for (let t3 = 0; t3 < r3; t3++) _2(t3 * o2, (t3 + 1) * o2, e17 === 8);
  for (let t3 = 1; t3 <= a2; t3++) {
    let n3 = r3 + t3;
    _2((t3 === 1 ? m2 : n3 - 1) * o2, n3 * o2, e17 >= 7 ? e17 !== 8 : e17 === 8);
    for (let r4 = 0; r4 <= i2; r4++) c2.set(c2.subarray((m2 * o2 + r4) * 3, (m2 * o2 + r4) * 3 + 3), (n3 * o2 + r4) * 3), l2.set([e17 >= 7 ? -t3 / a2 : 1 + t3 / a2, r4 / i2], (n3 * o2 + r4) * 2);
  }
  let v2 = new Cr(), y2 = new cr(c2, 3).setUsage(Re), b2 = new cr(u2, 2).setUsage(Re);
  v2.setAttribute(`position`, y2), v2.setAttribute(`uv`, new cr(l2, 2)), v2.setAttribute(`aFlowUv`, b2), v2.setIndex(d2), v2.computeVertexNormals(), v2.boundingSphere = new hr(new J(), 100);
  let x2 = v2.getAttribute(`normal`);
  x2.setUsage(Re);
  let S2 = e17 >= 7 ? 0 : 1, C2 = new J(...pl(e17, S2, 0.5)), w2 = new J(...pl(e17, S2 - g2 * 1e-3, 0.5)), T2 = C2.clone().sub(w2).normalize(), E2 = new J(...pl(e17, S2, 1)).sub(new J(...pl(e17, S2, 0))), D2 = E2.length(), ee2 = new Kt(), O2 = new J(), k2 = new J(), A2 = new J(), j2 = new J(), M2 = new J(), te2 = new J(), N2 = new J(), P2 = new J(), ne2 = new J(), re2 = new J(), ie2 = Array.from({ length: o2 }, () => new J()), ae2, F2 = () => ie2.every((e18) => e18.z < -1) || ie2.every((e18) => e18.x < -1.1) || ie2.every((e18) => e18.x > 1.1) || ie2.every((e18) => e18.y < -1.1) || ie2.every((e18) => e18.y > 1.1), oe2 = new J(), se2 = new Float32Array(r3 + 1), I2 = new Float32Array(r3 + 1), ce2 = [0, 0, 0], le2 = [0, 0, 0], ue2 = [0, 0, 0], de2 = [0, 0, 0], fe2 = [0, 0, 0], pe2 = (e18, t3, n3) => {
    let r4 = (e18 * o2 + t3) * 3;
    le2[0] = c2[r4], le2[1] = c2[r4 + 1], le2[2] = c2[r4 + 2], Cl(le2, ce2, n3);
  };
  return { geometry: v2, updateField(t3) {
    if (oe2.distanceToSquared(t3) < 1e-4) return;
    oe2.copy(t3), ce2[0] = t3.x, ce2[1] = t3.y, ce2[2] = t3.z;
    let n3 = 0, a3 = 0, s3 = 0, c3 = 0;
    for (let e18 = 0; e18 <= r3; e18++) pe2(e18, i2 / 2, ue2), e18 && (n3 += Math.hypot(ue2[0] - a3, ue2[1] - s3, ue2[2] - c3)), a3 = ue2[0], s3 = ue2[1], c3 = ue2[2], se2[e18] = n3, pe2(e18, 0, de2), pe2(e18, i2, fe2), I2[e18] = Math.hypot(fe2[0] - de2[0], fe2[1] - de2[1], fe2[2] - de2[2]);
    let l3 = e17 >= 7 ? 0 : h2 - n3;
    for (let e18 = 0; e18 <= r3; e18++) for (let t4 = 0; t4 <= i2; t4++) {
      let n4 = (e18 * o2 + t4) * 2;
      u2[n4] = se2[e18] + l3, u2[n4 + 1] = (t4 / i2 - 0.5) * I2[e18];
    }
    b2.addUpdateRange(0, (r3 + 1) * o2 * 2), b2.needsUpdate = true;
  }, update(t3, n3, s3 = 1) {
    ee2.copy(n3).invert(), O2.copy(C2).applyMatrix4(n3).applyMatrix4(t3.matrixWorldInverse), k2.copy(T2).transformDirection(n3).transformDirection(t3.matrixWorldInverse);
    let l3 = -O2.z, d3 = Math.tan(t3.fov * Math.PI / 360), f3 = Math.min(6, Math.max(0.4, Math.abs(l3) * 0.4));
    if (A2.copy(C2).addScaledVector(T2, f3), l3 <= 0) M2.copy(O2).addScaledVector(k2, 60), M2.z = Math.max(25, M2.z);
    else {
      let n4 = O2.x / (Math.max(0.1, l3) * d3 * t3.aspect), r4 = O2.y / (Math.max(0.1, l3) * d3), i3 = (k2.x * l3 + O2.x * k2.z) / t3.aspect, a3 = k2.y * l3 + O2.y * k2.z;
      (Math.max(Math.abs(n4), Math.abs(r4)) > 1.1 || i3 * n4 + a3 * r4 < 0) && (i3 = n4, a3 = r4), Math.hypot(i3, a3) < 1e-3 && (i3 = Math.cos(e17 * 1.7), a3 = Math.sin(e17 * 1.7));
      let o3 = Math.max(38, l3 + 28), c3 = 1.55 + D2 / (Math.max(10, o3 - D2) * d3 * Math.min(t3.aspect, 1)), u3 = Math.atan2(a3, i3);
      ae2 === void 0 && (ae2 = u3);
      let f4 = Math.atan2(Math.sin(u3 - ae2), Math.cos(u3 - ae2));
      ae2 += f4 * (1 - Math.exp(-s3 * 5));
      let p4 = Math.cos(ae2) * c3 * Math.SQRT2, m4 = Math.sin(ae2) * c3 * Math.SQRT2;
      M2.set(p4 * o3 * d3 * t3.aspect, m4 * o3 * d3, -o3);
    }
    M2.applyMatrix4(t3.matrixWorld).applyMatrix4(ee2), j2.copy(C2).lerp(M2, 0.72), v2.boundingSphere.radius = Math.max(60, M2.length() + D2, A2.length() + D2, j2.length() + D2), N2.copy(C2);
    let p3 = 0;
    for (let s4 = 1; s4 <= a2; s4++) {
      let l4 = s4 / a2, d4 = 1 - l4, f4 = r3 + s4;
      te2.copy(C2).multiplyScalar(d4 * d4 * d4).addScaledVector(A2, 3 * d4 * d4 * l4).addScaledVector(j2, 3 * d4 * l4 * l4).addScaledVector(M2, l4 * l4 * l4), p3 += te2.distanceTo(N2), N2.copy(te2), P2.copy(A2).sub(C2).multiplyScalar(3 * d4 * d4).addScaledVector(re2.copy(j2).sub(A2), 6 * d4 * l4).addScaledVector(re2.copy(M2).sub(j2), 3 * l4 * l4), ne2.crossVectors(P2, E2).normalize().multiplyScalar(e17 >= 7 ? e17 === 8 ? -1 : 1 : -1);
      for (let e18 = 0; e18 <= i2; e18++) {
        let r4 = f4 * o2 + e18;
        re2.copy(te2).addScaledVector(E2, e18 / i2 - 0.5), c2[r4 * 3] = re2.x, c2[r4 * 3 + 1] = re2.y, c2[r4 * 3 + 2] = re2.z, x2.setXYZ(r4, ne2.x, ne2.y, ne2.z), u2[r4 * 2] = h2 + g2 * p3, u2[r4 * 2 + 1] = (e18 / i2 - 0.5) * D2, s4 === a2 && (ie2[e18].copy(re2).applyMatrix4(n3).applyMatrix4(t3.matrixWorldInverse), ie2[e18].z >= 0 ? ie2[e18].set(0, 0, -2) : ie2[e18].copy(re2).applyMatrix4(n3).project(t3));
      }
    }
    let m3 = (r3 + 1) * o2;
    return y2.addUpdateRange(m3 * 3, a2 * o2 * 3), y2.needsUpdate = true, b2.addUpdateRange(m3 * 2, a2 * o2 * 2), b2.needsUpdate = true, x2.addUpdateRange(m3 * 3, a2 * o2 * 3), x2.needsUpdate = true, F2();
  } };
}

export { fl as referenceCamera, pl as referencePoint, Nl as referenceRibbon, Ol as referenceHole, jl as referenceFragments, Ml as referenceStars, wl as ribbonVertex, Tl as ribbonFragment, Cl as deformPoint };
