// Shared code artifacts (server: artifacts.py). Messages carry [artifact:<id>] tokens;
// renderMarkdown turns them into cards, the cards open a side panel with versions.
const Artifacts = (() => {
    const TOKEN = /\[artifact:([A-Za-z0-9_-]{6,24})\]/g;
    const PREVIEW_LINES = 5;
    const cache = new Map();   // "id" (latest) or "id@n" -> Promise of {meta + content}
    let panel = null;
    let current = null;        // {id, version, data}
    let opener = null;         // element focused before the panel opened

    const icon = (paths) => `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
    const ICONS = {
        file: icon('<path d="M9 1.5H4.5a1 1 0 0 0-1 1v11a1 1 0 0 0 1 1h7a1 1 0 0 0 1-1V5Z"/><path d="M9 1.5V5h3.5"/><path d="m6.5 8.5-1.5 1.5 1.5 1.5M9.5 8.5l1.5 1.5-1.5 1.5"/>'),
        download: icon('<path d="M8 2v8.5M4.5 7 8 10.5 11.5 7"/><path d="M2.5 11v2a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1v-2"/>'),
        copy: icon('<rect x="5.5" y="5.5" width="8" height="8" rx="1.2"/><path d="M3.5 10.5h-.5a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1h6.5a1 1 0 0 1 1 1v.5"/>'),
        check: icon('<path d="m3 8.5 3 3 7-7"/>'),
        close: icon('<path d="m4 4 8 8M12 4l-8 8"/>'),
    };
    // Fence tags that highlight.js knows by another name
    const HLJS_ALIAS = { batch: 'dos', bat: 'dos', cmd: 'dos', sh: 'bash', shell: 'bash', ps1: 'powershell', pwsh: 'powershell' };

    function load(id, version) {
        const key = version ? `${id}@${version}` : id;
        if (!cache.has(key)) {
            const p = fetch(`/api/artifacts/${encodeURIComponent(id)}` + (version ? `?version=${version}` : ''),
                { headers: { 'X-Session-Token': window.SESSION_TOKEN } })
                .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)));
            p.catch(() => cache.delete(key));
            cache.set(key, p);
        }
        return cache.get(key);
    }

    function highlighted(code, lang) {
        const name = HLJS_ALIAS[lang] || lang;
        if (window.hljs && name && hljs.getLanguage(name)) {
            try { return hljs.highlight(code, { language: name, ignoreIllegals: true }).value; } catch (e) { /* plain */ }
        }
        return escapeHtml(code);
    }

    const metaLine = (a) => [a.language || 'text', `${a.lines} line${a.lines === 1 ? '' : 's'}`, `v${a.version}`].join(' · ');

    // --- message cards ---

    function tokensToHtml(text) {
        // Blank lines around the card so markdown treats it as its own block
        return text.replace(TOKEN, (_, id) => `\n\n<div class="artifact-card" data-artifact-id="${id}"></div>\n\n`);
    }

    function fillCard(card, a) {
        card.classList.remove('artifact-card-missing');
        card.setAttribute('role', 'button');
        card.tabIndex = 0;
        card.setAttribute('aria-label', `Open artifact ${a.title}`);
        const preview = a.content.split('\n').slice(0, PREVIEW_LINES).join('\n');
        card.innerHTML = `
            <div class="artifact-card-head">
                <span class="artifact-card-icon">${ICONS.file}</span>
                <span class="artifact-card-title"></span>
                <span class="artifact-card-meta"></span>
            </div>
            <div class="artifact-card-preview" aria-hidden="true"><code class="hljs">${highlighted(preview, a.language)}</code></div>
            <div class="artifact-card-actions">
                <button type="button" class="artifact-btn" data-act="open">Open</button>
                <button type="button" class="artifact-btn" data-act="download" aria-label="Download ${escapeHtml(a.filename)}">${ICONS.download}<span>Download</span></button>
            </div>`;
        card.querySelector('.artifact-card-title').textContent = a.title;
        card.querySelector('.artifact-card-meta').textContent = metaLine(a);
    }

    function hydrate(root) {
        for (const card of root.querySelectorAll('.artifact-card:not([data-ready])')) {
            card.dataset.ready = '1';
            load(card.dataset.artifactId).then(a => fillCard(card, a)).catch(() => {
                card.classList.add('artifact-card-missing');
                card.textContent = 'Artifact unavailable';
            });
        }
    }

    function download(a) {
        const url = URL.createObjectURL(new Blob([a.content], { type: 'text/plain;charset=utf-8' }));
        const link = document.createElement('a');
        link.href = url;
        link.download = a.filename || 'artifact.txt';
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 60000);
    }

    async function copyText(text) {
        if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
        // Plain-http LAN pages have no async clipboard
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand('copy');
        ta.remove();
        if (!ok) throw new Error('copy failed');
    }

    // --- side panel ---

    function buildPanel() {
        panel = document.createElement('aside');
        panel.className = 'artifact-panel';
        panel.hidden = true;
        panel.setAttribute('aria-labelledby', 'artifact-panel-title');
        panel.innerHTML = `
            <div class="artifact-panel-head">
                <div class="artifact-panel-titles">
                    <h2 id="artifact-panel-title" class="artifact-panel-title"></h2>
                    <div class="artifact-panel-sub"></div>
                </div>
                <select class="artifact-version" aria-label="Version"></select>
                <button type="button" class="artifact-btn" data-act="download" aria-label="Download">${ICONS.download}<span>Download</span></button>
                <button type="button" class="artifact-icon-btn" data-act="copy" aria-label="Copy to clipboard" title="Copy to clipboard">${ICONS.copy}</button>
                <button type="button" class="artifact-icon-btn" data-act="close" aria-label="Close" title="Close (Esc)">${ICONS.close}</button>
            </div>
            <div class="artifact-panel-body">
                <div class="artifact-code"><div class="artifact-gutter" aria-hidden="true"></div><pre><code class="hljs"></code></pre></div>
            </div>`;
        document.body.appendChild(panel);
        panel.querySelector('.artifact-version').addEventListener('change', (e) => show(current.id, Number(e.target.value)));
        panel.addEventListener('click', async (e) => {
            const btn = e.target.closest('button[data-act]');
            if (!btn || !current) return;
            if (btn.dataset.act === 'close') return close();
            if (btn.dataset.act === 'download') return download(current.data);
            if (btn.dataset.act === 'copy') {
                try {
                    await copyText(current.data.content);
                    btn.innerHTML = ICONS.check;
                    btn.setAttribute('aria-label', 'Copied');
                } catch (err) {
                    btn.title = 'Copy failed';
                }
                setTimeout(() => { btn.innerHTML = ICONS.copy; btn.setAttribute('aria-label', 'Copy to clipboard'); }, 1500);
            }
        });
    }

    function render(a) {
        panel.querySelector('.artifact-panel-title').textContent = a.title;
        panel.querySelector('.artifact-panel-sub').textContent =
            [a.language || 'text', `${a.lines} lines`, a.filename !== a.title && a.filename, `#${a.channel}`].filter(Boolean).join(' · ');
        const sel = panel.querySelector('.artifact-version');
        sel.innerHTML = a.versions.slice().reverse().map(v =>
            `<option value="${v.n}">v${v.n} · ${escapeHtml(v.author)}${v.n === a.versions.length ? ' (latest)' : ''}</option>`).join('');
        sel.value = String(a.version);
        sel.hidden = a.versions.length < 2;
        const lines = a.content.split('\n').length;
        panel.querySelector('.artifact-gutter').textContent = Array.from({ length: lines }, (_, i) => i + 1).join('\n');
        panel.querySelector('.artifact-code code').innerHTML = highlighted(a.content, a.language);
    }

    async function show(id, version) {
        try {
            const a = await load(id, version);
            current = { id, version: a.version, data: a };
            render(a);
        } catch (e) {
            panel.querySelector('.artifact-panel-title').textContent = 'Artifact unavailable';
        }
    }

    function open(id) {
        if (!panel) buildPanel();
        if (panel.hidden) opener = document.activeElement;
        const header = document.querySelector('header');
        panel.style.top = `${header ? Math.max(0, header.getBoundingClientRect().bottom) : 0}px`;
        panel.hidden = false;
        requestAnimationFrame(() => panel.classList.add('open'));
        panel.querySelector('.artifact-panel-body').scrollTop = 0;
        show(id).then(() => panel.querySelector('[data-act="close"]').focus({ preventScroll: true }));
    }

    function close() {
        if (!panel || panel.hidden) return;
        panel.classList.remove('open');
        panel.hidden = true;
        current = null;
        if (opener && document.contains(opener)) opener.focus({ preventScroll: true });
    }

    // Card clicks (delegated), click outside the panel, Esc
    document.addEventListener('click', (e) => {
        const card = e.target.closest('.artifact-card[role="button"]');
        if (card) {
            e.stopPropagation();
            const act = e.target.closest('button[data-act]')?.dataset.act;
            if (act === 'download') load(card.dataset.artifactId).then(download);
            else open(card.dataset.artifactId);
            return;
        }
        if (panel && !panel.hidden && !panel.contains(e.target)) close();
    }, true);
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && panel && !panel.hidden) { e.stopPropagation(); close(); return; }
        const card = e.target.closest?.('.artifact-card[role="button"]');
        if (card && e.target === card && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); open(card.dataset.artifactId); }
    }, true);

    // A new version (system message from artifacts.update): refresh cards and the open panel
    Hub.on('message', (event) => {
        const a = event.data?.metadata?.artifact;
        if (!a) return;
        cache.delete(a.id);
        for (const card of document.querySelectorAll(`.artifact-card[data-artifact-id="${a.id}"]`)) {
            delete card.dataset.ready;
        }
        hydrate(document);
        if (current && current.id === a.id) show(a.id);
    });

    return { tokensToHtml, hydrate, open, close };
})();
window.Artifacts = Artifacts;
