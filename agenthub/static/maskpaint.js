// Paint an inpaint mask on a lightbox image. "Use mask" attaches masked-<name>.png
// (painted area fully transparent) to the composer through the normal upload flow.
(function () {
    const icon = (body) => `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
    const ICONS = {
        paint: icon('<path d="M18.4 2.6a2.1 2.1 0 0 1 3 3L12 15l-4-4z"/><path d="M8 11c-2 0-4 1.5-4 4 0 1.5-1 3-2 3 1.5 2 3.5 3 6 3a5 5 0 0 0 5-5c0-1-.4-2-1-2.6"/>'),
        brush: icon('<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3" fill="currentColor"/>'),
        eraser: icon('<path d="m7 21-4.3-4.3a1 1 0 0 1 0-1.4l10-10a1 1 0 0 1 1.4 0l5.6 5.6a1 1 0 0 1 0 1.4L11 21"/><path d="M22 21H7"/><path d="m5 11 9 9"/>'),
        undo: icon('<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>'),
        clear: icon('<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'),
        cancel: icon('<path d="M18 6 6 18"/><path d="m6 6 12 12"/>'),
        use: icon('<path d="M20 6 9 17l-5-5"/>'),
    };
    const MIN = 4, MAX = 200;
    let modal, img, canvas, ctx, ring, bar, status, sizeInput;
    let active = false, erasing = false, size = 32, undo = [], last = null, ro = null;

    const btn = (cls, ico, label, onClick) => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = `maskpaint-btn ${cls}`;
        b.innerHTML = `${ico}<span>${label}</span>`;
        b.setAttribute('aria-label', label);
        b.title = label;
        b.addEventListener('click', (e) => { e.stopPropagation(); onClick(); });
        return b;
    };

    window.maskPaintInit = function (m) {
        modal = m;
        img = modal.querySelector('img');
        modal.appendChild(btn('maskpaint-open', ICONS.paint, 'Paint mask', start));

        canvas = document.createElement('canvas');
        canvas.className = 'maskpaint-canvas';
        canvas.setAttribute('aria-label', 'Mask painting area');
        ring = document.createElement('div');
        ring.className = 'maskpaint-ring';
        bar = document.createElement('div');
        bar.className = 'maskpaint-bar';
        bar.setAttribute('role', 'toolbar');
        bar.setAttribute('aria-label', 'Mask painting');
        bar.addEventListener('click', (e) => e.stopPropagation());
        const brushBtn = btn('maskpaint-tool', ICONS.brush, 'Brush', () => setTool(false));
        const eraserBtn = btn('maskpaint-tool', ICONS.eraser, 'Eraser', () => setTool(true));
        const sizeLabel = document.createElement('label');
        sizeLabel.className = 'maskpaint-size';
        sizeLabel.innerHTML = `<span>Size</span><input type="range" min="${MIN}" max="${MAX}" aria-label="Brush size">`;
        sizeInput = sizeLabel.querySelector('input');
        sizeInput.addEventListener('input', () => setSize(+sizeInput.value));
        status = document.createElement('span');
        status.className = 'maskpaint-status';
        status.setAttribute('aria-live', 'polite');
        bar.append(brushBtn, eraserBtn, sizeLabel,
            btn('', ICONS.undo, 'Undo', doUndo), btn('', ICONS.clear, 'Clear', clear),
            btn('', ICONS.cancel, 'Cancel', stop), btn('maskpaint-use', ICONS.use, 'Use mask', useMask), status);
        modal.append(canvas, ring, bar);

        canvas.addEventListener('pointerdown', down);
        canvas.addEventListener('pointermove', move);
        canvas.addEventListener('pointerup', up);
        canvas.addEventListener('pointercancel', up);
        canvas.addEventListener('pointerleave', () => { ring.style.display = 'none'; });
        // While painting, clicks (backdrop, end of a stroke) must not close the lightbox and lose the mask.
        modal.addEventListener('click', (e) => { if (active && !bar.contains(e.target)) e.stopImmediatePropagation(); }, true);
        window.addEventListener('keydown', key, true);
    };

    function setTool(erase) {
        erasing = erase;
        bar.querySelectorAll('.maskpaint-tool').forEach((b, i) => b.setAttribute('aria-pressed', String(i === (erase ? 1 : 0))));
    }

    function setSize(v) {
        size = Math.max(MIN, Math.min(MAX, Math.round(v)));
        sizeInput.value = size;
        ring.style.width = ring.style.height = `${size}px`;
    }

    function place() {
        const r = img.getBoundingClientRect();
        Object.assign(canvas.style, { left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px` });
    }

    async function start() {
        if (!img.complete) await img.decode().catch(() => {});
        if (!img.naturalWidth) return;
        active = true;
        modal.classList.add('maskpainting');
        canvas.width = img.naturalWidth;
        canvas.height = img.naturalHeight;
        ctx = canvas.getContext('2d');
        undo = [];
        status.textContent = '';
        setTool(false);
        setSize(size);
        ro = new ResizeObserver(place);
        ro.observe(img);
        window.addEventListener('resize', place);
        place();
        canvas.focus?.();
    }

    function stop() {
        if (!active) return;
        active = false;
        modal.classList.remove('maskpainting');
        ro?.disconnect();
        window.removeEventListener('resize', place);
        ring.style.display = 'none';
        undo = [];
        if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);
    }

    function snapshot() {
        // Cap history memory: full-resolution ImageData per step (~200 MB total).
        const max = Math.max(1, Math.min(10, Math.floor(2e8 / (canvas.width * canvas.height * 4))));
        undo.push(ctx.getImageData(0, 0, canvas.width, canvas.height));
        if (undo.length > max) undo.shift();
    }

    function doUndo() { if (undo.length) ctx.putImageData(undo.pop(), 0, 0); }
    function clear() { snapshot(); ctx.clearRect(0, 0, canvas.width, canvas.height); }

    function point(e) {
        const r = canvas.getBoundingClientRect();
        const k = canvas.width / r.width;
        return { x: (e.clientX - r.left) * k, y: (e.clientY - r.top) * k, k };
    }

    function stroke(a, b) {
        ctx.globalCompositeOperation = erasing ? 'destination-out' : 'source-over';
        ctx.strokeStyle = ctx.fillStyle = getComputedStyle(modal).getPropertyValue('--accent').trim();
        ctx.lineWidth = size * b.k;
        ctx.lineCap = ctx.lineJoin = 'round';
        ctx.beginPath();
        if (a) { ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke(); } else { ctx.arc(b.x, b.y, ctx.lineWidth / 2, 0, Math.PI * 2); ctx.fill(); }
    }

    function showRing(e) {
        ring.style.display = 'block';
        ring.style.left = `${e.clientX}px`;
        ring.style.top = `${e.clientY}px`;
    }

    function down(e) {
        if (e.button !== 0) return;
        e.preventDefault();
        canvas.setPointerCapture(e.pointerId);
        snapshot();
        last = point(e);
        stroke(null, last);
        status.textContent = '';
    }

    function move(e) {
        showRing(e);
        if (!last) return;
        e.preventDefault();
        const p = point(e);
        stroke(last, p);
        last = p;
    }

    function up() { last = null; }

    function key(e) {
        if (!active) return;
        const k = e.key.toLowerCase();
        if (e.key === 'Escape') stop();
        else if (e.key === '[') setSize(size - 4);
        else if (e.key === ']') setSize(size + 4);
        else if (k === 'z' && (e.ctrlKey || e.metaKey)) doUndo();
        else if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
        // Paint mode owns these keys: Esc must not close the lightbox, arrows must not change image.
        e.preventDefault();
        e.stopImmediatePropagation();
    }

    function originalName() {
        const src = img.src;
        const shown = [...document.querySelectorAll('img[alt]')].find((i) => i !== img && i.src === src);
        const name = (shown && shown.alt) || decodeURIComponent(src.split('/').pop().split('?')[0]) || 'image';
        return name.replace(/\.[^.]+$/, '') || 'image';
    }

    const CRC = Array.from({ length: 256 }, (_, n) => {
        let c = n;
        for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
        return c >>> 0;
    });

    function chunk(type, data) {
        const out = new Uint8Array(12 + data.length), dv = new DataView(out.buffer);
        dv.setUint32(0, data.length);
        for (let i = 0; i < 4; i++) out[4 + i] = type.charCodeAt(i);
        out.set(data, 8);
        let c = 0xffffffff;
        for (let i = 4; i < 8 + data.length; i++) c = CRC[(c ^ out[i]) & 255] ^ (c >>> 8);
        dv.setUint32(8 + data.length, (c ^ 0xffffffff) >>> 0);
        return out;
    }

    // Straight (non-premultiplied) RGBA -> PNG blob.
    async function encodePng(w, h, rgba) {
        const raw = new Uint8Array(h * (w * 4 + 1));
        for (let y = 0; y < h; y++) raw.set(rgba.subarray(y * w * 4, (y + 1) * w * 4), y * (w * 4 + 1) + 1);
        const z = new Uint8Array(await new Response(new Blob([raw]).stream()
            .pipeThrough(new CompressionStream('deflate'))).arrayBuffer());
        const ihdr = new Uint8Array(13), dv = new DataView(ihdr.buffer);
        dv.setUint32(0, w); dv.setUint32(4, h);
        ihdr.set([8, 6, 0, 0, 0], 8);
        return new Blob([new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', ihdr),
            chunk('IDAT', z), chunk('IEND', new Uint8Array(0))], { type: 'image/png' });
    }

    async function useMask() {
        const w = canvas.width, h = canvas.height;
        const mask = ctx.getImageData(0, 0, w, h).data;
        const out = document.createElement('canvas');
        out.width = w;
        out.height = h;
        const octx = out.getContext('2d');
        octx.drawImage(img, 0, 0, w, h);
        let px;
        try {
            px = octx.getImageData(0, 0, w, h);
        } catch {
            status.textContent = 'This image cannot be masked here.';
            return;
        }
        let painted = 0;
        // Threshold the anti-aliased stroke edge so the mask stays binary.
        for (let i = 3; i < mask.length; i += 4) {
            const hit = mask[i] >= 128;
            px.data[i] = hit ? 0 : 255;
            painted += hit;
        }
        if (!painted) { status.textContent = 'Paint the area to change first.'; return; }
        const use = bar.querySelector('.maskpaint-use');
        use.disabled = true;
        status.textContent = 'Preparing image…';
        // Own PNG encoder: canvas.toBlob premultiplies alpha, which blacks out the pixels under the
        // mask; partial-denoise inpaints need the original colours there.
        const blob = await encodePng(w, h, px.data).catch(() => null);
        use.disabled = false;
        if (!blob) { status.textContent = 'Could not create the mask image.'; return; }
        const file = new File([blob], `masked-${originalName()}.png`, { type: 'image/png' });
        stop();
        closeImageModal();
        const input = document.getElementById('input');
        if (input) input.focus();
        await uploadFiles([file], false);
    }
})();
