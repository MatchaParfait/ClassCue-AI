import * as THREE from 'three';
import { TIMES, smooth } from './core.js';

export const INTRO_LABELS = ['视频', '音频提取', 'ASR 启动'];
const CYAN = 0x64d9e8, MUTED = 0x345461, AMBER = 0xe2b878;
const point = (x, y, z = 0) => new THREE.Vector3(x, y, z);

export class SignalIntro {
  constructor(scene) {
    this.group = new THREE.Group();
    this.group.position.set(0, 3, -9);
    scene.add(this.group);
    this.materials = []; this.textures = []; this.paths = []; this.packets = []; this.bars = []; this.arcs = [];
    const line = (points, color = CYAN, opacity = 0.5, parent = this.group) => {
      const material = new THREE.LineBasicMaterial({ color, transparent: true, opacity, depthWrite: false });
      material.userData.base = opacity;
      const mesh = new THREE.Line(new THREE.BufferGeometry().setFromPoints(points.map((p) => point(...p))), material);
      parent.add(mesh); this.materials.push(material); return mesh;
    };
    const rectangle = (x, y, w, h, z = 0, opacity = 0.5) =>
      line([[x-w/2,y-h/2,z],[x+w/2,y-h/2,z],[x+w/2,y+h/2,z],[x-w/2,y+h/2,z],[x-w/2,y-h/2,z]], CYAN, opacity);
    const dot = (size, color = CYAN) => {
      const material = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.9, depthWrite: false });
      material.userData.base = 0.9; this.materials.push(material);
      const mesh = new THREE.Mesh(new THREE.BoxGeometry(size, size, size), material);
      this.group.add(mesh); return mesh;
    };
    const text = (value, x, y, w, h, small = false) => {
      const canvas = document.createElement('canvas'); canvas.width = 640; canvas.height = 96;
      const ctx = canvas.getContext('2d');
      ctx.font = `${small ? 400 : 500} ${small ? 24 : 38}px "Noto Experiment"`;
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillStyle = small ? '#749ba8' : '#c4e8ef';
      ctx.fillText(value, 320, 48);
      const texture = new THREE.CanvasTexture(canvas); texture.generateMipmaps = false; texture.minFilter = THREE.LinearFilter;
      this.textures.push(texture);
      const material = new THREE.MeshBasicMaterial({ map: texture, transparent: true, depthWrite: false });
      material.userData.base = 1; this.materials.push(material);
      const mesh = new THREE.Mesh(new THREE.PlaneGeometry(w, h), material); mesh.position.set(x, y, 1); this.group.add(mesh);
    };

    [-10, 0, 10].forEach((x, i) => {
      text(INTRO_LABELS[i], x, -4.3, 8, 1.2);
      text(['01 / MEDIA STREAM', '02 / AUDIO SIGNAL', '03 / SPEECH ENGINE'][i], x, -5.2, 7, 0.9, true);
      line([[x-1.8,-3.4,0],[x+1.8,-3.4,0]], MUTED, 0.65);
    });
    for (let layer = 2; layer >= 0; layer--) rectangle(-10 + layer * 0.28, layer * 0.24, 5, 3.2, -layer * 0.7, 0.6 - layer * 0.16);
    for (let i = 0; i < 9; i++) {
      const y = -1.3 + i * 0.33;
      line([[-12.3,y,0.1],[-7.7,y,0.1]], MUTED, 0.25);
    }
    for (let i = 0; i < 14; i++) {
      const x = -12.1 + i * 0.32;
      rectangle(x, -1.85, 0.14, 0.12, 0, 0.4);
    }
    line([[-10.5,-0.65,0.2],[-9.35,0,0.2],[-10.5,0.65,0.2],[-10.5,-0.65,0.2]], CYAN, 0.8);
    this.scan = line([[-12.3,-1.55,0.4],[-12.3,1.55,0.4]], CYAN, 1);
    this.scanGlow = rectangle(-12.3, 0, 0.28, 3.1, 0.3, 0.2);
    for (let i = 0; i < 40; i++) {
      const bar = line([[0,-0.5,0],[0,0.5,0]], i % 5 === 0 ? AMBER : CYAN, i % 5 === 0 ? 0.85 : 0.5);
      bar.position.x = -2.4 + i * 4.8 / 39;
      this.bars.push(bar);
    }
    line([[-2.7,0,0],[2.7,0,0]], MUTED, 0.45);
    [-2.4, 2.4].forEach((y) => line([[-2.7,y,0],[-2.7 + 0.5,y,0],[2.7,y,0]], MUTED, 0.45));
    this.wave = line(Array.from({ length: 129 }, (_, i) => [-2.5 + i * 5 / 128, 0, 0.25]), CYAN, 0.85);
    for (let ring = 0; ring < 3; ring++) {
      for (let segment = 0; segment < 8; segment++) {
        const radius = 1.7 + ring * 0.48;
        const mesh = line(Array.from({ length: 25 }, (_, j) => {
          const angle = segment * Math.PI / 4 + j / 24 * Math.PI * 0.18;
          return [Math.cos(angle) * radius, Math.sin(angle) * radius, 0];
        }), ring === 1 ? AMBER : CYAN, ring === 1 ? 0.55 : 0.45);
        mesh.position.x = 10; this.arcs.push({ mesh, ring, segment });
      }
    }
    for (let y = 0; y < 7; y++) for (let x = 0; x < 7; x++) {
      const mesh = dot(0.065);
      mesh.position.set(10 + (x-3)*0.32, (y-3)*0.32, 0.1);
      mesh.userData.cell = x + y * 7;
    }
    this.engineScan = line([[8.8,-1.05,0.2],[11.2,-1.05,0.2]], CYAN, 0.9);
    for (let leg = 0; leg < 2; leg++) {
      const start = leg ? 2.9 : -7.1, end = leg ? 7.0 : -2.9;
      for (let branch = -1; branch <= 1; branch++) {
        const points = [[start,branch*0.5,0],[start+0.9,branch*0.5,0],[start+1.5,branch*1.2,0],[end-0.8,branch*1.2,0],[end,branch*0.3,0]];
        line(points, MUTED, 0.65);
        const path = new THREE.CurvePath();
        for (let i = 1; i < points.length; i++) path.add(new THREE.LineCurve3(point(...points[i-1]), point(...points[i])));
        this.paths.push(path);
        for (let i = 0; i < 5; i++) {
          const mesh = dot(i % 2 ? 0.09 : 0.14, leg ? CYAN : AMBER);
          this.packets.push({ mesh, path, leg, offset: i/5 + branch * 0.05 });
        }
      }
    }
    line([[-13.2,-6.2,0],[13.2,-6.2,0]], MUTED, 0.3);
    for (let i = 0; i <= 52; i++) {
      const x = -13 + i*0.5;
      line([[x,-6.2,0],[x,-6.2+(i%5 ? 0.1 : 0.25),0]], MUTED, 0.4);
    }
  }

  render(time, reduced) {
    this.group.visible = time < TIMES.space;
    if (!this.group.visible) return;
    const t = reduced ? (time < TIMES.extract ? 1.5 : 4.5) : time * 6 / TIMES.recognize;
    const opacity = reduced ? (time < TIMES.recognize ? 1 : 0) : 1 - smooth((time - TIMES.recognize) / (TIMES.space - TIMES.recognize));
    this.materials.forEach((m) => { m.opacity = m.userData.base * opacity; });
    const scan = ((t * 0.42) % 1) * 4.6;
    this.scan.position.x = this.scanGlow.position.x = scan;
    const audio = 0.18 + 0.82 * smooth((t - 0.6) / 2);
    this.bars.forEach((bar, i) => {
      bar.scale.y = (0.25 + Math.abs(Math.sin(i*1.27-t*3.4) * Math.cos(i*0.21+t*1.4)) * 3.4) * audio;
    });
    const vertices = this.wave.geometry.attributes.position;
    for (let i = 0; i < vertices.count; i++) vertices.setY(i, Math.sin(i*0.2-t*5) * Math.sin(i*0.073+t) * 0.65 * audio);
    vertices.needsUpdate = true;
    this.packets.forEach(({ mesh, path, leg, offset }) => {
      const active = leg === 0 ? smooth((t-0.25)/0.8) : smooth((t-2.8)/0.6);
      mesh.position.copy(path.getPoint(((t*0.65+offset)%1+1)%1));
      mesh.material.opacity *= active;
    });
    this.arcs.forEach(({ mesh, ring, segment }) => {
      const active = smooth((t-2.8)/1.2);
      mesh.rotation.z = reduced ? 0 : active*(t-2.8)*(ring%2 ? -0.25 : 0.2);
      mesh.material.opacity *= 0.3 + 0.7*active;
      mesh.geometry.setDrawRange(0, Math.max(3, Math.round(25*(0.35+0.65*smooth((t-2.8-segment*0.09)/1.3)))));
    });
    this.engineScan.position.y = ((t*0.7)%1)*2.1;
    this.engineScan.material.opacity *= smooth((t-2.8)/0.8);
    this.group.children.forEach((mesh) => {
      if (mesh.userData.cell === undefined) return;
      const index = mesh.userData.cell;
      mesh.material.opacity *= 0.15 + 0.85*smooth((t-3-index*0.035)/0.4);
    });
  }

  dispose() {
    this.group.traverse((object) => { object.geometry?.dispose(); });
    this.materials.forEach((m) => m.dispose());
    this.textures.forEach((t) => t.dispose());
    this.group.removeFromParent();
  }
}
