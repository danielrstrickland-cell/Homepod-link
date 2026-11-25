"""HomePod Link - 3D visualizer.

Runs in its own process (so it can never disturb the audio stream) and renders
with the GPU through OpenGL 3.3: instanced glossy bars with lighting and glow,
floor reflections, fog, HDR bloom, beat-reactive camera and sparks.

The main app writes the live spectrum into shared memory; this process only reads.

Shared memory layout (float32):
  [0] sequence counter     [1] beat/bass energy (unused, computed here)
  [2] palette index        [3] paused (1 = pause, e.g. while a sim runs)
  [4] mode (0 waterfall, 1 bars, 2 ring)   [5] quit flag
  [8:8+BANDS] band levels 0..1
"""

import math
import time

import numpy as np

BANDS = 64
SHM_FLOATS = 8 + BANDS
HIST = 64  # waterfall rows

PALETTES = [
    # Theme (Apple blue -> purple), Rainbow, Sunset, Ocean, Neon, Mono
    ["#0A84FF", "#5E5CE6", "#BF5AF2", "#FF375F"],
    ["#E40303", "#FF8C00", "#FFED00", "#008026", "#004DFF", "#750787"],
    ["#FF5F6D", "#FF8A5B", "#FFC371", "#F857A6", "#B24592"],
    ["#00E5FF", "#00A8FF", "#005BEA", "#3A0CA3"],
    ["#39FF14", "#00FFF0", "#FF00E6", "#FFE600"],
    ["#C7C7CC", "#8E8E93", "#E5E5EA"],
    ["#00F0FF", "#7B2FFF", "#FF00C8", "#FF2E63"],   # Nightclub
    ["#FFD60A", "#FF8C00", "#FF3B00", "#D00000"],   # Turbo
    ["#00F5FF", "#00A2FF", "#4D4DFF", "#B400FF"],   # Hyperdrive
]
PALETTE_NAMES = ["Theme", "Rainbow", "Sunset", "Ocean", "Neon", "Mono", "Nightclub", "Turbo", "Hyperdrive"]
MODE_NAMES = ["3D Waterfall", "3D Bars", "3D Ring", "Neon Highway"]
MODE_VIEWS = {0: (0.0, 0.5, 21.0), 1: (-0.35, 0.32, 13.0), 2: (-0.4, 0.45, 14.0), 3: (0.0, 0.10, 14.0)}


def _hex(c):
    c = c.lstrip("#")
    return [int(c[i:i + 2], 16) / 255 for i in (0, 2, 4)]


def palette_array(idx):
    cols = [_hex(c) for c in PALETTES[int(idx) % len(PALETTES)]]
    while len(cols) < 8:
        cols.append(cols[-1])
    return np.array(cols[:8], dtype="f4"), len(PALETTES[int(idx) % len(PALETTES)])


# ----------------------------------------------------------------- shaders --

COMMON_PAL = """
uniform vec3 palette[8];
uniform int pal_n;
vec3 pal(float t) {
    t = clamp(t, 0.0, 0.9999) * float(pal_n - 1);
    int i = int(floor(t));
    return mix(palette[i], palette[min(i + 1, pal_n - 1)], fract(t));
}
"""

BAR_VS = """
#version 330
in vec3 in_pos;
in vec3 in_norm;
in vec4 in_inst;   // x, z, height, band 0..1
in float in_age;   // 0 = newest row, 1 = oldest
uniform mat4 mvp;
uniform float bar_w;
uniform float bar_d;
uniform float mirror;   // 1 normal, -1 reflection
out vec3 v_world;
out vec3 v_norm;
out float v_h;
out float v_band;
out float v_age;
out float v_local_y;
void main() {
    float h = max(in_inst.z, 0.02);
    vec3 p = vec3(in_inst.x + in_pos.x * bar_w, in_pos.y * h, in_inst.y + in_pos.z * bar_d);
    p.y *= mirror;
    v_world = p;
    v_norm = vec3(in_norm.x, in_norm.y * mirror, in_norm.z);
    v_h = in_inst.z;
    v_band = in_inst.w;
    v_age = in_age;
    v_local_y = in_pos.y;
    gl_Position = mvp * vec4(p, 1.0);
}
"""

BAR_FS = """
#version 330
""" + COMMON_PAL + """
in vec3 v_world;
in vec3 v_norm;
in float v_h;
in float v_band;
in float v_age;
in float v_local_y;
uniform vec3 cam_pos;
uniform float mirror;
uniform float beat;
uniform float dim;
uniform float fog_far;
out vec4 frag;
void main() {
    vec3 n = normalize(v_norm);
    vec3 base = pal(v_band);
    vec3 L1 = normalize(vec3(0.4, 1.0, 0.6));
    vec3 L2 = normalize(vec3(-0.7, 0.4, -0.3));
    vec3 V = normalize(cam_pos - v_world);
    float diff = max(dot(n, L1), 0.0) * 0.75 + max(dot(n, L2), 0.0) * 0.25;
    vec3 H = normalize(L1 + V);
    float spec = pow(max(dot(n, H), 0.0), 48.0) * 0.9;
    float rim = pow(1.0 - max(dot(n, V), 0.0), 3.0) * 0.6;
    // glow: brighter toward the top of tall bars
    float hn = clamp(v_h / 4.5, 0.0, 1.0);                       // 0..1 bar height
    float glow = pow(v_local_y, 2.0) * (0.05 + 0.9 * hn * hn) * (1.0 + beat * 0.6);
    float is_top = step(0.98, v_local_y);
    vec3 side = base * (0.02 + diff * 0.16) + base * glow * 1.4 + vec3(spec) * 0.25 + base * rim * 0.35;
    vec3 topc = base * (0.18 + 1.0 * hn) * (1.0 + beat * 0.5);
    vec3 col = mix(side, topc, is_top);
    col += mix(base, vec3(1.0), 0.35) * is_top * smoothstep(0.75, 1.0, hn) * 1.4;   // white-hot peaks only
    float age_fade = 1.0 - v_age;
    col *= mix(0.03, 1.0, pow(age_fade, 1.6));
    float dist = length(cam_pos - v_world);
    float fog = clamp((dist - fog_far * 0.25) / (fog_far * 0.55), 0.0, 1.0);
    col = mix(col, vec3(0.008, 0.01, 0.03), fog);
    float a = 1.0;
    if (mirror < 0.0) {           // reflection: faded with depth below the floor
        col *= 0.32 * exp(v_world.y * 0.9);
    }
    frag = vec4(col * dim, a);
}
"""

FLOOR_VS = """
#version 330
in vec2 in_pos;
uniform mat4 mvp;
uniform float size;
out vec3 v_world;
void main() {
    v_world = vec3(in_pos.x * size, 0.0, in_pos.y * size);
    gl_Position = mvp * vec4(v_world, 1.0);
}
"""

FLOOR_FS = """
#version 330
""" + COMMON_PAL + """
in vec3 v_world;
uniform vec3 cam_pos;
uniform float beat;
uniform float time;
uniform float dim;
uniform float fog_far;
uniform float road;
uniform float road_off;
out vec4 frag;
void main() {
    // glossy dark floor: translucent so reflections show through, with a fine grid
    vec2 g = abs(fract(v_world.xz * 0.5) - 0.5) / fwidth(v_world.xz * 0.5);
    float line = 1.0 - min(min(g.x, g.y), 1.0);
    float dist = length(cam_pos - v_world);
    float fade = 1.0 - clamp((dist - fog_far * 0.25) / (fog_far * 0.75), 0.0, 1.0);
    vec3 grid = pal(0.5 + 0.5 * sin(v_world.x * 0.05 + time * 0.2)) * line * (0.10 + beat * 0.35) * fade;
    float sheen = pow(fade, 3.0) * 0.05;
    vec3 col = grid + vec3(0.02, 0.025, 0.05) * sheen;
    if (road > 0.5) {
        float ax = abs(v_world.x);
        float zz = v_world.z - road_off;
        float onroad = 1.0 - smoothstep(2.9, 3.0, ax);
        col = mix(col, vec3(0.006, 0.006, 0.012), onroad * 0.9);                 // asphalt
        float dash = step(0.5, fract(zz / 3.0)) * (1.0 - smoothstep(0.06, 0.1, ax));
        float edge = 1.0 - smoothstep(0.04, 0.1, abs(ax - 2.85));
        col += pal(0.15) * dash * 2.2 * fade + pal(0.85) * edge * (1.6 + beat * 2.0) * fade;
    }
    frag = vec4(col * dim, 0.80);
}
"""

SPARK_VS = """
#version 330
in vec3 in_pos;
in vec3 in_col;
in float in_life;
uniform mat4 mvp;
uniform float px;
out vec3 v_col;
out float v_life;
void main() {
    gl_Position = mvp * vec4(in_pos, 1.0);
    gl_PointSize = px * (0.6 + in_life) / max(gl_Position.w, 0.1);
    v_col = in_col;
    v_life = in_life;
}
"""

SPARK_FS = """
#version 330
in vec3 v_col;
in float v_life;
uniform float dim;
out vec4 frag;
void main() {
    vec2 d = gl_PointCoord - 0.5;
    float r = dot(d, d) * 4.0;
    float a = exp(-r * 3.0) * v_life;
    frag = vec4((v_col * 2.5 + vec3(0.6)) * a * dim, 1.0);
}
"""

QUAD_VS = """
#version 330
in vec2 in_pos;
out vec2 uv;
void main() { uv = in_pos * 0.5 + 0.5; gl_Position = vec4(in_pos, 0.0, 1.0); }
"""

BG_FS = """
#version 330
""" + COMMON_PAL + """
in vec2 uv;
uniform float beat;
uniform float dim;
out vec4 frag;
void main() {
    vec3 top = vec3(0.004, 0.006, 0.02);
    vec3 horizon = mix(vec3(0.02, 0.025, 0.07), pal(0.4) * 0.10, 0.6) * (1.0 + beat * 0.6);
    float t = smoothstep(0.25, 0.75, uv.y);
    vec3 c = mix(horizon, top, t);
    frag = vec4(c * dim, 1.0);
}
"""

BRIGHT_FS = """
#version 330
in vec2 uv;
uniform sampler2D tex;
out vec4 frag;
void main() {
    vec3 c = texture(tex, uv).rgb;
    float l = max(max(c.r, c.g), c.b);
    frag = vec4(c * smoothstep(0.9, 2.2, l), 1.0);
}
"""

BLUR_FS = """
#version 330
in vec2 uv;
uniform sampler2D tex;
uniform vec2 dir;
out vec4 frag;
void main() {
    float w[5] = float[](0.227027, 0.1945946, 0.1216216, 0.054054, 0.016216);
    vec3 c = texture(tex, uv).rgb * w[0];
    for (int i = 1; i < 5; i++) {
        c += texture(tex, uv + dir * float(i)).rgb * w[i];
        c += texture(tex, uv - dir * float(i)).rgb * w[i];
    }
    frag = vec4(c, 1.0);
}
"""

COMPOSITE_FS = """
#version 330
in vec2 uv;
uniform sampler2D scene;
uniform sampler2D bloom1;
uniform sampler2D bloom2;
uniform float bloom_amt;
out vec4 frag;
vec3 aces(vec3 x) {
    return clamp((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14), 0.0, 1.0);
}
void main() {
    vec3 c = texture(scene, uv).rgb;
    c += (texture(bloom1, uv).rgb * 0.7 + texture(bloom2, uv).rgb * 0.9) * bloom_amt;
    c = aces(c * 0.95);
    float v = smoothstep(1.15, 0.35, length(uv - 0.5) * 1.4);   // vignette
    c *= mix(0.55, 1.0, v);
    frag = vec4(pow(c, vec3(1.0 / 2.2)), 1.0);
}
"""


# ------------------------------------------------------------------- math --


def perspective(fov, aspect, near, far):
    f = 1 / math.tan(fov / 2)
    m = np.zeros((4, 4), "f4")
    m[0, 0], m[1, 1] = f / aspect, f
    m[2, 2], m[2, 3] = (far + near) / (near - far), 2 * far * near / (near - far)
    m[3, 2] = -1
    return m


def look_at(eye, target, up=(0, 1, 0)):
    eye, target, up = (np.array(v, "f4") for v in (eye, target, up))
    f = target - eye
    f /= np.linalg.norm(f)
    s = np.cross(f, up)
    s /= np.linalg.norm(s)
    u = np.cross(s, f)
    m = np.identity(4, "f4")
    m[0, :3], m[1, :3], m[2, :3] = s, u, -f
    m[0, 3], m[1, 3], m[2, 3] = -s @ eye, -u @ eye, f @ eye
    return m


def box_mesh():
    """Unit bar: x,z in [-0.5,0.5], y in [0,1]; per-face normals."""
    faces = [
        ((0, 1, 0), [(-.5, 1, -.5), (.5, 1, -.5), (.5, 1, .5), (-.5, 1, .5)]),
        ((0, 0, 1), [(-.5, 0, .5), (.5, 0, .5), (.5, 1, .5), (-.5, 1, .5)]),
        ((0, 0, -1), [(.5, 0, -.5), (-.5, 0, -.5), (-.5, 1, -.5), (.5, 1, -.5)]),
        ((1, 0, 0), [(.5, 0, .5), (.5, 0, -.5), (.5, 1, -.5), (.5, 1, .5)]),
        ((-1, 0, 0), [(-.5, 0, -.5), (-.5, 0, .5), (-.5, 1, .5), (-.5, 1, -.5)]),
    ]
    out = []
    for n, q in faces:
        for i in (0, 1, 2, 0, 2, 3):
            out += list(q[i]) + list(n)
    return np.array(out, "f4")


# --------------------------------------------------------------- renderer --


class Renderer:
    """Draws one frame into a framebuffer. Independent of the window system so it
    can also render offscreen (used for testing)."""

    def __init__(self, ctx, width, height):
        self.ctx = ctx
        self.mode = 0
        self.palette_idx = 0
        self.levels = np.zeros(BANDS, "f4")
        self.peaks = np.zeros(BANDS, "f4")
        self.hist = np.zeros((HIST, BANDS), "f4")
        self.scroll = 0.0
        self.beat = 0.0
        self._bass_avg = 0.0
        self._last_beat = 0.0
        self.yaw, self.pitch, self.dist = MODE_VIEWS[0]
        self.yaw_v = 0.0
        self.road_off = 0.0
        self._mode_seen = 0
        self.idle_since = time.monotonic()
        self.t = 0.0
        self.dim = 1.0
        self.sparks = np.zeros((0, 8), "f4")  # pos3, vel3, life, band
        self._build(width, height)

    # -- GL resources -----------------------------------------------------
    def _build(self, w, h):
        ctx = self.ctx
        self.w, self.h = w, h
        self.bar_prog = ctx.program(vertex_shader=BAR_VS, fragment_shader=BAR_FS)
        self.floor_prog = ctx.program(vertex_shader=FLOOR_VS, fragment_shader=FLOOR_FS)
        self.spark_prog = ctx.program(vertex_shader=SPARK_VS, fragment_shader=SPARK_FS)
        self.bg_prog = ctx.program(vertex_shader=QUAD_VS, fragment_shader=BG_FS)
        self.bright_prog = ctx.program(vertex_shader=QUAD_VS, fragment_shader=BRIGHT_FS)
        self.blur_prog = ctx.program(vertex_shader=QUAD_VS, fragment_shader=BLUR_FS)
        self.comp_prog = ctx.program(vertex_shader=QUAD_VS, fragment_shader=COMPOSITE_FS)

        self.box_vbo = ctx.buffer(box_mesh().tobytes())
        self.inst_vbo = ctx.buffer(reserve=HIST * BANDS * 5 * 4 + 1024, dynamic=True)
        self.bar_vao = ctx.vertex_array(self.bar_prog, [
            (self.box_vbo, "3f 3f", "in_pos", "in_norm"),
            (self.inst_vbo, "4f 1f/i", "in_inst", "in_age"),
        ])
        quad = ctx.buffer(np.array([-1, -1, 1, -1, -1, 1, 1, 1], "f4").tobytes())
        self.quad_buf = quad
        self.floor_vao = ctx.vertex_array(self.floor_prog, [(quad, "2f", "in_pos")])
        self.quads = {p: ctx.vertex_array(p, [(quad, "2f", "in_pos")])
                      for p in (self.bg_prog, self.bright_prog, self.blur_prog, self.comp_prog)}
        self.spark_vbo = ctx.buffer(reserve=4000 * 7 * 4, dynamic=True)
        self.spark_vao = ctx.vertex_array(self.spark_prog, [(self.spark_vbo, "3f 3f 1f", "in_pos", "in_col", "in_life")])
        self._targets(w, h)

    def _targets(self, w, h):
        ctx = self.ctx
        self.w, self.h = max(2, w), max(2, h)
        self.scene_tex = ctx.texture((self.w, self.h), 4, dtype="f2")
        self.scene_depth = ctx.depth_renderbuffer((self.w, self.h))
        self.scene_fbo = ctx.framebuffer([self.scene_tex], self.scene_depth)
        self.bloom = []
        for div in (2, 4):
            size = (max(2, self.w // div), max(2, self.h // div))
            a = ctx.texture(size, 4, dtype="f2")
            b = ctx.texture(size, 4, dtype="f2")
            for t in (a, b):
                t.repeat_x = t.repeat_y = False
            self.bloom.append((size, a, b, ctx.framebuffer([a]), ctx.framebuffer([b])))

    def resize(self, w, h):
        if (w, h) != (self.w, self.h) and w > 1 and h > 1:
            self._targets(w, h)

    # -- input --------------------------------------------------------------
    def drag(self, dx, dy):
        self.yaw += dx * 0.008
        self.pitch = float(np.clip(self.pitch + dy * 0.006, 0.05, 1.35))
        self.yaw_v = dx * 0.008
        self.idle_since = time.monotonic()

    def zoom(self, amount):
        self.dist = float(np.clip(self.dist * (0.9 ** amount), 6, 40))
        self.idle_since = time.monotonic()

    def reset_view(self):
        self.yaw, self.pitch, self.dist = MODE_VIEWS[self.mode]
        self.yaw_v = 0.0

    # -- per-frame update ---------------------------------------------------
    def feed(self, bands, dt):
        bands = np.clip(np.asarray(bands, "f4"), 0, 1)
        # fast attack, smooth fall (frame-rate independent)
        fall = math.exp(-dt * 7.5)
        self.levels = np.maximum(bands, self.levels * fall)
        self.peaks = np.maximum(self.levels, self.peaks - dt * 0.35)
        # beat detection on the bass
        bass = float(self.levels[:8].mean())
        self._bass_avg = self._bass_avg * 0.95 + bass * 0.05
        now = time.monotonic()
        if bass > self._bass_avg * 1.30 + 0.04 and bass > 0.25 and now - self._last_beat > 0.22:
            self._last_beat = now
            self.beat = 1.0
            self._spawn_sparks(28)
        self.beat *= math.exp(-dt * 5.0)
        if self.mode != self._mode_seen:
            self._mode_seen = self.mode
            self.reset_view()
        energy = float(self.levels.mean())
        if self.mode == 3:  # driving: speed follows the music
            speed = 18.0 + 55.0 * energy + 25.0 * self.beat
            self.road_off += dt * speed * 0.42
            self.scroll += dt * speed
        else:
            self.scroll += dt * 26.0
        while self.scroll >= 1.0:
            self.scroll -= 1.0
            self.hist = np.roll(self.hist, 1, axis=0)
        self.hist[0] = self.levels
        # sparks: physics
        if len(self.sparks):
            s = self.sparks
            if self.mode != 3:
                s[:, 4] -= 9.0 * dt      # gravity (streaks fly straight)
            s[:, 0:3] += s[:, 3:6] * dt
            s[:, 6] -= dt * 0.8          # life
            self.sparks = s[(s[:, 6] > 0) & (s[:, 1] > -0.2) & (s[:, 2] < 12)]
        # camera: inertia + gentle auto-orbit when idle
        self.yaw_v *= math.exp(-dt * 3)
        self.yaw += self.yaw_v
        if time.monotonic() - self.idle_since > 2.5:
            if self.mode in (0, 3):
                sway = 0.35 if self.mode == 0 else 0.12
                self.yaw += (sway * math.sin(self.t * 0.15) - self.yaw) * min(1, dt * 0.5)  # slow sway
            else:
                self.yaw += dt * 0.12
        self.t += dt

    def _layout(self):
        """Instance data (x, z, height, band, age) for the current mode."""
        b = np.arange(BANDS, dtype="f4")
        band_t = b / (BANDS - 1)
        if self.mode == 0:  # waterfall
            rows = np.arange(HIST, dtype="f4")
            xs = (b - (BANDS - 1) / 2) * 0.30
            zs = (rows + self.scroll) * 0.42 - 2.0
            X, Z = np.meshgrid(xs, zs)
            H = self.hist * 4.5
            T = np.broadcast_to(band_t, (HIST, BANDS))
            A = np.broadcast_to((rows / (HIST - 1))[:, None], (HIST, BANDS))
            inst = np.stack([X, -Z, H, T, A], axis=-1).reshape(-1, 5)
            return inst, 0.22, 0.34, 13.0
        if self.mode == 1:  # bars with floating peak caps
            xs = (b - (BANDS - 1) / 2) * 0.30
            bars = np.stack([xs, np.zeros(BANDS), self.levels * 5.5, band_t, np.zeros(BANDS)], axis=1)
            return bars.astype("f4"), 0.22, 0.22, 11.0
        if self.mode == 3:  # neon highway: light walls both sides, rushing toward you
            rows = np.arange(HIST, dtype="f4")
            z = -HIST * 0.6 + (rows + self.scroll) * 0.6 + 6.0  # newest far ahead, rushing past
            groups = self.hist.reshape(HIST, 8, 8).mean(axis=2)  # 8 bands per row
            inst = []
            for k in range(4):
                left_h, right_h = groups[:, k] * 4.0, groups[:, 7 - k] * 4.0
                xl, xr = -(3.7 + k * 0.75), 3.7 + k * 0.75
                zeros = np.zeros(HIST, "f4")
                inst.append(np.stack([np.full(HIST, xl), z, left_h, np.full(HIST, k / 7), zeros], 1))
                inst.append(np.stack([np.full(HIST, xr), z, right_h, np.full(HIST, (7 - k) / 7), zeros], 1))
            return np.concatenate(inst).astype("f4"), 0.45, 0.42, 0.0
        ang = b / BANDS * 2 * np.pi  # ring
        r = 5.0
        ring = np.stack([np.cos(ang) * r, np.sin(ang) * r, self.levels * 4.8, band_t, np.zeros(BANDS)], axis=1)
        return ring.astype("f4"), 0.36, 0.36, 9.0

    def _spawn_sparks(self, n):
        if self.mode == 3:  # speed streaks flying past
            x = np.random.choice([-1, 1], n) * np.random.uniform(1.5, 6.0, n)
            new = np.column_stack([x, np.random.uniform(0.2, 4.0, n), np.random.uniform(-40, -25, n),
                                   np.zeros(n), np.random.uniform(-1, 1, n), np.random.uniform(45, 70, n),
                                   np.random.uniform(0.7, 1.0, n), np.random.rand(n)]).astype("f4")
            self.sparks = np.concatenate([self.sparks, new])[-3500:]
            return
        inst, *_ = self._layout()
        if self.mode == 0:
            inst = inst[:BANDS]  # newest row
        tall = inst[np.argsort(-inst[:, 2])[: max(4, len(inst) // 6)]]
        pick = tall[np.random.randint(0, len(tall), n)]
        vel = np.random.randn(n, 3).astype("f4") * [1.2, 0.8, 1.2] + [0, 5.0, 0]
        new = np.column_stack([pick[:, 0], pick[:, 2] + 0.1, pick[:, 1], vel,
                               np.random.uniform(0.6, 1.0, n), pick[:, 3]]).astype("f4")
        self.sparks = np.concatenate([self.sparks, new])[-3500:]

    # -- draw ------------------------------------------------------------------
    def render(self, fbo):
        ctx = self.ctx
        pal, pal_n = palette_array(self.palette_idx)
        inst, bw, bd, target_y = self._layout()
        self.inst_vbo.orphan(inst.nbytes)
        self.inst_vbo.write(inst.astype("f4").tobytes())

        zoom_kick = 1.0 - self.beat * 0.04
        eye = np.array([
            math.sin(self.yaw) * math.cos(self.pitch),
            math.sin(self.pitch),
            math.cos(self.yaw) * math.cos(self.pitch),
        ]) * self.dist * zoom_kick
        center = np.array([0.0, 1.0, {0: -8.5, 3: -14.0}.get(self.mode, 0.0)])
        eye = eye + center
        proj = perspective(math.radians(45), self.w / self.h, 0.1, 200)
        mvp = (proj @ look_at(eye, center)).T.copy()
        fog_far = self.dist * 2.6 + 10

        for p in (self.bar_prog, self.floor_prog, self.bg_prog):
            p["palette"].write(pal.tobytes())
            p["pal_n"].value = pal_n
        for p, names in ((self.bar_prog, ("beat", "dim", "fog_far")), (self.floor_prog, ("beat", "dim", "fog_far")),
                         (self.bg_prog, ("beat", "dim"))):
            for nm in names:
                p[nm].value = {"beat": self.beat, "dim": self.dim, "fog_far": fog_far}[nm]

        # 1) scene into HDR buffer
        self.scene_fbo.use()
        ctx.viewport = (0, 0, self.w, self.h)
        self.scene_fbo.clear(0.0, 0.0, 0.0, 1.0, depth=1.0)
        ctx.disable(ctx.DEPTH_TEST | ctx.BLEND)
        self.quads[self.bg_prog].render(ctx.TRIANGLE_STRIP)  # no depth test = no depth writes

        ctx.enable(ctx.DEPTH_TEST)
        bp = self.bar_prog
        bp["mvp"].write(mvp.tobytes())
        bp["cam_pos"].value = tuple(eye)
        bp["bar_w"].value, bp["bar_d"].value = bw, bd
        n_inst = len(inst)
        # reflections first (below floor), then the floor, then real bars
        ctx.front_face = "cw"
        bp["mirror"].value = -1.0
        self.bar_vao.render(instances=n_inst)
        ctx.front_face = "ccw"
        ctx.enable(ctx.BLEND)
        ctx.blend_func = ctx.SRC_ALPHA, ctx.ONE_MINUS_SRC_ALPHA
        fp = self.floor_prog
        fp["mvp"].write(mvp.tobytes())
        fp["cam_pos"].value = tuple(eye)
        fp["size"].value = 60.0
        fp["time"].value = self.t
        fp["road"].value = 1.0 if self.mode == 3 else 0.0
        fp["road_off"].value = self.road_off
        ctx.depth_mask = False
        self.floor_vao.render(ctx.TRIANGLE_STRIP)
        ctx.depth_mask = True
        ctx.disable(ctx.BLEND)
        bp["mirror"].value = 1.0
        self.bar_vao.render(instances=n_inst)

        # sparks (additive)
        if len(self.sparks):
            s = self.sparks
            cols = np.array([_pal_lookup(pal, pal_n, t) for t in s[:, 7]], "f4")
            data = np.column_stack([s[:, 0:3], cols, s[:, 6]]).astype("f4")
            self.spark_vbo.orphan(data.nbytes)
            self.spark_vbo.write(data.tobytes())
            sp = self.spark_prog
            sp["mvp"].write(mvp.tobytes())
            sp["px"].value = 22.0 * self.h / 720
            sp["dim"].value = self.dim
            ctx.enable(ctx.BLEND | ctx.PROGRAM_POINT_SIZE)
            ctx.blend_func = ctx.ONE, ctx.ONE
            ctx.depth_mask = False
            self.spark_vao.render(ctx.POINTS, vertices=len(data))
            ctx.depth_mask = True
            ctx.disable(ctx.BLEND)
        ctx.disable(ctx.DEPTH_TEST)

        # 2) bloom: bright pass -> blur at 1/2 and 1/4 res
        src = self.scene_tex
        for size, a, b, fa, fb in self.bloom:
            fa.use()
            ctx.viewport = (0, 0, *size)
            src.use(0)
            self.bright_prog["tex"].value = 0
            self.quads[self.bright_prog].render(ctx.TRIANGLE_STRIP)
            for _ in range(2):
                fb.use()
                a.use(0)
                self.blur_prog["tex"].value = 0
                self.blur_prog["dir"].value = (1.5 / size[0], 0)
                self.quads[self.blur_prog].render(ctx.TRIANGLE_STRIP)
                fa.use()
                b.use(0)
                self.blur_prog["dir"].value = (0, 1.5 / size[1])
                self.quads[self.blur_prog].render(ctx.TRIANGLE_STRIP)
            src = a

        # 3) composite + tone map to the window
        fbo.use()
        ctx.viewport = (0, 0, self.w, self.h)
        self.scene_tex.use(0)
        self.bloom[0][1].use(1)
        self.bloom[1][1].use(2)
        cp = self.comp_prog
        cp["scene"].value, cp["bloom1"].value, cp["bloom2"].value = 0, 1, 2
        cp["bloom_amt"].value = 0.55 + self.beat * 0.7
        self.quads[self.comp_prog].render(ctx.TRIANGLE_STRIP)


def _pal_lookup(pal, n, t):
    t = min(max(float(t), 0.0), 0.9999) * (n - 1)
    i = int(t)
    f = t - i
    return pal[i] * (1 - f) + pal[min(i + 1, n - 1)] * f


# ----------------------------------------------------------------- window --


def run_window(shm_name):
    """Entry point for the visualizer process (HomePodLink.exe --viz <shm>)."""
    from multiprocessing import shared_memory

    import moderngl
    import pyglet

    shm = shared_memory.SharedMemory(name=shm_name)
    try:  # POSIX only: don't let this process's tracker delete the parent's memory
        from multiprocessing import resource_tracker

        resource_tracker.unregister(shm._name, "shared_memory")
    except Exception:
        pass
    data = np.ndarray((SHM_FLOATS,), dtype="f4", buffer=shm.buf)

    cfg = pyglet.gl.Config(major_version=3, minor_version=3, forward_compatible=True,
                           double_buffer=True, depth_size=24, sample_buffers=1, samples=4)
    try:
        win = pyglet.window.Window(1280, 720, "HomePod Link · 3D Visualizer", resizable=True, config=cfg, vsync=True)
    except Exception:
        cfg = pyglet.gl.Config(major_version=3, minor_version=3, forward_compatible=True,
                               double_buffer=True, depth_size=24)
        win = pyglet.window.Window(1280, 720, "HomePod Link · 3D Visualizer", resizable=True, config=cfg, vsync=True)
    ctx = moderngl.create_context(require=330)
    r = Renderer(ctx, *win.get_framebuffer_size())
    state = {"last": time.monotonic(), "title_t": 0.0, "hidden": False, "help_until": time.monotonic() + 6}

    def set_title(extra=""):
        name = MODE_NAMES[r.mode]
        help_txt = "  —  drag to spin · scroll to zoom · double-click reset · 1-4 modes · F full screen"
        win.set_caption(f"HomePod Link · {name}{extra}" + (help_txt if time.monotonic() < state["help_until"] else ""))

    import os

    snap = {"path": os.environ.get("HOMEPOD_VIZ_SNAPSHOT"), "frames": 0}

    @win.event
    def on_draw():
        r.resize(*win.get_framebuffer_size())
        r.render(ctx.screen)
        snap["frames"] += 1
        if snap["path"] and snap["frames"] == int(os.environ.get("HOMEPOD_VIZ_SNAPSHOT_FRAME", "240")):  # debug: save what the window shows
            from PIL import Image

            w, h = win.get_framebuffer_size()
            Image.frombytes("RGB", (w, h), ctx.screen.read(components=3)).transpose(
                Image.FLIP_TOP_BOTTOM).save(snap["path"])

    @win.event
    def on_mouse_drag(x, y, dx, dy, buttons, mods):
        r.drag(dx, -dy)

    @win.event
    def on_mouse_scroll(x, y, sx, sy):
        r.zoom(sy)

    last_click = [0.0]

    @win.event
    def on_mouse_press(x, y, button, mods):
        now = time.monotonic()
        if now - last_click[0] < 0.35:
            r.reset_view()
        last_click[0] = now

    @win.event
    def on_key_press(sym, mods):
        k = pyglet.window.key
        if sym in (k._1, k._2, k._3, k._4):
            r.mode = {k._1: 0, k._2: 1, k._3: 2, k._4: 3}[sym]
            data[4] = r.mode
            set_title()
        elif sym in (k.F, k.F11):
            win.set_fullscreen(not win.fullscreen)
        elif sym == k.ESCAPE and win.fullscreen:
            win.set_fullscreen(False)
            return pyglet.event.EVENT_HANDLED

    @win.event
    def on_hide():
        state["hidden"] = True

    @win.event
    def on_show():
        state["hidden"] = False

    def tick(_dt):
        now = time.monotonic()
        dt = min(0.1, now - state["last"])
        state["last"] = now
        if data[5] > 0.5:  # main app closed
            pyglet.app.exit()
            return
        paused = data[3] > 0.5 or state["hidden"]
        r.palette_idx = int(data[2])
        r.mode = int(data[4]) % 4
        target_dim = 0.25 if paused else 1.0
        r.dim += (target_dim - r.dim) * min(1, dt * 4)
        if not paused:
            r.feed(data[8:8 + BANDS].copy(), dt)
        state["title_t"] += dt
        if state["title_t"] > 1:
            state["title_t"] = 0
            set_title("  (paused while your sim runs)" if data[3] > 0.5 else "")

    set_title()
    pyglet.clock.schedule_interval(tick, 1 / 120)
    pyglet.app.run(interval=1 / 240)  # redraw continuously; vsync caps it at the monitor refresh rate
    shm.close()


class Analyzer:
    """Measured spectrum, no decoration.

    Multi-resolution FFT like pro analyzers: below 150 Hz a 16384-point window
    (2.7 Hz resolution), 150-400 Hz 8192, 400 Hz-2 kHz 4096, above 2 kHz 1024
    (23 ms, so transients show immediately). Hann windows, calibrated so a
    full-scale sine reads 0 dBFS (window ENBW corrected). Each band is the total
    power within it, on log-spaced bands, so pink noise reads flat. Range shown:
    -66 to 0 dBFS."""

    RANGE_DB = 66.0

    def __init__(self, sample_rate, bands=BANDS):
        self.sr = sample_rate
        edges = np.geomspace(30, 16000, bands + 1)
        self.centers = np.sqrt(edges[:-1] * edges[1:])
        self.plans = []
        for n, lo_f, hi_f in ((16384, 0, 150), (8192, 150, 400), (4096, 400, 2000), (1024, 2000, 1e9)):
            win = np.hanning(n).astype("f8")
            fr = np.fft.rfftfreq(n, 1 / sample_rate)
            sel = [i for i, c in enumerate(self.centers) if lo_f <= c < hi_f]
            ranges = []
            for i in sel:
                lo = int(np.searchsorted(fr, edges[i]))
                hi = max(int(np.searchsorted(fr, edges[i + 1])), lo + 1)
                ranges.append((i, lo, hi))
            self.plans.append((n, win, 2.0 / win.sum(), ranges))
        # Log-spaced bands that sum power already show pink noise flat: no extra tilt.
        self.tilt = np.zeros(bands)
        self.db = np.full(bands, -120.0)

    def measure(self, mono):
        """mono: latest samples (>= 16384). Returns 0..1 per band; also sets self.db."""
        db = np.empty(len(self.centers))
        for n, win, scale, ranges in self.plans:
            amp = np.abs(np.fft.rfft(mono[-n:] * win)) * scale  # sine amplitude per bin
            pw = amp * amp / 2.0  # power (mean square) per bin
            for i, lo, hi in ranges:
                # /1.5: Hann equivalent-noise bandwidth; x2: full-scale sine = 0 dBFS
                db[i] = 10 * np.log10(pw[lo:hi].sum() / 1.5 * 2.0 + 1e-14)
        self.db = db
        return np.clip((db + self.tilt + self.RANGE_DB) / self.RANGE_DB, 0, 1).astype("f4")


class VizBridge:
    """Main-app side: measures the live audio and publishes it to the visualizer
    process through shared memory. Delayed by the speaker latency so the picture
    matches what you hear from the HomePods, not what the PC is about to send."""

    def __init__(self, timeline, sample_rate, log, delay_s=lambda: 0.0):
        from multiprocessing import shared_memory

        self.timeline, self.sr, self.log, self.delay_s = timeline, sample_rate, log, delay_s
        self.shm = shared_memory.SharedMemory(create=True, size=SHM_FLOATS * 4)
        self.data = np.ndarray((SHM_FLOATS,), dtype="f4", buffer=self.shm.buf)
        self.data[:] = 0
        self.proc = None
        self.alive = True
        self.analyzer = Analyzer(sample_rate)
        import threading

        threading.Thread(target=self._loop, daemon=True).start()

    def open(self, palette_idx=0, mode=0):
        import subprocess
        import sys

        self.data[2], self.data[4], self.data[5] = palette_idx, mode, 0
        if self.proc and self.proc.poll() is None:
            return
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--viz", self.shm.name]
        else:
            import os

            cmd = [sys.executable, os.path.abspath(sys.argv[0]), "--viz", self.shm.name]
        self.proc = subprocess.Popen(cmd)
        self.log("3D visualizer opened")

    def is_open(self):
        return self.proc is not None and self.proc.poll() is None

    def set(self, palette_idx=None, mode=None, paused=None):
        if palette_idx is not None:
            self.data[2] = palette_idx
        if mode is not None:
            self.data[4] = mode
        if paused is not None:
            self.data[3] = 1.0 if paused else 0.0

    def _loop(self):
        n = 16384
        nxt = time.monotonic()
        while self.alive:
            nxt += 1 / 120
            time.sleep(max(0.0, nxt - time.monotonic()))
            if not self.is_open():
                nxt = time.monotonic()
                continue
            tl = self.timeline
            lag = int(self.delay_s() * self.sr)
            with tl.lock:
                wp = tl.write_pos
                if wp is None:
                    continue
                end = tl.now_pos() - lag  # what the speakers are playing right now
                end = min(end, wp)
                a = (end - n) % tl.size
                x = np.take(tl.ring[:, 0] + tl.ring[:, 1], range(a, a + n), mode="wrap") * 0.5
            self.data[8:8 + BANDS] = self.analyzer.measure(x.astype("f8"))
            self.data[0] += 1

    def close(self):
        self.alive = False
        self.data[5] = 1
        time.sleep(0.1)
        try:
            self.shm.close()
            self.shm.unlink()
        except Exception:
            pass
