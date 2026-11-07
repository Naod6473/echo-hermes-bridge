/* Hermes avatar: a vector portrait that blinks, talks and reacts, in three styles.
 *
 *   const a = HermesAvatar.create(canvas, data, { style: "medallion" | "spectrum" | "hologram", ink: "white" | "black" });
 *   a.frame(timeMs, state, level)      // state: idle | listening | thinking | speaking | error; level 0..1
 *
 * `data` is an avatar.json made by tools/make_avatar.py: SVG path strings in a 640 x 640 space for the
 * drawing ("base") and each eye, plus where each eye closes and which box is the lower lip.
 *  - ink "white": light lines on the dark screen (the drawing as white ink);
 *  - ink "black": the positive print, dark lines on a light disc (a cameo).
 */
(function () {
  "use strict";
  const mk = (w, h) => { const c = document.createElement("canvas"); c.width = w; c.height = h; return c; };
  const RES = 2;                                        // the portrait is rendered at 1280 px: sharp on 4K

  function prepare(data) {
    const eyes = (data.eyes || []).map((e) => ({ ...e, path: new Path2D(e.d) }));
    const lip = data.lip;                               // [x, y, w, h] or null
    const notLip = new Path2D(); notLip.rect(-50, -50, 740, 740);
    const lipBox = new Path2D();
    if (lip) { notLip.rect(lip[0], lip[1], lip[2], lip[3]); lipBox.rect(lip[0], lip[1], lip[2], lip[3]); }
    const disc = new Path2D(); disc.arc(320, 320, 318, 0, Math.PI * 2);
    return { base: new Path2D(data.base), eyes, lip, notLip, lipBox, disc, neck: data.neck || [320, 560] };
  }

  function create(canvas, data, opts) {
    const P = prepare(data);
    const ctx = canvas.getContext("2d");
    const tmp = mk(640 * RES, 640 * RES), g = tmp.getContext("2d");
    const style = opts.style || "medallion", ink = opts.ink === "black" ? "black" : "white";
    const st8 = { lvl: 0, grow: 0, nextBlink: 900 + Math.random() * 2500, blinkAt: -1e9, double: false,
                  ripples: [], lastRipple: 0, bars: new Float32Array(72), wave: new Float32Array(90) };

    function fit() {
      const r = canvas.getBoundingClientRect(), d = Math.min(window.devicePixelRatio || 1, 2);
      const px = Math.max(64, Math.round(r.width * d));
      if (canvas.width !== px) { canvas.width = px; canvas.height = px; }
    }
    if (window.ResizeObserver) new ResizeObserver(fit).observe(canvas);
    fit();

    function blink(t, state) {
      if (t > st8.nextBlink) {
        st8.blinkAt = t; st8.double = Math.random() < .25;
        st8.nextBlink = t + (state === "thinking" ? 1400 : 2300) + Math.random() * 3800;
      }
      const one = (k) => k >= 0 && k <= 1 ? 1 - Math.sin(k * Math.PI) * .93 : 1;
      const k = (t - st8.blinkAt) / 150;
      return Math.min(one(k), st8.double ? one(k - 1.6) : 1);
    }

    /** The drawing on the offscreen canvas, tinted with `tint` (a color or a gradient maker). */
    function portrait(t, state, tint) {
      g.setTransform(1, 0, 0, 1, 0, 0); g.globalCompositeOperation = "source-over"; g.globalAlpha = 1;
      g.clearRect(0, 0, 640 * RES, 640 * RES);
      g.setTransform(RES, 0, 0, RES, 0, 0);
      const tilt = Math.sin(t / 2900) * .007 + (state === "speaking" ? Math.sin(t / 420) * st8.lvl * .008 : 0);
      g.translate(P.neck[0], P.neck[1]); g.rotate(tilt); g.translate(-P.neck[0], -P.neck[1]);
      g.fillStyle = "#fff";
      if (ink === "black") { g.fill(P.disc); g.globalCompositeOperation = "destination-out"; }  // ink cut out of a disc
      const inkOp = g.globalCompositeOperation, eraseOp = ink === "black" ? "source-over" : "destination-out";
      const open = state === "speaking" ? st8.lvl * 4.5 : 0;
      if (P.lip) {
        g.save(); g.clip(P.notLip, "evenodd"); g.fill(P.base, "evenodd"); g.restore();
        g.save(); g.clip(P.lipBox); g.translate(0, open); g.fill(P.base, "evenodd"); g.restore();
      } else g.fill(P.base, "evenodd");
      const s = blink(t, state);
      for (const e of P.eyes) {
        // an eye drawn in ink closes as ink; an eye cut out of the ink ("cut") closes as a gap
        g.save(); g.globalCompositeOperation = e.cut ? eraseOp : inkOp;
        g.translate(0, e.line); g.scale(1, s); g.translate(0, -e.line); g.fill(e.path, "evenodd"); g.restore();
        if (s < .75 && e.lid) {
          const [p0, c, p1] = e.lid;
          g.save(); g.globalCompositeOperation = e.cut ? eraseOp : inkOp;
          g.globalAlpha = Math.min(1, (.75 - s) * 2.2); g.strokeStyle = "#fff"; g.lineCap = "round"; g.lineWidth = e.w || 4;
          g.beginPath(); g.moveTo(p0[0], p0[1]); g.quadraticCurveTo(c[0], c[1], p1[0], p1[1]); g.stroke(); g.restore();
        }
      }
      g.setTransform(RES, 0, 0, RES, 0, 0); g.globalAlpha = 1;
      if (tint) { g.globalCompositeOperation = "source-in"; g.fillStyle = typeof tint === "function" ? tint(g) : tint; g.fillRect(0, 0, 640, 640); }
      g.globalCompositeOperation = "source-over";
      return tmp;
    }

    const grad = (a, b) => (c) => { const x = c.createLinearGradient(80, 40, 560, 600); x.addColorStop(0, a); x.addColorStop(1, b); return x; };
    const breathe = (t) => 1 + .012 * Math.sin(t / 1700);

    // ------------------------------------------------------------------ A: medallion
    function medallion(c, t, state) {
      const cx = 320, cy = 320, R = 210;
      const scale = breathe(t) * (1 + st8.grow * .08 + st8.lvl * .07);
      if (state === "speaking" && st8.lvl > .55 && t - st8.lastRipple > 220) { st8.ripples.push({ t, s: st8.lvl }); st8.lastRipple = t; }
      if (state === "listening" && t - st8.lastRipple > 700) { st8.ripples.push({ t, s: .5 }); st8.lastRipple = t; }
      st8.ripples = st8.ripples.filter((p) => t - p.t < 1500);
      for (const p of st8.ripples) {
        const k = (t - p.t) / 1500;
        c.beginPath(); c.arc(cx, cy, R * scale * (1.02 + k * .48), 0, Math.PI * 2);
        c.strokeStyle = state === "listening" ? `rgba(34,211,238,${(1 - k) * .55})` : `rgba(140,190,255,${(1 - k) * .6 * p.s})`;
        c.lineWidth = 3 + (1 - k) * 5; c.stroke();
      }
      c.save(); c.translate(cx, cy); c.scale(scale, scale); c.translate(-cx, -cy);
      const glow = c.createRadialGradient(cx, cy, R * .6, cx, cy, R * 1.35);
      const hot = state === "thinking" ? "167,139,250" : state === "error" ? "251,113,133" : "34,211,238";
      glow.addColorStop(0, `rgba(${hot},${.25 + st8.lvl * .35})`); glow.addColorStop(1, "rgba(0,0,0,0)");
      c.fillStyle = glow; c.fillRect(0, 0, 640, 640);
      c.beginPath(); c.arc(cx, cy, R, 0, Math.PI * 2);
      const disc = c.createRadialGradient(cx - 60, cy - 80, 20, cx, cy, R);
      disc.addColorStop(0, "#1b2260"); disc.addColorStop(1, "#090c22");
      c.fillStyle = disc; c.fill();
      c.save(); c.clip();
      const tint = ink === "black" ? grad("#f4f5ff", "#cfd3fb") : grad("#ffffff", "#c9cdfb");
      c.drawImage(portrait(t, state, tint), cx - R * 1.05, cy - R * .98, R * 2.1, R * 2.1);
      c.restore();
      const spin = t / (state === "thinking" ? 300 : 2400);
      const ring = c.createConicGradient ? c.createConicGradient(spin, cx, cy) : null;
      if (ring) {
        const a = state === "error" ? "#fb7185" : "#22d3ee";
        ring.addColorStop(0, a); ring.addColorStop(.35, "#a78bfa"); ring.addColorStop(.55, "rgba(167,139,250,.15)");
        ring.addColorStop(.75, a); ring.addColorStop(1, a);
      }
      c.beginPath(); c.arc(cx, cy, R + 7, 0, Math.PI * 2); c.strokeStyle = ring || "#22d3ee"; c.lineWidth = 9; c.stroke();
      c.restore();
    }

    // ------------------------------------------------------------------ B: radial spectrum
    function spectrum(c, t, state) {
      const cx = 320, cy = 320, R = 200, N = st8.bars.length;
      const scale = breathe(t) * (1 + st8.grow * .07 + st8.lvl * .05);
      for (let i = 0; i < N; i++) {
        let v;
        if (state === "thinking") { const a = (((i / N) - t / 1300) % 1 + 1) % 1; v = .1 + .7 * Math.pow(Math.max(0, Math.cos(a * Math.PI * 2)), 6); }
        else if (state === "idle" || state === "error") v = .05 + .04 * Math.sin(i * .5 + t / 600);
        else v = st8.lvl * (.3 + .7 * Math.abs(Math.sin(i * 1.31 + t / 95) * Math.sin(i * .67 - t / 160)));
        st8.bars[i] += (v - st8.bars[i]) * .35;
      }
      c.save(); c.translate(cx, cy); c.lineCap = "round"; c.lineWidth = 9;
      for (let i = 0; i < N; i++) {
        const a = i / N * Math.PI * 2 - Math.PI / 2, len = 10 + st8.bars[i] * 95, r0 = R * scale + 14;
        const hue = state === "error" ? 350 : 190 + 80 * (0.5 + 0.5 * Math.sin(a));
        c.strokeStyle = `hsl(${hue} 90% ${62 + st8.bars[i] * 18}%)`;
        c.beginPath(); c.moveTo(Math.cos(a) * r0, Math.sin(a) * r0); c.lineTo(Math.cos(a) * (r0 + len), Math.sin(a) * (r0 + len)); c.stroke();
      }
      c.restore();
      c.save(); c.translate(cx, cy); c.scale(scale, scale);
      c.shadowColor = "rgba(34,211,238,.55)"; c.shadowBlur = 30 + st8.lvl * 30;
      const tint = ink === "black" ? grad("#a5f3fc", "#ddd6fe") : grad("#67e8f9", "#c4b5fd");
      c.drawImage(portrait(t, state, tint), -R, -R, R * 2, R * 2);
      c.restore();
    }

    // ------------------------------------------------------------------ C: hologram
    function hologram(c, t, state) {
      const cx = 320, cy = 290, S = 400;
      const scale = breathe(t) * (1 + st8.grow * .08 + st8.lvl * .06);
      const beam = c.createLinearGradient(0, 610, 0, 120);
      beam.addColorStop(0, "rgba(34,211,238,.28)"); beam.addColorStop(1, "rgba(34,211,238,0)");
      c.fillStyle = beam; c.beginPath(); c.moveTo(250, 610); c.lineTo(390, 610); c.lineTo(560, 90); c.lineTo(80, 90); c.closePath(); c.fill();
      const p = portrait(t, state, state === "error" ? "#fda4af" : "#7df3ff");
      c.save(); c.translate(cx, cy); c.scale(scale, scale);
      const jitter = state === "thinking" ? (Math.random() < .2 ? (Math.random() - .5) * 18 : 0) : 0;
      c.globalCompositeOperation = "lighter";
      if (state === "thinking") {
        c.globalAlpha = .5; c.filter = "hue-rotate(110deg)"; c.drawImage(p, -S / 2 - 6 + jitter, -S / 2, S, S);
        c.filter = "hue-rotate(-60deg)"; c.drawImage(p, -S / 2 + 6 - jitter, -S / 2, S, S); c.filter = "none";
      }
      c.globalAlpha = ink === "black" ? .75 : .9; c.shadowColor = "#22d3ee"; c.shadowBlur = 24;
      c.drawImage(p, -S / 2 + jitter * .3, -S / 2, S, S);
      c.restore();
      c.globalAlpha = 1; c.globalCompositeOperation = "destination-out"; c.fillStyle = "rgba(0,0,0,.35)";
      const off = (t / 40) % 6;
      for (let y = off; y < 640; y += 6) c.fillRect(0, y, 640, 2);
      c.globalCompositeOperation = "source-over";
      st8.wave.copyWithin(0, 1); st8.wave[st8.wave.length - 1] = state === "idle" ? .03 : st8.lvl;
      const y0 = 575, n = st8.wave.length;
      c.beginPath();
      for (let i = 0; i < n; i++) {
        const x = 120 + i / (n - 1) * 400, h = 4 + st8.wave[i] * 46 * Math.abs(Math.sin(i * .9 + t / 80));
        c.moveTo(x, y0 - h); c.lineTo(x, y0 + h);
      }
      c.strokeStyle = state === "thinking" ? "#c4b5fd" : "#67e8f9"; c.lineWidth = 2.5; c.lineCap = "round"; c.stroke();
    }

    const draw = { medallion, spectrum, hologram }[style] || medallion;
    return {
      frame(t, state, level) {
        const lv = state === "speaking" || state === "listening" ? Math.max(0, Math.min(1, level || 0)) : 0;
        st8.lvl += (lv - st8.lvl) * (lv > st8.lvl ? .5 : .15);
        st8.grow += ((state === "speaking" ? 1 : 0) - st8.grow) * .06;
        const W = canvas.width;
        ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, W, W);
        ctx.save(); ctx.scale(W / 640, W / 640); draw(ctx, t, state); ctx.restore();
      },
    };
  }

  window.HermesAvatar = { create, STYLES: ["medallion", "spectrum", "hologram"] };
})();
