import { Color } from 'three';

export const GPT_PALETTE = {
  background: '#100d1b', halo: '#b6a0ec', core: '#ece5fa',
  ribbon: '#b9b0d0', distant: '#c7bddc', pink: '#c79bc4', hit: '#83ddb2',
};
const glsl = hex => `vec3(${new Color(hex).toArray().map(value => value.toFixed(6)).join(',')})`;

// Theme-local emission and ink; never tint the character texture globally.
export function applyGptPalette(scene) {
  const p = GPT_PALETTE;
  scene.scene.background = new Color(p.background);
  scene.hole.material.fragmentShader = scene.hole.material.fragmentShader
    .replace('vec3 gold=mix(vec3(1.,.60,.25),vec3(1.,.90,.69),pow(heat,2.4));',
      `vec3 gold=mix(mix(${glsl(p.pink)},${glsl(p.halo)},.88),${glsl(p.core)},pow(heat,5.));`)
    .replace('return gold*luminosity*arc*inner*outer*(1.+uPulse*.18);',
      'return gold*luminosity*arc*inner*outer*(1.+uPulse*.18)*.85;');
  scene.sheets.forEach((mesh, i) => {
    mesh.material.uniforms.uColor.value.set(i >= 7 ? p.distant : p.ribbon);
    mesh.material.fragmentShader = mesh.material.fragmentShader
      .replace('uColor*vec3(1.,.78,.48),vec3(1.,.91,.75)', `uColor*vec3(.86,.78,1.),${glsl(p.core)}`)
      .replace('vec3(.32,1.,.52)', glsl(p.hit))
      .replace('vec3(.08,1.,.28)', glsl(p.hit))
      .replace('vec3(.24,.95,.5)', glsl(p.hit));
  });
  scene.fragments.material.vertexShader = scene.fragments.material.vertexShader.replace(
    'mix(vec3(1.,.78,.44),vec3(.82,.72,.52),smoothstep(5.,19.,r))',
    `mix(${glsl(p.halo)},${glsl(p.ribbon)},smoothstep(5.,19.,r))`);
  scene.fragments.material.fragmentShader = scene.fragments.material.fragmentShader.replace('vec3(.32,1.,.52)', glsl(p.hit));
  const stars = scene.stars.group.children[0].geometry.getAttribute('color');
  const moon = new Color(p.core), pink = new Color(p.pink);
  for (let i = 0; i < stars.count; i++) {
    const luminance = Math.max(stars.getX(i), stars.getY(i), stars.getZ(i));
    const color = i % 5 === 0 ? pink : moon;
    stars.setXYZ(i, color.r * luminance, color.g * luminance, color.b * luminance);
  }
  stars.needsUpdate = true;
  const dust = scene.stars.group.children[1].material;
  dust.fragmentShader = dust.fragmentShader.replace('vec3(.028,.034,.042),vec3(.047,.04,.029)',
    'vec3(.028,.016,.052),vec3(.045,.023,.043)');
}
