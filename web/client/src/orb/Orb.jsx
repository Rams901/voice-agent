import { useMemo, useRef } from "react";
import { useFrame, useThree } from "@react-three/fiber";
import { AdditiveBlending, CanvasTexture, GLSL1, Vector4 } from "three";
import { coreFrag, coreVert, glassFrag, glassVert } from "./shader.js";

function glowMap() {
  const canvas = document.createElement("canvas");
  canvas.width = 256;
  canvas.height = 256;
  const g = canvas.getContext("2d");
  const grd = g.createRadialGradient(128, 128, 16, 128, 128, 128);
  grd.addColorStop(0, "rgba(214, 230, 255, 0.72)");
  grd.addColorStop(0.38, "rgba(92, 140, 255, 0.22)");
  grd.addColorStop(1, "rgba(0, 0, 0, 0)");
  g.fillStyle = grd;
  g.fillRect(0, 0, 256, 256);
  const tex = new CanvasTexture(canvas);
  tex.needsUpdate = true;
  return tex;
}

function fillBands(bus, preview, now) {
  if (preview === "speak" || preview === "listen") {
    const speaking = preview === "speak";
    const t = now / 1000;
    for (let i = 0; i < 16; i++) {
      const wave = 0.5 + 0.5 * Math.sin(t * (speaking ? 3.4 : 2.2) + i * 0.62);
      const shape = speaking ? 1 - i / 28 : 0.45 + 0.55 * ((i % 5) / 5);
      bus.targetBands[i] = (0.42 + 0.58 * wave) * (speaking ? 1 : 0.62) * shape;
    }
    bus.targetAmp = speaking ? 0.78 : 0.42;
    bus.modeTarget = speaking ? 2 : 1;
    return;
  }
  if (bus.modeTarget === 2 && now - bus.updated > 180) {
    const t = now / 1000;
    for (let i = 0; i < 16; i++) {
      bus.targetBands[i] = 0.28 + 0.22 * Math.sin(t * 4.2 + i * 0.45);
    }
    bus.targetAmp = 0.4;
    return;
  }
  if (bus.modeTarget === 1 && !bus.micLive && now - bus.updated > 160) {
    const t = now / 1000;
    for (let i = 0; i < 16; i++) {
      bus.targetBands[i] = 0.16 + 0.14 * Math.sin(t * 6 + i * 0.9);
    }
    bus.targetAmp = 0.22;
    return;
  }
  if (bus.modeTarget === 0) {
    for (let i = 0; i < 16; i++) bus.targetBands[i] *= 0.94;
    bus.targetAmp *= 0.94;
  }
}

export default function Orb({ audio, preview }) {
  const group = useRef();
  const halo = useRef();
  const glassMat = useRef();
  const coreMat = useRef();
  const { camera, size } = useThree();
  const reduced = useMemo(
    () => window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ?? false,
    [],
  );
  const map = useMemo(() => glowMap(), []);
  const uniforms = useMemo(
    () => ({
      glass: {
        uTime: { value: 0 },
        uAmp: { value: 0 },
        uMode: { value: 0 },
        uDim: { value: 1 },
        uB0: { value: new Vector4() },
        uB1: { value: new Vector4() },
        uB2: { value: new Vector4() },
        uB3: { value: new Vector4() },
      },
      core: {
        uTime: { value: 0 },
        uAmp: { value: 0 },
        uMode: { value: 0 },
        uDim: { value: 1 },
      },
    }),
    [],
  );

  useFrame((state, dt) => {
    const bus = audio.current;
    const now = performance.now();
    fillBands(bus, preview, now);
    const glass = glassMat.current?.uniforms;
    const core = coreMat.current?.uniforms;
    if (!glass || !core) return;
    const dimGoal = bus.phase === "ended" || bus.phase === "error" ? 0.42 : 1;
    glass.uDim.value += (dimGoal - glass.uDim.value) * 0.08;

    for (let i = 0; i < 16; i++) {
      const k = bus.targetBands[i] > bus.bands[i] ? 0.4 : 0.075;
      bus.bands[i] += (bus.targetBands[i] - bus.bands[i]) * k;
    }
    const attack = bus.targetAmp > bus.amp ? 0.38 : 0.07;
    bus.amp += (bus.targetAmp - bus.amp) * attack;
    bus.mode += (bus.modeTarget - bus.mode) * 0.08;

    const gain = reduced ? 0.25 : 1;
    glass.uTime.value += dt;
    glass.uAmp.value = bus.amp * gain;
    glass.uMode.value = bus.mode;
    const b = bus.bands;
    glass.uB0.value.set(b[0], b[1], b[2], b[3]);
    glass.uB1.value.set(b[4], b[5], b[6], b[7]);
    glass.uB2.value.set(b[8], b[9], b[10], b[11]);
    glass.uB3.value.set(b[12], b[13], b[14], b[15]);
    core.uTime.value = glass.uTime.value;
    core.uAmp.value = glass.uAmp.value;
    core.uMode.value = glass.uMode.value;
    core.uDim.value = glass.uDim.value;

    camera.position.z = size.width < 760 ? 9.4 : 8.35;
    const pointer = reduced ? { x: 0, y: 0 } : state.pointer;
    group.current.rotation.y += (pointer.x * 0.28 - group.current.rotation.y) * 0.045;
    group.current.rotation.x += (-pointer.y * 0.14 - group.current.rotation.x) * 0.045;
    const spin = bus.phase === "connecting" ? 0.35 : 0.08;
    group.current.rotation.z += dt * spin * (reduced ? 0 : 1);
    halo.current.material.opacity = 0.42 + bus.amp * 0.45;
    halo.current.scale.setScalar(2.15 + bus.amp * 0.28);
  });

  return (
    <group ref={group} position={[0, 0.55, 0]} scale={0.78}>
      <sprite ref={halo} scale={2.15}>
        <spriteMaterial map={map} transparent depthWrite={false} blending={AdditiveBlending} opacity={0.7} />
      </sprite>
      <mesh renderOrder={0}>
        <sphereGeometry args={[1, 96, 96]} />
        <shaderMaterial
          ref={coreMat}
          glslVersion={GLSL1}
          uniforms={uniforms.core}
          vertexShader={coreVert}
          fragmentShader={coreFrag}
          transparent
          depthWrite={false}
          blending={AdditiveBlending}
          toneMapped={false}
        />
      </mesh>
      <mesh renderOrder={1}>
        <sphereGeometry args={[1, 192, 192]} />
        <shaderMaterial
          ref={glassMat}
          glslVersion={GLSL1}
          uniforms={uniforms.glass}
          vertexShader={glassVert}
          fragmentShader={glassFrag}
          transparent
          depthWrite={false}
          toneMapped={false}
        />
      </mesh>
    </group>
  );
}
