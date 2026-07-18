import React, { useEffect, useMemo, useRef } from 'react';

type Vec3 = { x: number; y: number; z: number };

type Connection = {
  a: number;
  b: number;
  birth: number; // ms
  ttl: number; // ms
};

function clamp(v: number, a: number, b: number) {
  return Math.max(a, Math.min(b, v));
}

function hash01(n: number) {
  // deterministic pseudo-random in [0,1)
  const x = Math.sin(n * 127.1 + 311.7) * 43758.5453123;
  return x - Math.floor(x);
}

function smoothstep(t: number) {
  const x = clamp(t, 0, 1);
  return x * x * (3 - 2 * x);
}

export function PlexusNeural3DBackground() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const rafRef = useRef<number>(0);
  const lastTRef = useRef<number>(0);

  const prefersReducedMotion = useMemo(() => {
    try {
      return window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches ?? false;
    } catch {
      return false;
    }
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d', { alpha: true, desynchronized: true });
    if (!ctx) return;

    const state = {
      w: 0,
      h: 0,
      dpr: Math.max(1, Math.min(2, window.devicePixelRatio || 1)),

      // camera
      camYaw: 0,
      camPitch: 0,
      camDist: 1,
      pointerX: 0,
      pointerY: 0,

      // world bounds
      volume: 1,
      half: 1,
      zNear: 0.15,
      zFar: 1.9,

      // particles
      count: 2400,
      pts: [] as Vec3[],
      velSeed: [] as number[],

      // connections
      conns: [] as Connection[],
      connMax: 900,
    };

    const setup = () => {
      const rect = canvas.getBoundingClientRect();
      state.w = Math.max(1, Math.floor(rect.width));
      state.h = Math.max(1, Math.floor(rect.height));
      canvas.width = Math.floor(state.w * state.dpr);
      canvas.height = Math.floor(state.h * state.dpr);

      // density scales a bit with viewport; keep it stable.
      const area = state.w * state.h;
      const base = prefersReducedMotion ? 1200 : 2400;
      state.count = prefersReducedMotion ? Math.floor(base) : Math.floor(base + area / 850000);

      // world
      state.volume = prefersReducedMotion ? 1.0 : 1.15;
      state.half = state.volume;
      state.zNear = 0.12;
      state.zFar = 2.1;

      state.connMax = prefersReducedMotion ? 520 : 900;

      // particles
      state.pts = new Array(state.count);
      state.velSeed = new Array(state.count);
      for (let i = 0; i < state.count; i++) {
        // distribute in a rough 3D torus-ish cloud for nicer loops
        const u = hash01(i * 1.17);
        const v = hash01(i * 2.31 + 9);
        const w = hash01(i * 0.73 + 3);

        const theta = u * Math.PI * 2;
        const phi = (v - 0.5) * Math.PI; // -pi/2..pi/2
        const r = 0.38 + 0.55 * Math.pow(w, 0.7);

        const x = Math.cos(theta) * r * Math.cos(phi);
        const y = Math.sin(phi) * r;
        const z = Math.sin(theta) * r * Math.cos(phi);

        state.pts[i] = {
          x: x * state.half,
          y: y * state.half,
          z: (z * state.half + 0.25) % (state.half * 2),
        };
        state.velSeed[i] = hash01(i * 12.9898 + 78.233);
      }

      state.conns = [];
    };

    const onPointerMove = (e: PointerEvent) => {
      const nx = (e.clientX / window.innerWidth) * 2 - 1;
      const ny = (e.clientY / window.innerHeight) * 2 - 1;
      state.pointerX = nx;
      state.pointerY = ny;
    };

    setup();
    window.addEventListener('pointermove', onPointerMove, { passive: true });
    const onResize = () => setup();
    window.addEventListener('resize', onResize);

    // spatial hashing for connections (simple)
    let cellSize = 0.26;
    let grid = new Map<string, number[]>();

    const gridKey = (cx: number, cy: number, cz: number) => `${cx},${cy},${cz}`;

    const rebuildGrid = () => {
      grid.clear();
      cellSize = prefersReducedMotion ? 0.3 : 0.26;
      const inv = 1 / cellSize;
      for (let i = 0; i < state.count; i++) {
        const p = state.pts[i];
        const cx = Math.floor((p.x + state.half) * inv);
        const cy = Math.floor((p.y + state.half) * inv);
        const cz = Math.floor((p.z - state.zNear) * inv);
        const key = gridKey(cx, cy, cz);
        const arr = grid.get(key);
        if (arr) arr.push(i);
        else grid.set(key, [i]);
      }
    };

    const addConnections = (now: number) => {
      // cap connections, create new ones based on proximity in local neighborhood
      const desired = Math.min(state.connMax, prefersReducedMotion ? 420 : 700);
      if (state.conns.length >= desired) return;

      const inv = 1 / cellSize;
      const neighborR = prefersReducedMotion ? 1 : 1;

      const tryCount = prefersReducedMotion ? 260 : 520;
      for (let t = 0; t < tryCount && state.conns.length < state.connMax; t++) {
        const a = Math.floor(hash01(now * 0.00037 + t * 9.1) * state.count);
        const pa = state.pts[a];
        const cx = Math.floor((pa.x + state.half) * inv);
        const cy = Math.floor((pa.y + state.half) * inv);
        const cz = Math.floor((pa.z - state.zNear) * inv);

        let bestB = -1;
        let bestD = 1e9;

        for (let dx = -neighborR; dx <= neighborR; dx++) {
          for (let dy = -neighborR; dy <= neighborR; dy++) {
            for (let dz = -neighborR; dz <= neighborR; dz++) {
              const key = gridKey(cx + dx, cy + dy, cz + dz);
              const arr = grid.get(key);
              if (!arr) continue;
              for (let idx = 0; idx < arr.length; idx++) {
                const b = arr[idx];
                if (b === a) continue;
                // avoid duplicates: mostly skip by index ordering
                if (b < a && hash01(a * 0.13 + b * 0.17 + 0.33) < 0.92) continue;

                const pb = state.pts[b];
                const ddx = pa.x - pb.x;
                const ddy = pa.y - pb.y;
                const ddz = pa.z - pb.z;
                const d2 = ddx * ddx + ddy * ddy + ddz * ddz;

                const maxD = prefersReducedMotion ? 0.58 : 0.52;
                if (d2 < maxD * maxD && d2 < bestD) {
                  bestD = d2;
                  bestB = b;
                }
              }
            }
          }
        }

        if (bestB >= 0) {
          const ttl = (prefersReducedMotion ? 900 : 650) + hash01(bestB * 0.91 + a * 0.31) * (prefersReducedMotion ? 600 : 520);
          state.conns.push({ a, b: bestB, birth: now, ttl });
        }
      }
    };

    const project = (p: Vec3, camYaw: number, camPitch: number) => {
      // rotate around Y (yaw), X (pitch)
      const cy = Math.cos(camYaw);
      const sy = Math.sin(camYaw);
      const cp = Math.cos(camPitch);
      const sp = Math.sin(camPitch);

      let x = p.x;
      let y = p.y;
      let z = p.z;

      // yaw
      const x1 = x * cy + z * sy;
      const z1 = -x * sy + z * cy;
      x = x1;
      z = z1;

      // pitch
      const y1 = y * cp - z * sp;
      const z2 = y * sp + z * cp;
      y = y1;
      z = z2;


      // shift camera
      const dist = prefersReducedMotion ? 1.16 : 1.08;
      z = z * dist + 0.9;

      // perspective
      const persp = 1 / (z + 0.4);
      const sx = state.w * 0.5 + x * persp * (state.w * 0.78);
      const screenY = state.h * 0.52 + y * persp * (state.w * 0.78);

      return {
        x: sx,
        y: screenY,
        z,
        persp,
      };
    };

    const render = (t: number) => {
      const now = t;
      const dt = Math.min(48, now - lastTRef.current || 16.6);
      lastTRef.current = now;

      const w = state.w;
      const h = state.h;
      const dpr = state.dpr;

      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      // subtle background wash
      ctx.globalCompositeOperation = 'source-over';
      ctx.fillStyle = 'rgba(3, 6, 16, 0.22)';
      ctx.fillRect(0, 0, w, h);

      const time = now * 0.001;
      const sync = 0.5 + 0.5 * Math.sin(time * (prefersReducedMotion ? 0.25 : 0.33) + 0.9);

      // camera smooth movement
      const autoYaw = (prefersReducedMotion ? 0.25 : 0.35) * Math.sin(time * 0.23) + time * 0.08;
      const autoPitch = 0.12 * Math.sin(time * 0.19 + 1.8);

      const pointerYaw = state.pointerX * 0.16;
      const pointerPitch = state.pointerY * 0.12;

      state.camYaw = autoYaw + pointerYaw;
      state.camPitch = autoPitch + pointerPitch;

      // particle motion
      const swirlA = 0.22 + sync * 0.32;
      const swirlB = 0.14 + sync * 0.21;
      const moveScale = prefersReducedMotion ? 0.55 : 1.0;

      for (let i = 0; i < state.count; i++) {
        const p = state.pts[i];
        const s = state.velSeed[i];

        // organic field: layered sines
        const f1 = Math.sin(time * (0.55 + s * 0.65) + s * 6.3);
        const f2 = Math.cos(time * (0.45 + s * 0.6) + (p.x + p.y) * 2.4 + s * 2.1);
        const f3 = Math.sin(time * (0.33 + s * 0.4) + (p.z + p.y) * 1.9);

        const ax = (f1 * 0.45 + f2 * 0.35) * swirlA;
        const ay = (f2 * 0.42 - f3 * 0.28) * swirlB;
        const az = (f3 * 0.48 + f1 * 0.18) * 0.18;

        p.x += (ax * 0.00085 * dt) * moveScale;
        p.y += (ay * 0.00085 * dt) * moveScale;
        p.z += (az * 0.00095 * dt) * moveScale;

        // keep in volume with wrap-ish behavior
        const bound = state.half;
        if (p.x > bound) p.x = -bound;
        if (p.x < -bound) p.x = bound;
        if (p.y > bound) p.y = -bound;
        if (p.y < -bound) p.y = bound;

        // z keeps it in view
        if (p.z < state.zNear) p.z = state.zFar - 0.1;
        if (p.z > state.zFar) p.z = state.zNear + 0.1;
      }

      rebuildGrid();
      addConnections(now);

      // prune connections
      if (state.conns.length) {
        state.conns = state.conns.filter((c) => now - c.birth < c.ttl);
      }

      // draw connections with depth sorting
      // compute projected positions for faster draws
      const proj = new Array(state.count);
      for (let i = 0; i < state.count; i++) {
        proj[i] = project(state.pts[i], state.camYaw, state.camPitch);
      }

      // sort connections by average z (farthest first)
      const connList = state.conns
        .map((c) => {
          const pa = proj[c.a];
          const pb = proj[c.b];
          const az = (pa.z + pb.z) * 0.5;
          return { c, az };
        })
        .sort((u, v) => v.az - u.az);

      ctx.globalCompositeOperation = 'lighter';

      // fake DoF by drawing two passes
      for (let pass = 0; pass < 2; pass++) {
        const blurBoost = pass === 0 ? 0.0 : 1.0;
        const alphaMul = pass === 0 ? 1.0 : 0.45;

        for (let idx = 0; idx < connList.length; idx++) {
          const { c } = connList[idx];
          const life = (now - c.birth) / c.ttl;
          const fade = 1 - smoothstep(life);
          const pulse = Math.sin((time * 7.0 + idx * 0.11) + life * 6.0) * 0.5 + 0.5;

          const pa = proj[c.a];
          const pb = proj[c.b];
          const dz = (pa.z + pb.z) * 0.5;

          const dof = clamp((dz - state.zNear) / (state.zFar - state.zNear), 0, 1);
          const depthAlpha = 1 - dof;

          const baseA = (0.07 + depthAlpha * 0.22) * fade * (0.7 + pulse * 0.5) * alphaMul;
          if (baseA < 0.008) continue;

          const colMode = (c.a + c.b) % 3;
          const color = colMode === 0 ? [77, 163, 255] : colMode === 1 ? [139, 123, 255] : [56, 217, 150];

          // connection line
          const sx1 = pa.x;
          const sy1 = pa.y;

          const sx2 = pb.x;
          const sy2 = pb.y;


          const dx = sx2 - sx1;
          const dy = sy2 - sy1;
          // thickness varies with depth
          const lw = (pass === 0 ? 0.55 : 0.35) + (1 - dof) * (pass === 0 ? 0.75 : 0.45);

          ctx.lineWidth = lw;
          ctx.strokeStyle = `rgba(${color[0]},${color[1]},${color[2]},${baseA})`;

          // optional blur effect
          if (blurBoost) {
            ctx.shadowColor = `rgba(${color[0]},${color[1]},${color[2]},${baseA * 2.2})`;
            ctx.shadowBlur = 10 + dof * 10;
          } else {
            ctx.shadowBlur = 0;
          }

          ctx.beginPath();
          ctx.setLineDash([4, 7]);
          ctx.lineDashOffset = -time * 18;
          ctx.moveTo(sx1, sy1);
          ctx.lineTo(sx2, sy2);
          ctx.stroke();

          // data pulse traveling along the line
          // param along line: 0..1
          const travel = (life + (pulse * 0.35 + idx * 0.002)) % 1;
          const seg = 0.08 + depthAlpha * 0.06;
          const t0 = travel - seg;
          const t1 = travel + seg * 0.7;
          const tt0 = clamp(t0, 0, 1);
          const tt1 = clamp(t1, 0, 1);

          if (tt1 > tt0) {
            const px0 = sx1 + dx * tt0;
            const py0 = sy1 + dy * tt0;
            const px1 = sx1 + dx * tt1;
            const py1 = sy1 + dy * tt1;

            const pulseA = baseA * 1.9 * (0.7 + pulse);
            ctx.lineWidth = lw + 0.35 + (1 - dof) * 0.55;
            ctx.strokeStyle = `rgba(${color[0]},${color[1]},${color[2]},${pulseA})`;

            ctx.beginPath();
            ctx.setLineDash([]);
            ctx.moveTo(px0, py0);
            ctx.lineTo(px1, py1);
            ctx.stroke();
          }
        }
      }

      // draw particles after connections
      // draw far->near for proper layering
      const order = new Array(state.count);
      for (let i = 0; i < state.count; i++) order[i] = i;
      order.sort((i, j) => proj[j].z - proj[i].z);

      for (let oi = 0; oi < order.length; oi++) {
        const i = order[oi];
        const p2 = proj[i];
        const dz = p2.z;
        const dof = clamp((dz - state.zNear) / (state.zFar - state.zNear), 0, 1);
        const depthAlpha = 1 - dof;

        const s = state.velSeed[i];
        const hue = ((i * 0.7 + s * 1.7) % 1 + 1) % 1;
        const c0 = hue < 0.33 ? [77, 163, 255] : hue < 0.66 ? [139, 123, 255] : [56, 217, 150];

        const size = (0.8 + depthAlpha * 2.2) * (prefersReducedMotion ? 0.9 : 1.0);
        const a = (0.08 + depthAlpha * 0.35) * (0.55 + sync * 0.45);

        if (a < 0.01) continue;

        // fake DoF by double draw
        for (let pass = 0; pass < 2; pass++) {
          const blur = pass === 0 ? 0 : 1;
          const alpha = pass === 0 ? a : a * 0.55;
          ctx.globalAlpha = alpha;

          if (blur) {
            ctx.shadowColor = `rgba(${c0[0]},${c0[1]},${c0[2]},${alpha * 1.7})`;
            ctx.shadowBlur = 12 + dof * 10;
          } else {
            ctx.shadowBlur = 0;
          }

          ctx.fillStyle = `rgb(${c0[0]},${c0[1]},${c0[2]})`;
          ctx.beginPath();
          ctx.arc(p2.x, p2.y, size + (pass === 0 ? 0.0 : 0.6), 0, Math.PI * 2);
          ctx.fill();
        }
      }

      ctx.globalAlpha = 1;

      rafRef.current = requestAnimationFrame(render);
    };

    // start
    lastTRef.current = performance.now();
    rafRef.current = requestAnimationFrame(render);

    return () => {
      cancelAnimationFrame(rafRef.current);
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('resize', onResize);
    };
  }, [prefersReducedMotion]);

  return (
    <div className="plexus-bg" aria-hidden="true">
      <canvas ref={canvasRef} className="plexus-bg__canvas" />
    </div>
  );
}

