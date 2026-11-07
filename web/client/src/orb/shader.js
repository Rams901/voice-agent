export const glassVert = /* glsl */ `
uniform float uTime;
uniform float uAmp;
uniform float uMode;
uniform vec4 uB0;
uniform vec4 uB1;
uniform vec4 uB2;
uniform vec4 uB3;

varying vec3 vNormal;
varying vec3 vWorld;
varying float vDisp;

vec3 mod289(vec3 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
vec4 mod289(vec4 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
vec4 permute(vec4 x) { return mod289(((x * 34.0) + 1.0) * x); }
vec4 taylorInvSqrt(vec4 r) { return 1.79284291400159 - 0.85373472095314 * r; }

float snoise(vec3 v) {
  const vec2 C = vec2(1.0 / 6.0, 1.0 / 3.0);
  const vec4 D = vec4(0.0, 0.5, 1.0, 2.0);
  vec3 i = floor(v + dot(v, C.yyy));
  vec3 x0 = v - i + dot(i, C.xxx);
  vec3 g = step(x0.yzx, x0.xyz);
  vec3 l = 1.0 - g;
  vec3 i1 = min(g.xyz, l.zxy);
  vec3 i2 = max(g.xyz, l.zxy);
  vec3 x1 = x0 - i1 + C.xxx;
  vec3 x2 = x0 - i2 + C.yyy;
  vec3 x3 = x0 - D.yyy;
  i = mod289(i);
  vec4 p = permute(permute(permute(
      i.z + vec4(0.0, i1.z, i2.z, 1.0))
    + i.y + vec4(0.0, i1.y, i2.y, 1.0))
    + i.x + vec4(0.0, i1.x, i2.x, 1.0));
  float n_ = 0.142857142857;
  vec3 ns = n_ * D.wyz - D.xzx;
  vec4 j = p - 49.0 * floor(p * ns.z * ns.z);
  vec4 x_ = floor(j * ns.z);
  vec4 y_ = floor(j - 7.0 * x_);
  vec4 x = x_ * ns.x + ns.yyyy;
  vec4 y = y_ * ns.x + ns.yyyy;
  vec4 h = 1.0 - abs(x) - abs(y);
  vec4 b0 = vec4(x.xy, y.xy);
  vec4 b1 = vec4(x.zw, y.zw);
  vec4 s0 = floor(b0) * 2.0 + 1.0;
  vec4 s1 = floor(b1) * 2.0 + 1.0;
  vec4 sh = -step(h, vec4(0.0));
  vec4 a0 = b0.xzyw + s0.xzyw * sh.xxyy;
  vec4 a1 = b1.xzyw + s1.xzyw * sh.zzww;
  vec3 p0 = vec3(a0.xy, h.x);
  vec3 p1 = vec3(a0.zw, h.y);
  vec3 p2 = vec3(a1.xy, h.z);
  vec3 p3 = vec3(a1.zw, h.w);
  vec4 norm = taylorInvSqrt(vec4(dot(p0, p0), dot(p1, p1), dot(p2, p2), dot(p3, p3)));
  p0 *= norm.x; p1 *= norm.y; p2 *= norm.z; p3 *= norm.w;
  vec4 m = max(0.6 - vec4(dot(x0, x0), dot(x1, x1), dot(x2, x2), dot(x3, x3)), 0.0);
  m = m * m;
  return 42.0 * dot(m * m, vec4(dot(p0, x0), dot(p1, x1), dot(p2, x2), dot(p3, x3)));
}

float bandIndex(float i) {
  if (i < 1.0) return uB0.x;
  if (i < 2.0) return uB0.y;
  if (i < 3.0) return uB0.z;
  if (i < 4.0) return uB0.w;
  if (i < 5.0) return uB1.x;
  if (i < 6.0) return uB1.y;
  if (i < 7.0) return uB1.z;
  if (i < 8.0) return uB1.w;
  if (i < 9.0) return uB2.x;
  if (i < 10.0) return uB2.y;
  if (i < 11.0) return uB2.z;
  if (i < 12.0) return uB2.w;
  if (i < 13.0) return uB3.x;
  if (i < 14.0) return uB3.y;
  if (i < 15.0) return uB3.z;
  return uB3.w;
}

float bandAt(float f) {
  float x = clamp(f, 0.0, 15.0);
  float i = floor(x);
  return mix(bandIndex(i), bandIndex(min(i + 1.0, 15.0)), fract(x));
}

float displace(vec3 p) {
  float dir = atan(p.z, p.x) / 6.2831853 + 0.5;
  float elev = asin(clamp(p.y, -1.0, 1.0)) / 3.14159265 + 0.5;
  float b = bandAt(dir * 15.0) * 0.72 + bandAt(elev * 15.0) * 0.28;
  float listen = smoothstep(0.4, 1.2, uMode) * (1.0 - smoothstep(1.4, 1.9, uMode));
  float speak = smoothstep(1.35, 2.0, uMode);
  float freq = mix(2.1, 5.4, listen);
  freq = mix(freq, 1.55, speak);
  float n = snoise(normalize(p) * freq + uTime * (0.28 + speak * 0.35));
  float n2 = snoise(normalize(p) * (freq * 2.6) + uTime * 0.9);
  float breath = 0.008 * sin(uTime * 0.65);
  float ripple = sin(9.0 * elev * 3.14159 - uTime * (2.4 + uAmp * 6.0));
  float disp = breath + n * (0.006 + uAmp * 0.02) + n2 * uAmp * (0.01 + listen * 0.02);
  disp += b * uAmp * (0.22 + speak * 0.04);
  disp += ripple * uAmp * 0.035;
  return disp;
}

void main() {
  vec3 p = normalize(position);
  float d = displace(p);
  vec3 tangent = normalize(cross(p, abs(p.y) > 0.9 ? vec3(1.0, 0.0, 0.0) : vec3(0.0, 1.0, 0.0)));
  vec3 bitangent = cross(p, tangent);
  float e = 0.12;
  float dx = displace(normalize(p + tangent * e)) - d;
  float dy = displace(normalize(p + bitangent * e)) - d;
  vec3 n = normalize(p - tangent * (dx / e) - bitangent * (dy / e));
  vec3 displaced = p * (1.0 + d);
  vec4 world = modelMatrix * vec4(displaced, 1.0);
  vWorld = world.xyz;
  vNormal = normalize(mat3(modelMatrix) * n);
  vDisp = d;
  gl_Position = projectionMatrix * viewMatrix * world;
}
`;

export const glassFrag = /* glsl */ `
uniform float uAmp;
uniform float uMode;
uniform float uDim;

varying vec3 vNormal;
varying vec3 vWorld;
varying float vDisp;

void main() {
  vec3 viewDir = normalize(cameraPosition - vWorld);
  vec3 n = normalize(vNormal);
  float ndv = max(dot(n, viewDir), 0.0);
  float fres = pow(1.0 - ndv, 2.15);
  float listen = smoothstep(0.4, 1.2, uMode) * (1.0 - smoothstep(1.4, 1.9, uMode));
  float speak = smoothstep(1.35, 2.0, uMode);

  vec3 deep = vec3(0.025, 0.05, 0.14);
  vec3 glass = vec3(0.16, 0.38, 0.92);
  vec3 rimCool = vec3(0.78, 0.9, 1.0);
  vec3 rimWarm = vec3(0.98, 0.86, 0.7);
  vec3 rim = mix(rimCool, rimWarm, listen * 0.72);
  vec3 core = mix(vec3(0.55, 0.78, 1.0), vec3(0.95, 0.9, 0.78), listen * 0.55);

  vec3 col = mix(deep, glass, 0.35 + fres * 0.75);
  col += core * (0.1 + uAmp * 0.28) * pow(ndv, 2.4);
  col += rim * fres * (0.42 + uAmp * 0.55 + speak * 0.12);
  col += vec3(0.7, 0.85, 1.0) * vDisp * 1.6;

  vec3 lightDir = normalize(vec3(0.35, 0.85, 0.45));
  vec3 halfVec = normalize(lightDir + viewDir);
  float spec = pow(max(dot(n, halfVec), 0.0), 64.0);
  col += vec3(1.0, 0.98, 0.95) * spec * (0.7 + uAmp * 0.4);

  vec3 refl = reflect(-viewDir, n);
  float sky = smoothstep(-0.15, 0.85, refl.y);
  col = mix(col, vec3(0.62, 0.8, 1.0), sky * fres * 0.42);

  float alpha = mix(0.5, 0.94, fres);
  col *= uDim;
  gl_FragColor = vec4(col, alpha);
}
`;

export const coreVert = /* glsl */ `
uniform float uTime;
uniform float uAmp;
varying vec3 vNormal;
varying vec3 vWorld;

void main() {
  vec3 p = position * (0.72 + uAmp * 0.08 + 0.02 * sin(uTime * 1.3));
  vec4 world = modelMatrix * vec4(p, 1.0);
  vWorld = world.xyz;
  vNormal = normalize(mat3(modelMatrix) * normal);
  gl_Position = projectionMatrix * viewMatrix * world;
}
`;

export const coreFrag = /* glsl */ `
uniform float uAmp;
uniform float uMode;
uniform float uDim;
varying vec3 vNormal;
varying vec3 vWorld;

void main() {
  vec3 viewDir = normalize(cameraPosition - vWorld);
  float ndv = max(dot(normalize(vNormal), viewDir), 0.0);
  float listen = smoothstep(0.4, 1.2, uMode) * (1.0 - smoothstep(1.4, 1.9, uMode));
  vec3 cool = vec3(0.72, 0.88, 1.0);
  vec3 warm = vec3(1.0, 0.9, 0.74);
  vec3 col = mix(cool, warm, listen * 0.8);
  float glow = (0.45 + uAmp * 0.9) * pow(ndv, 1.4);
  gl_FragColor = vec4(col * glow * uDim, glow * 0.85);
}
`;
