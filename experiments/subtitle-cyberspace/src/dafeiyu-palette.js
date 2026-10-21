import { Color } from 'three';

export const DAFEIYU_PALETTE = {
  background: '#050b1b', halo: '#91dfff', core: '#edf8ff',
  ribbon: '#a5bfea', distant: '#c1d3f4', violet: '#7772cb', hit: '#78edb0',
};
const glsl = hex => `vec3(${new Color(hex).toArray().map(value => value.toFixed(6)).join(',')})`;

// Recolor emitted light and subtitle ink only. The character texture, camera,
// geometry and the two previous themes never pass through a global color filter.
export function applyDafeiyuPalette(scene) {
  const p = DAFEIYU_PALETTE;
  scene.scene.background = new Color(p.background);
  scene.hole.material.fragmentShader = scene.hole.material.fragmentShader.replace(
    'vec3 gold=mix(vec3(1.,.60,.25),vec3(1.,.90,.69),pow(heat,2.4));',
    `vec3 gold=mix(mix(${glsl(p.violet)},${glsl(p.halo)},.88),${glsl(p.core)},pow(heat,2.4));`);
  scene.sheets.forEach((mesh, i) => {
    mesh.material.uniforms.uColor.value.set(i >= 7 ? p.distant : p.ribbon);
    mesh.material.fragmentShader = mesh.material.fragmentShader
      .replace('uColor*vec3(1.,.78,.48),vec3(1.,.91,.75)', `uColor*vec3(.75,.88,1.),${glsl(p.core)}`)
      .replace('vec3(.32,1.,.52)', glsl(p.hit))
      .replace('vec3(.08,1.,.28)', glsl(p.hit))
      .replace('vec3(.24,.95,.5)', glsl(p.hit));
  });
  scene.fragments.material.vertexShader = scene.fragments.material.vertexShader.replace(
    'mix(vec3(1.,.78,.44),vec3(.82,.72,.52),smoothstep(5.,19.,r))',
    `mix(${glsl(p.halo)},${glsl(p.ribbon)},smoothstep(5.,19.,r))`);
  scene.fragments.material.fragmentShader = scene.fragments.material.fragmentShader.replace('vec3(.32,1.,.52)', glsl(p.hit));
  const stars = scene.stars.group.children[0].geometry.getAttribute('color');
  const silver = new Color(p.core), blue = new Color(p.halo);
  for (let i = 0; i < stars.count; i++) {
    const luminance = Math.max(stars.getX(i), stars.getY(i), stars.getZ(i));
    const color = i % 5 === 0 ? blue : silver;
    stars.setXYZ(i, color.r * luminance, color.g * luminance, color.b * luminance);
  }
  stars.needsUpdate = true;
  const dust = scene.stars.group.children[1].material;
  dust.fragmentShader = dust.fragmentShader.replace('vec3(.028,.034,.042),vec3(.047,.04,.029)',
    'vec3(.012,.024,.065),vec3(.035,.023,.075)');
}
