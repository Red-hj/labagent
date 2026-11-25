/* Original rounded 3D keycap visual + high-FPS shadow/grid caching + slow-descending mouse trail & click ripples */
(() => {
  "use strict";
  const root = document.getElementById("ambient-network");
  const canvas = root?.querySelector("canvas");
  if (!root || !canvas || typeof canvas.getContext !== "function") return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  const CELL = 28;
  const MAJOR_EVERY = 4;
  const HOVER_RADIUS = 118;
  const HOVER_RADIUS_SQ = HOVER_RADIUS * HOVER_RADIUS;
  const RIPPLE_DURATION = 1.75;
  const RIPPLE_RING_WIDTH = 62;
  const RIPPLE_PEAK_DIST = 115;
  const RIPPLE_TRAIL_LAG = 76;
  const MAX_RIPPLES = 5;
  const CORNER_R = 2.4;

  // Frame-rate-independent time constants (seconds): prompt rise, slow graceful descent!
  const RISE_TAU = 0.06;
  const FALL_TAU = 0.46;

  const SHADOW_SIZE = 76;
  const SHADOW_PAD = 18;
  const SHADOW_INNER = SHADOW_SIZE - SHADOW_PAD * 2;
  const Z_BUCKETS = 24;
  const hasNativeRoundRect = typeof ctx.roundRect === "function";

  const gridCache = document.createElement("canvas");
  const gridCacheCtx = gridCache.getContext("2d");
  const shadowSprite = document.createElement("canvas");
  shadowSprite.width = SHADOW_SIZE;
  shadowSprite.height = SHADOW_SIZE;
  const shadowCtx = shadowSprite.getContext("2d");

  const reduced = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
  let paused = Boolean(reduced);
  document.body.classList.toggle("animations-paused", paused);
  root.dataset.engine = "interactive-3d-ripple-grid";

  let width = 0;
  let height = 0;
  let dpr = 1;
  let cols = 0;
  let rows = 0;
  let totalCells = 0;

  // Pre-allocated typed buffers (zero GC pauses)
  let elevations = new Float32Array(0);
  let targetZArr = new Float32Array(0);
  let weightArr = new Float32Array(0);
  let sumXArr = new Float32Array(0);
  let sumYArr = new Float32Array(0);
  let sourceXArr = new Float32Array(0);
  let sourceYArr = new Float32Array(0);
  let activeFlag = new Uint8Array(0);
  let activeList = new Int32Array(0);
  let activeCount = 0;
  let bucketHeads = new Int32Array(Z_BUCKETS);
  let bucketNext = new Int32Array(0);

  let rafId = 0;
  let lastFrameTime = 0;
  let pointerActive = false;
  let mouseX = -9999;
  let mouseY = -9999;
  let smoothX = -9999;
  let smoothY = -9999;
  let cachedTheme = "";
  let cachedPal = null;
  const ripples = [];

  const motionButton = document.getElementById("motion-toggle");
  if (motionButton) {
    motionButton.setAttribute("aria-pressed", String(paused));
    motionButton.title = paused ? "开启网格鼠标浮动与点击涟漪" : "暂停网格鼠标浮动与点击涟漪";
  }

  motionButton?.addEventListener("click", event => {
    paused = !paused;
    document.body.classList.toggle("animations-paused", paused);
    event.currentTarget.setAttribute("aria-pressed", String(paused));
    event.currentTarget.title = paused ? "开启网格鼠标浮动与点击涟漪" : "暂停网格鼠标浮动与点击涟漪";
    if (paused) ripples.length = 0;
    ensureLoop();
  });

  // Exact original beloved palette & lighting parameters
  function getPalette() {
    const dark = document.documentElement.dataset.theme === "dark";
    const key = dark ? "dark" : "light";
    if (cachedTheme === key && cachedPal) return cachedPal;
    cachedTheme = key;
    cachedPal = dark
      ? {
          dark: true,
          minor: "rgba(148, 163, 184, 0.056)",
          major: "rgba(56, 189, 248, 0.086)",
          capBase: "#101d34",
          sideBase: [7, 13, 26],
          shadowColor: "rgba(0, 0, 0, 0.82)",
          creaseRgb: "0, 0, 0",
          creaseAlpha: 0.42,
          borderRgb: "56, 189, 248",
          specularRgb: "186, 230, 253",
          specularAlpha: 0.56,
          peakAlpha: 0.54,
          topRgb: [45, 212, 191],
          midRgb: [56, 189, 248],
          coreRgb: [125, 211, 252],
          botRgb: [129, 140, 248]
        }
      : {
          dark: false,
          minor: "rgba(15, 23, 42, 0.046)",
          major: "rgba(2, 132, 199, 0.074)",
          capBase: "#ffffff",
          sideBase: [194, 213, 232],
          shadowColor: "rgba(15, 23, 42, 0.24)",
          creaseRgb: "15, 23, 42",
          creaseAlpha: 0.09,
          borderRgb: "2, 132, 199",
          specularRgb: "255, 255, 255",
          specularAlpha: 0.96,
          peakAlpha: 0.37,
          topRgb: [13, 148, 136],
          midRgb: [2, 132, 199],
          coreRgb: [14, 165, 233],
          botRgb: [79, 70, 229]
        };
    bakeShadowSprite(cachedPal);
    bakeGridCache(cachedPal);
    return cachedPal;
  }

  function mixRgb(a, b, t, out) {
    const k = t < 0 ? 0 : t > 1 ? 1 : t;
    out[0] = (a[0] + (b[0] - a[0]) * k + 0.5) | 0;
    out[1] = (a[1] + (b[1] - a[1]) * k + 0.5) | 0;
    out[2] = (a[2] + (b[2] - a[2]) * k + 0.5) | 0;
  }

  function roundedRectPath(targetCtx, x, y, w, h, r) {
    targetCtx.beginPath();
    if (hasNativeRoundRect && targetCtx === ctx) {
      targetCtx.roundRect(x, y, w, h, r);
      return;
    }
    const cr = Math.min(r, w * 0.5, h * 0.5);
    targetCtx.moveTo(x + cr, y);
    targetCtx.lineTo(x + w - cr, y);
    targetCtx.quadraticCurveTo(x + w, y, x + w, y + cr);
    targetCtx.lineTo(x + w, y + h - cr);
    targetCtx.quadraticCurveTo(x + w, y + h, x + w - cr, y + h);
    targetCtx.lineTo(x + cr, y + h);
    targetCtx.quadraticCurveTo(x, y + h, x, y + h - cr);
    targetCtx.lineTo(x, y + cr);
    targetCtx.quadraticCurveTo(x, y, x + cr, y);
    targetCtx.closePath();
  }

  // Pre-bake pure full-strength Gaussian shadow ONCE per theme (caster offset off-canvas so alpha isn't squared!)
  function bakeShadowSprite(pal) {
    if (!shadowCtx) return;
    const off = 256;
    shadowCtx.clearRect(0, 0, SHADOW_SIZE, SHADOW_SIZE);
    shadowCtx.save();
    shadowCtx.shadowColor = pal.shadowColor;
    shadowCtx.shadowBlur = 14;
    shadowCtx.shadowOffsetX = off;
    shadowCtx.shadowOffsetY = 0;
    shadowCtx.fillStyle = "#000000";
    roundedRectPath(shadowCtx, SHADOW_PAD - off, SHADOW_PAD, SHADOW_INNER, SHADOW_INNER, 4);
    shadowCtx.fill();
    shadowCtx.restore();
  }

  // Pre-bake stationary straight grid + smooth radial vignette ONCE on resize/theme change
  function bakeGridCache(pal) {
    if (!gridCacheCtx || !width || !height) return;
    gridCache.width = canvas.width;
    gridCache.height = canvas.height;
    gridCacheCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    gridCacheCtx.clearRect(0, 0, width, height);

    gridCacheCtx.beginPath();
    for (let i = 0; i <= cols; i++) {
      if (i % MAJOR_EVERY === 0) continue;
      const x = i * CELL + 0.5;
      gridCacheCtx.moveTo(x, 0);
      gridCacheCtx.lineTo(x, height);
    }
    for (let j = 0; j <= rows; j++) {
      if (j % MAJOR_EVERY === 0) continue;
      const y = j * CELL + 0.5;
      gridCacheCtx.moveTo(0, y);
      gridCacheCtx.lineTo(width, y);
    }
    gridCacheCtx.lineWidth = 1;
    gridCacheCtx.strokeStyle = pal.minor;
    gridCacheCtx.stroke();

    gridCacheCtx.beginPath();
    for (let i = 0; i <= cols; i += MAJOR_EVERY) {
      const x = i * CELL + 0.5;
      gridCacheCtx.moveTo(x, 0);
      gridCacheCtx.lineTo(x, height);
    }
    for (let j = 0; j <= rows; j += MAJOR_EVERY) {
      const y = j * CELL + 0.5;
      gridCacheCtx.moveTo(0, y);
      gridCacheCtx.lineTo(width, y);
    }
    gridCacheCtx.lineWidth = 1;
    gridCacheCtx.strokeStyle = pal.major;
    gridCacheCtx.stroke();

    gridCacheCtx.save();
    gridCacheCtx.globalCompositeOperation = "destination-in";
    const cx = width * 0.56;
    const cy = height * 0.48;
    const rx = Math.max(1, width * 0.92);
    const ry = Math.max(1, height * 0.82);
    gridCacheCtx.translate(cx, cy);
    gridCacheCtx.scale(rx, ry);
    const mask = gridCacheCtx.createRadialGradient(0, 0, 0.38, 0, 0, 1);
    mask.addColorStop(0, "rgba(0, 0, 0, 1)");
    mask.addColorStop(0.64, "rgba(0, 0, 0, 0.55)");
    mask.addColorStop(1, "rgba(0, 0, 0, 0)");
    gridCacheCtx.fillStyle = mask;
    gridCacheCtx.fillRect(-1.5, -1.5, 3, 3);
    gridCacheCtx.restore();
  }

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 1.75);
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = Math.max(1, Math.round(width * dpr));
    canvas.height = Math.max(1, Math.round(height * dpr));
    const nextCols = Math.ceil(width / CELL) + 1;
    const nextRows = Math.ceil(height / CELL) + 1;
    if (nextCols !== cols || nextRows !== rows) {
      cols = nextCols;
      rows = nextRows;
      totalCells = cols * rows;
      elevations = new Float32Array(totalCells);
      targetZArr = new Float32Array(totalCells);
      weightArr = new Float32Array(totalCells);
      sumXArr = new Float32Array(totalCells);
      sumYArr = new Float32Array(totalCells);
      sourceXArr = new Float32Array(totalCells);
      sourceYArr = new Float32Array(totalCells);
      activeFlag = new Uint8Array(totalCells);
      activeList = new Int32Array(totalCells);
      bucketNext = new Int32Array(totalCells);
      activeCount = 0;
      for (let j = 0; j < rows; j++) {
        for (let i = 0; i < cols; i++) {
          const idx = j * cols + i;
          sourceXArr[idx] = (i + 0.5) * CELL + 0.5;
          sourceYArr[idx] = (j + 0.5) * CELL + 0.5;
        }
      }
    }
    bakeGridCache(getPalette());
    render();
  }

  function activateCell(idx) {
    if (!activeFlag[idx]) {
      activeFlag[idx] = 1;
      activeList[activeCount++] = idx;
    }
  }

  function closestOnSegment(px, py, ax, ay, bx, by, out) {
    const abx = bx - ax;
    const aby = by - ay;
    const len2 = abx * abx + aby * aby;
    if (len2 < 0.25) {
      out[0] = bx;
      out[1] = by;
      return;
    }
    let t = ((px - ax) * abx + (py - ay) * aby) / len2;
    if (t < 0) t = 0;
    else if (t > 1) t = 1;
    out[0] = ax + abx * t;
    out[1] = ay + aby * t;
  }

  const rgbTmp = [0, 0, 0];
  const rgbAccent = [0, 0, 0];
  const rgbSide = [0, 0, 0];
  const segPt = [0, 0];

  function render() {
    if (!width || !height) return;
    const pal = getPalette();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    // 1. Single GPU blit of the cached straight grid
    ctx.drawImage(gridCache, 0, 0, width, height);

    if (activeCount === 0) return;

    // 2. O(N) 24-bucket elevation sort (lower tiers first, higher tiers overlap & cast shadows onto lower tiers)
    bucketHeads.fill(-1);
    for (let k = activeCount - 1; k >= 0; k--) {
      const idx = activeList[k];
      const z = elevations[idx];
      if (z <= 0.006) continue;
      let b = (z * (Z_BUCKETS - 1) * 0.8) | 0;
      if (b >= Z_BUCKETS) b = Z_BUCKETS - 1;
      bucketNext[idx] = bucketHeads[b];
      bucketHeads[b] = idx;
    }

    const baseSize = CELL - 3;
    const shadowPadRatio = SHADOW_PAD / SHADOW_INNER;

    // 3. Exact original beloved 3D rounded keycap visual per active cell
    for (let b = 0; b < Z_BUCKETS; b++) {
      for (let idx = bucketHeads[b]; idx !== -1; idx = bucketNext[idx]) {
        const z = elevations[idx];
        const zClamped = z > 1.22 ? 1.22 : z;
        const zNorm = zClamped > 1 ? 1 : zClamped;

        const j = (idx / cols) | 0;
        const i = idx - j * cols;
        const restCx = (i + 0.5) * CELL + 0.5;
        const restCy = (j + 0.5) * CELL + 0.5;

        const awayX = restCx - sourceXArr[idx];
        const awayY = restCy - sourceYArr[idx];
        const d2 = awayX * awayX + awayY * awayY;
        const dist = d2 > 0.25 ? Math.sqrt(d2) : 0;
        const invDist = dist > 0.5 ? 1 / dist : 0;
        const nx = awayX * invDist;
        const ny = awayY * invDist;

        const zCurve = Math.pow(zClamped, 1.12);
        const nudge = 3.6 * zNorm * (1 - 0.5 * zNorm);
        const liftY = -7.8 * zCurve;
        const size = baseSize * (1 + 0.25 * zCurve);
        const extrude = 1.0 + 4.1 * Math.pow(zClamped, 1.08);

        const rx = restCx + nx * nudge - size * 0.5;
        const ry = restCy + ny * nudge + liftY - size * 0.5;

        // Smooth vertical & radial blend within Teal / Sky Cyan / Indigo palette
        const vRatio = awayY / 170;
        if (vRatio < 0) {
          mixRgb(pal.midRgb, pal.topRgb, -vRatio * 1.25, rgbTmp);
        } else {
          mixRgb(pal.midRgb, pal.botRgb, vRatio * 1.25, rgbTmp);
        }
        const crestHighlight = zClamped > 0.85 ? (zClamped - 0.85) / 0.37 : 0;
        const proxGlow = Math.exp(-d2 / (CELL * CELL * 1.35));
        const overhead = crestHighlight * 0.7 > proxGlow ? crestHighlight * 0.7 : proxGlow;
        mixRgb(rgbTmp, pal.coreRgb, overhead * 0.78, rgbAccent);
        mixRgb(pal.sideBase, rgbAccent, pal.dark ? 0.28 : 0.22, rgbSide);

        const tierOpacity = zNorm * 1.55 > 1 ? 1 : zNorm * 1.55;

        // Step A: Full-depth soft Gaussian drop shadow + shaded 3D rounded plinth side-wall
        const shadowPad = size * shadowPadRatio * (0.65 + 0.52 * zNorm);
        ctx.globalAlpha = tierOpacity * (0.32 + 0.68 * zNorm);
        ctx.drawImage(
          shadowSprite,
          rx + nx * zNorm * 2 - shadowPad,
          ry + extrude + 1.8 + zClamped * 6.2 - shadowPad,
          size + shadowPad * 2,
          size + shadowPad * 2
        );

        ctx.globalAlpha = tierOpacity;
        ctx.fillStyle = `rgb(${rgbSide[0]}, ${rgbSide[1]}, ${rgbSide[2]})`;
        roundedRectPath(ctx, rx, ry + extrude, size, size, CORNER_R);
        ctx.fill();

        // Step B: Solid-feeling porcelain/slate rounded top-cap surface
        ctx.globalAlpha = (0.64 + 0.36 * zNorm) * tierOpacity;
        ctx.fillStyle = pal.capBase;
        roundedRectPath(ctx, rx, ry, size, size, CORNER_R);
        ctx.fill();
        ctx.globalAlpha = 1;

        // Step C: Original 3-stop directional 3D lighting facing the cursor / ripple origin
        const ux = -nx;
        const uy = dist > 0.5 ? -ny : -1;
        const proj = Math.abs(ux) + Math.abs(uy) > 0.7 ? Math.abs(ux) + Math.abs(uy) : 0.7;
        const half = size * 0.5;
        const cx = rx + half;
        const cy = ry + half;

        const aNear = Math.min(0.76, pal.peakAlpha * Math.pow(zClamped, 0.88) * (1 + 0.28 * overhead));
        const aMid = aNear * (0.34 + 0.56 * overhead);
        const aFar = aNear * (0.04 + 0.72 * overhead);
        const rgbStr = `${rgbAccent[0]}, ${rgbAccent[1]}, ${rgbAccent[2]}`;

        const grad = ctx.createLinearGradient(
          cx + ux * half * proj,
          cy + uy * half * proj,
          cx - ux * half * proj,
          cy - uy * half * proj
        );
        grad.addColorStop(0, `rgba(${rgbStr}, ${aNear.toFixed(4)})`);
        grad.addColorStop(0.54, `rgba(${rgbStr}, ${aMid.toFixed(4)})`);
        grad.addColorStop(1, `rgba(${rgbStr}, ${aFar.toFixed(4)})`);

        ctx.fillStyle = grad;
        roundedRectPath(ctx, rx, ry, size, size, CORNER_R);
        ctx.fill();

        // Step D: Crisp rounded bevel border + clean top-edge specular gleam
        const bAlpha = (0.1 + 0.3 * zNorm) * tierOpacity;
        ctx.lineWidth = 1;
        ctx.strokeStyle = `rgba(${pal.borderRgb}, ${bAlpha.toFixed(4)})`;
        roundedRectPath(ctx, rx + 0.5, ry + 0.5, size - 1, size - 1, Math.max(1, CORNER_R - 0.5));
        ctx.stroke();

        const specAlpha = pal.specularAlpha * Math.pow(zNorm, 0.85) * tierOpacity;
        if (specAlpha > 0.03) {
          ctx.beginPath();
          ctx.moveTo(rx + CORNER_R, ry + 1);
          ctx.lineTo(rx + size - CORNER_R, ry + 1);
          ctx.lineWidth = 1.15;
          ctx.strokeStyle = `rgba(${pal.specularRgb}, ${specAlpha.toFixed(4)})`;
          ctx.stroke();

          // Subtle bottom bevel crease separating the top cap from the 3D side-wall skirt
          const creaseA = pal.creaseAlpha * zNorm * tierOpacity;
          ctx.beginPath();
          ctx.moveTo(rx + CORNER_R, ry + size - 0.6);
          ctx.lineTo(rx + size - CORNER_R, ry + size - 0.6);
          ctx.lineWidth = 1;
          ctx.strokeStyle = `rgba(${pal.creaseRgb}, ${creaseA.toFixed(4)})`;
          ctx.stroke();
        }
      }
    }
    ctx.globalAlpha = 1;
  }

  function tick(now) {
    rafId = 0;
    if (!now) now = performance.now();
    const dt = lastFrameTime > 0 ? Math.min(0.05, Math.max(0.004, (now - lastFrameTime) * 0.001)) : 0.016;
    lastFrameTime = now;

    const prevSmoothX = smoothX;
    const prevSmoothY = smoothY;
    const smoothLerp = 1 - Math.exp(-dt / 0.035);
    smoothX += (mouseX - smoothX) * smoothLerp;
    smoothY += (mouseY - smoothY) * smoothLerp;

    const maxRippleR = Math.min(680, Math.hypot(width, height) * 0.65);
    const activeRipples = [];
    let hoverDip = 1;

    for (let r = ripples.length - 1; r >= 0; r--) {
      const age = (now - ripples[r].start) * 0.001;
      if (age >= RIPPLE_DURATION) {
        ripples.splice(r, 1);
      } else {
        const tau = age / RIPPLE_DURATION;
        const inv = 1 - tau;
        const progress = 1 - inv * Math.sqrt(inv);
        const waveR = progress * maxRippleR;
        const timeFade = Math.pow(inv, 0.65);
        if (age < 0.62) {
          const cdx = smoothX - ripples[r].x;
          const cdy = smoothY - ripples[r].y;
          if (cdx * cdx + cdy * cdy < HOVER_RADIUS_SQ) {
            const t = 1 - age / 0.62;
            const dip = 1 - 0.86 * t * t;
            if (dip < hoverDip) hoverDip = dip;
          }
        }
        activeRipples.push({
          x: ripples[r].x,
          y: ripples[r].y,
          waveR,
          outerBound: waveR + RIPPLE_RING_WIDTH,
          innerBound: Math.max(0, waveR - RIPPLE_RING_WIDTH - RIPPLE_TRAIL_LAG - 8),
          timeFade
        });
      }
    }

    for (let k = 0; k < activeCount; k++) {
      const idx = activeList[k];
      targetZArr[idx] = 0;
      weightArr[idx] = 0;
      sumXArr[idx] = 0;
      sumYArr[idx] = 0;
    }

    // 1. Continuous stroke-segment hover (lifts smoothly along fast glides; old cells slowly descend!)
    if (!paused && pointerActive) {
      const segAx = prevSmoothX > -1000 ? prevSmoothX : smoothX;
      const segAy = prevSmoothY > -1000 ? prevSmoothY : smoothY;
      const minX = (segAx < smoothX ? segAx : smoothX) - HOVER_RADIUS;
      const maxX = (segAx > smoothX ? segAx : smoothX) + HOVER_RADIUS;
      const minY = (segAy < smoothY ? segAy : smoothY) - HOVER_RADIUS;
      const maxY = (segAy > smoothY ? segAy : smoothY) + HOVER_RADIUS;

      const i0 = Math.max(0, Math.floor(minX / CELL));
      const i1 = Math.min(cols - 1, Math.ceil(maxX / CELL));
      const j0 = Math.max(0, Math.floor(minY / CELL));
      const j1 = Math.min(rows - 1, Math.ceil(maxY / CELL));
      const invHoverSq = 1 / HOVER_RADIUS_SQ;

      for (let j = j0; j <= j1; j++) {
        const restCy = (j + 0.5) * CELL + 0.5;
        const rowOff = j * cols;
        for (let i = i0; i <= i1; i++) {
          const restCx = (i + 0.5) * CELL + 0.5;
          closestOnSegment(restCx, restCy, segAx, segAy, smoothX, smoothY, segPt);
          const dx = restCx - segPt[0];
          const dy = restCy - segPt[1];
          const d2 = dx * dx + dy * dy;
          if (d2 >= HOVER_RADIUS_SQ) continue;
          const raw = 1 - d2 * invHoverSq;
          const hz = Math.pow(raw, 1.95) * hoverDip;
          if (hz <= 0.002) continue;
          const idx = rowOff + i;
          activateCell(idx);
          targetZArr[idx] += hz;
          sumXArr[idx] += segPt[0] * hz;
          sumYArr[idx] += segPt[1] * hz;
          weightArr[idx] += hz;
        }
      }
    }

    // 2. Click ripples: swells higher leaving click center, then tapers outward
    const invRingW = 1 / RIPPLE_RING_WIDTH;
    const subWidth = RIPPLE_RING_WIDTH * 0.78;
    const invSubW = 1 / subWidth;
    const invMaxR = 1 / maxRippleR;

    for (let r = 0; r < activeRipples.length; r++) {
      const rp = activeRipples[r];
      const ob = rp.outerBound;
      const ib = rp.innerBound;
      const ob2 = ob * ob;
      const ib2 = ib * ib;

      const i0 = Math.max(0, Math.floor((rp.x - ob) / CELL));
      const i1 = Math.min(cols - 1, Math.ceil((rp.x + ob) / CELL));
      const j0 = Math.max(0, Math.floor((rp.y - ob) / CELL));
      const j1 = Math.min(rows - 1, Math.ceil((rp.y + ob) / CELL));

      for (let j = j0; j <= j1; j++) {
        const rdy = (j + 0.5) * CELL + 0.5 - rp.y;
        const rdy2 = rdy * rdy;
        if (rdy2 >= ob2) continue;
        const rowOff = j * cols;

        for (let i = i0; i <= i1; i++) {
          const rdx = (i + 0.5) * CELL + 0.5 - rp.x;
          const d2 = rdx * rdx + rdy2;
          if (d2 >= ob2 || d2 < ib2) continue;

          const dist = Math.sqrt(d2);
          let ringShape = 0;
          const d1 = Math.abs(dist - rp.waveR);
          if (d1 < RIPPLE_RING_WIDTH) {
            const w1 = 1 - (d1 * invRingW) * (d1 * invRingW);
            ringShape += w1 * w1;
          }
          if (rp.waveR > RIPPLE_TRAIL_LAG * 0.9) {
            const dTrail = Math.abs(dist - (rp.waveR - RIPPLE_TRAIL_LAG));
            if (dTrail < subWidth) {
              const w2 = 1 - (dTrail * invSubW) * (dTrail * invSubW);
              ringShape += 0.34 * w2 * w2;
            }
          }
          if (ringShape <= 0.002) continue;

          const s = dist < RIPPLE_PEAK_DIST ? dist / RIPPLE_PEAK_DIST : 1;
          const surgeUp = 0.36 + 0.94 * Math.sin(s * (Math.PI * 0.5));
          const dn = dist * invMaxR;
          const fadeD = 1 - Math.pow(dn, 1.55);
          const decayOut = fadeD > 0 ? Math.pow(fadeD, 1.9) : 0;
          const rz = ringShape * surgeUp * decayOut * rp.timeFade;

          if (rz > 0.002) {
            const idx = rowOff + i;
            activateCell(idx);
            targetZArr[idx] += rz;
            sumXArr[idx] += rp.x * rz;
            sumYArr[idx] += rp.y * rz;
            weightArr[idx] += rz;
          }
        }
      }
    }

    // 3. Asymmetric dt-based transition: brisk smooth rise (RISE_TAU), slow lingering descent (FALL_TAU)!
    const riseAlpha = 1 - Math.exp(-dt / RISE_TAU);
    const fallAlpha = 1 - Math.exp(-dt / FALL_TAU);
    const srcAlpha = 1 - Math.exp(-dt / 0.06);

    let maxDelta = activeRipples.length > 0 || Math.max(Math.abs(mouseX - smoothX), Math.abs(mouseY - smoothY)) > 0.25 ? 1 : 0;
    let writePos = 0;

    for (let k = 0; k < activeCount; k++) {
      const idx = activeList[k];
      let tz = targetZArr[idx];
      if (tz > 1.2) tz = 1.2;

      const w = weightArr[idx];
      if (w > 0.005) {
        const invW = 1 / w;
        sourceXArr[idx] += (sumXArr[idx] * invW - sourceXArr[idx]) * srcAlpha;
        sourceYArr[idx] += (sumYArr[idx] * invW - sourceYArr[idx]) * srcAlpha;
      }

      const cur = elevations[idx];
      const diff = tz - cur;
      let next = tz;
      if (diff > 0.0015 || diff < -0.0015) {
        next = cur + diff * (diff > 0 ? riseAlpha : fallAlpha);
        const ad = diff > 0 ? diff : -diff;
        if (ad > maxDelta) maxDelta = ad;
      }
      elevations[idx] = next;

      if (next > 0.004 || tz > 0.004) {
        activeList[writePos++] = idx;
      } else {
        elevations[idx] = 0;
        activeFlag[idx] = 0;
      }
    }
    activeCount = writePos;

    render();

    if (maxDelta > 0.0015 || ripples.length > 0) {
      rafId = requestAnimationFrame(tick);
    } else {
      lastFrameTime = 0;
    }
  }

  function ensureLoop() {
    if (!rafId) {
      lastFrameTime = performance.now();
      rafId = requestAnimationFrame(tick);
    }
  }

  function onPointerMove(event) {
    const overOverlay = event.target instanceof Element && Boolean(event.target.closest("#session-sidebar, dialog[open]"));
    if (smoothX < -1000) {
      smoothX = event.clientX;
      smoothY = event.clientY;
    }
    mouseX = event.clientX;
    mouseY = event.clientY;
    pointerActive = !overOverlay;
    ensureLoop();
  }

  function onPointerDown(event) {
    if (paused || event.button !== 0) return;
    const overOverlay = event.target instanceof Element && Boolean(event.target.closest("#session-sidebar, dialog[open]"));
    if (overOverlay) return;
    if (ripples.length >= MAX_RIPPLES) {
      ripples.shift();
    }
    ripples.push({
      x: event.clientX,
      y: event.clientY,
      start: performance.now()
    });
    ensureLoop();
  }

  function onPointerLeave() {
    pointerActive = false;
    ensureLoop();
  }

  window.addEventListener("pointermove", onPointerMove, { passive: true });
  window.addEventListener("pointerdown", onPointerDown, { passive: true });
  document.addEventListener("mouseleave", onPointerLeave, { passive: true });
  window.addEventListener("blur", onPointerLeave, { passive: true });
  window.addEventListener("resize", resize, { passive: true });

  if (typeof MutationObserver !== "undefined") {
    new MutationObserver(() => render()).observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"]
    });
  }

  resize();
})();
