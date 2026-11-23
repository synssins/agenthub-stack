// Settings window (gear icon): categories that expand on the left, one page on the right.
// Pages are built from UIKit parts (ui.js). The original settings controls (#settings-bar
// in index.html) keep their handlers from chat.js: pages borrow each control and give it
// back when the page changes. The other pages talk to admin_api.py / agent_runner.py.
// Secrets are shown once. Everything is built with createElement / textContent.

(function () {
    const { el, api } = window.SettingsUI;
    const { group, field, empty, actions } = window.UIKit;

    // keys: extra words the search box matches besides the page and category names
    const CATS = [
        { id: 'appearance', label: 'Appearance', pages: [
            { id: 'appearance', label: 'Theme and layout', keys: 'theme themes color colour dark light font contrast sidebar channel list look swatch', render: renderAppearance },
            { id: 'notifications', label: 'Notifications', keys: 'sound sounds alert chime mute', render: renderNotifications },
        ] },
        { id: 'room', label: 'Room', pages: [
            { id: 'chat', label: 'General', keys: 'name username server label title messages loaded history limit chat', render: renderGeneral },
            { id: 'turns', label: 'Turn-taking', keys: 'loop guard hops untagged picking model picker mention names rule refresh routing', render: renderTurns },
            { id: 'rulebooks', label: 'Rulebooks', keys: 'rules guide review fine-grained access', render: renderRulebooks },
            { id: 'history', label: 'History', keys: 'export import backup zip archive', render: renderHistory },
        ] },
        { id: 'agents', label: 'Agents', pages: [
            { id: 'agents', label: 'Room agents', keys: 'agent create edit model permissions claude api bot', render: renderAgents },
            { id: 'emotion', label: 'Emotion engine', keys: 'emotion feelings mood personality hexaco model morning time zone timezone tone', render: renderEmotion },
            { id: 'games', label: 'Games', keys: 'game games trivia twenty questions word chain story tune host play score voice', render: renderGames },
            { id: 'metrics', label: 'Usage', keys: 'metrics tokens cost speed cache calls statistics', render: renderMetrics },
        ] },
        { id: 'providers', label: 'Providers', pages: [
            { id: 'llms', label: 'LLMs', keys: 'llm server model openrouter openai anthropic gemini base url key provider', render: renderLlms },
            { id: 'mcp', label: 'MCP servers', keys: 'tools tool server oauth stdio http sign in', render: renderMcp },
            { id: 'secrets', label: 'Secrets', keys: 'token tokens environment variable env github key password', render: renderSecrets },
            { id: 'mqtt', label: 'MQTT', keys: 'mqtt broker home assistant state emotion face display publish topic', render: renderMqtt },
        ] },
        { id: 'voice', label: 'Voice', pages: [
            { id: 'voice', label: 'Conversation', keys: 'passphrase pin lock door garage barge-in interrupt named wake word voice check model mic microphone typing hands-free', render: renderVoice },
            { id: 'voice-providers', label: 'Speech providers', keys: 'tts text to speech provider engine parameters voices saved audio cache streaming key', render: renderVoiceProviders },
            { id: 'voice-stt', label: 'Speech to text', keys: 'stt whisper wyoming language transcription recognition', render: renderVoiceStt },
            { id: 'voice-devices', label: 'Devices', keys: 'device always listening speaker echo dot dots techo5 adopt key channel', render: renderVoiceDevices },
            { id: 'voice-who', label: 'Voice recognition', keys: 'speaker recognition recognize identify who is speaking voice enroll enrol enrollment person people', render: renderWho },
            { id: 'voice-clone', label: 'Cloned voices', keys: 'clone cloning sample upload tts', render: renderVoiceClone },
        ] },
        { id: 'senses', label: 'Senses', pages: [
            { id: 'senses', label: 'Overview', keys: 'senses house home sense activity mute status said felt log', render: renderSenses },
            { id: 'senses-ha', label: 'Home Assistant', keys: 'home assistant ha door window lock leak watched things reminders phone notify token people', render: renderSensesHa },
            { id: 'senses-cameras', label: 'Cameras', keys: 'camera cameras unifi protect doorbell ring package person vehicle alarm trigger api key vision look', render: renderSensesCameras },
            { id: 'senses-speakers', label: 'Speakers and display', keys: 'speaker speakers room rooms announce satellite desk display knob face mqtt events topics', render: renderSensesOut },
            { id: 'senses-reflexes', label: 'Reflexes', keys: 'reflex reflexes question yes no ask lights instant immediate', render: renderSensesReflexes },
        ] },
        { id: 'access', label: 'Keys and access', pages: [
            { id: 'apikeys', label: 'API keys', keys: 'key keys openai compatible client base url v1 opencode', render: renderApiKeys },
            { id: 'connections', label: 'Agent invites', keys: 'invite invites registration join connections remote agent access code revoke', render: renderConnections },
            { id: 'irc', label: 'IRC', keys: 'irc client gateway password', render: renderIrc },
            { id: 'discord', label: 'Discord bots', keys: 'discord bot server guild invite token persona start stop restart voice', render: renderDiscord },
        ] },
    ];
    const PAGES = {};
    for (const c of CATS) for (const p of c.pages) PAGES[p.id] = { ...p, cat: c };
    // Other names a page may be asked for by
    const ALIAS = { theme: 'appearance', general: 'chat', 'turn-taking': 'turns', usage: 'metrics', invites: 'connections',
        'api-keys': 'apikeys', sounds: 'notifications', providers: 'llms', stt: 'voice-stt', devices: 'voice-devices',
        tts: 'voice-providers', 'voice-conversation': 'voice' };
    const pageId = (id) => (PAGES[id] ? id : PAGES[ALIAS[id]] ? ALIAS[id] : 'appearance');

    const CATS_KEY = 'agenthub-settings-collapsed';
    let collapsed = new Set();
    try { collapsed = new Set(JSON.parse(localStorage.getItem(CATS_KEY) || '[]')); } catch (_) { /* private mode */ }
    let current = 'appearance';
    let query = '';
    const phone = () => window.matchMedia('(max-width: 720px)').matches;

    function icon(d) {
        const NS = 'http://www.w3.org/2000/svg';
        const s = document.createElementNS(NS, 'svg');
        for (const [k, v] of Object.entries({ viewBox: '0 0 16 16', width: 16, height: 16, fill: 'none', stroke: 'currentColor',
            'stroke-width': 1.5, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' })) s.setAttribute(k, v);
        const p = document.createElementNS(NS, 'path');
        p.setAttribute('d', d);
        s.append(p);
        return s;
    }

    // ------------------------------------------------------------ Window and navigation
    function dialog() {
        let d = document.getElementById('settings-dialog');
        if (d) return d;
        d = el('dialog', { id: 'settings-dialog', class: 'settings-dialog', 'aria-label': 'Settings', tabindex: '-1' });
        const search = el('input', { type: 'search', class: 'settings-search-input', placeholder: 'Search settings',
            'aria-label': 'Search settings', autocomplete: 'off', spellcheck: 'false' });
        search.addEventListener('input', () => { query = search.value.trim().toLowerCase(); syncNav(); });
        search.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && search.value) { e.preventDefault(); search.value = ''; query = ''; syncNav(); }
            if (e.key === 'Enter') { const first = d.querySelector('.settings-link:not([hidden])'); if (first) first.click(); }
        });
        const list = el('div', { class: 'settings-nav-list' });
        for (const c of CATS) {
            const pages = el('div', { class: 'settings-cat-pages', id: 'settings-cat-' + c.id });
            for (const p of c.pages) pages.append(el('button', { type: 'button', class: 'settings-link', 'data-section': p.id,
                text: p.label, onclick: () => show(p.id) }));
            list.append(el('div', { class: 'settings-cat', 'data-cat': c.id },
                el('button', { type: 'button', class: 'settings-cat-head', 'aria-controls': pages.id, onclick: () => {
                    if (collapsed.has(c.id)) collapsed.delete(c.id); else collapsed.add(c.id);
                    try { localStorage.setItem(CATS_KEY, JSON.stringify([...collapsed])); } catch (_) { /* private mode */ }
                    syncNav();
                } }, icon('M6 4l4 4-4 4'), el('span', { text: c.label })),
                pages));
        }
        const none = el('p', { class: 'settings-nav-none', hidden: true, text: 'No settings match.' });
        const nav = el('nav', { class: 'settings-side', 'aria-label': 'Settings pages' },
            el('div', { class: 'settings-side-head' },
                el('h1', { class: 'settings-side-title', text: 'Settings' }),
                el('label', { class: 'settings-search' }, icon('M7 12A5 5 0 1 0 7 2a5 5 0 0 0 0 10zM11 11l3 3'), search)),
            list, none);
        // Arrow keys walk the search box, categories and pages
        nav.addEventListener('keydown', (e) => {
            if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
            const stops = [search, ...list.querySelectorAll('button')].filter(b => b.offsetParent !== null);
            const next = stops[stops.indexOf(document.activeElement) + (e.key === 'ArrowDown' ? 1 : -1)];
            if (next) { e.preventDefault(); next.focus(); }
        });
        const back = el('button', { type: 'button', class: 'ui-btn ghost sm settings-back',
            onclick: () => { d.classList.remove('on-page'); d.focus(); } }, icon('M10 3L5 8l5 5'), el('span', { text: 'Settings' }));
        const close = el('button', { type: 'button', class: 'settings-close', 'aria-label': 'Close settings', title: 'Close',
            onclick: () => d.close() }, icon('M4 4l8 8M12 4l-8 8'));
        d.append(nav, el('section', { class: 'settings-panel', id: 'settings-panel' }, back, el('div', { class: 'settings-page' })), close);
        d.addEventListener('click', (e) => { if (e.target === d) d.close(); });
        d.addEventListener('close', park);
        document.body.append(d);
        return d;
    }

    // Reflects the open page, the search text and the remembered categories in the navigation
    function syncNav() {
        const d = dialog();
        const words = query.split(/\s+/).filter(Boolean);
        let shown = 0;
        for (const c of CATS) {
            const box = d.querySelector(`.settings-cat[data-cat="${c.id}"]`);
            let hits = 0;
            for (const p of c.pages) {
                const b = box.querySelector(`[data-section="${p.id}"]`);
                const text = `${p.label} ${c.label} ${p.keys}`.toLowerCase();
                const hit = words.every(w => text.includes(w));
                b.hidden = !hit;
                b.setAttribute('aria-current', p.id === current ? 'page' : 'false');
                if (hit) hits++;
            }
            shown += hits;
            box.hidden = !hits;
            // the category of the open page is always expanded; a search expands every match
            const open = words.length ? true : (PAGES[current].cat === c || !collapsed.has(c.id));
            box.querySelector('.settings-cat-head').setAttribute('aria-expanded', String(open));
            box.querySelector('.settings-cat-pages').hidden = !open;
        }
        d.querySelector('.settings-nav-none').hidden = shown > 0;
        const here = d.querySelector('.settings-link[aria-current="page"]');
        if (here && !here.hidden) here.scrollIntoView({ block: 'nearest' });
    }

    // Controls borrowed from #settings-bar go home before a page is dropped
    const homes = new Map();
    function borrow(node) {
        if (node && !homes.has(node)) homes.set(node, node.parentElement);
        return node;
    }
    function park() { for (const [node, home] of homes) home.append(node); }

    // A fresh page element per render, so a slow load never lands on another page
    function mount(render) {
        const panel = dialog().querySelector('#settings-panel');
        park();
        const body = el('div', { class: 'settings-page' });
        panel.querySelector('.settings-page').replaceWith(body);
        panel.scrollTop = 0;
        let r;
        try { r = render(body); } catch (err) { r = Promise.reject(err); }
        Promise.resolve(r).catch(err => fail(body, err, () => mount(render)));
    }

    function show(id) {
        current = pageId(id);
        dialog().classList.add('on-page');
        syncNav();
        mount(PAGES[current].render);
    }
    const refresh = (id) => { if (current === id) show(id); };   // after a save: only if still on that page

    function open(section) {
        const d = dialog();
        if (!d.open) d.showModal();
        show(typeof section === 'string' ? section : current);
        // phones: the page list is the first level unless a page was asked for
        if (typeof section !== 'string' && phone()) { d.classList.remove('on-page'); d.focus(); }
    }
    function close() { const d = document.getElementById('settings-dialog'); if (d && d.open) d.close(); }

    // ------------------------------------------------------------ Page parts
    function heading(body, title, purpose) {
        body.append(el('div', { class: 'settings-head' }, el('h2', { text: title }),
            purpose ? el('p', { text: purpose }) : null));
    }

    async function load(body, promise) {
        const n = el('p', { class: 'settings-loading', role: 'status', text: 'Loading…' });
        body.append(n);
        try { return await promise; } finally { n.remove(); }
    }

    function fail(body, err, retry) {
        body.append(el('div', { class: 'settings-error', role: 'alert' },
            el('strong', { text: 'This page could not be loaded' }),
            el('span', { text: `${(err && err.message) || err}. Check that the server is running, then try again.` }),
            el('button', { type: 'button', class: 'ui-btn sm', text: 'Try again', onclick: retry })));
    }

    // Runs a click or change handler; a failed request is reported instead of lost
    const guard = (fn) => async (...a) => { try { await fn(...a); } catch (err) { alert(err.message); } };

    const card = (...children) => el('div', { class: 'settings-card' }, ...children);
    const note = (text) => el('p', { class: 'settings-note', text });
    const value = (text) => el('span', { class: 'settings-value', text });
    const btn = (text, onclick, cls = '') => el('button', { type: 'button', class: ('ui-btn ' + cls).trim(), text, onclick });

    function secretBox(label, secret, hint) {
        const input = el('input', { readonly: true, value: secret, class: 'settings-copy-input', 'aria-label': label });
        const copy = btn('Copy', async () => {
            try { await navigator.clipboard.writeText(secret); copy.textContent = 'Copied'; }
            catch { input.select(); }
        });
        return el('div', { class: 'settings-copy' },
            el('strong', { text: label }), el('div', { class: 'settings-copy-row' }, input, copy),
            el('small', { text: hint || 'Shown once. Copy it now; it cannot be shown again.' }));
    }

    function table(headers, rows) {
        const t = el('table', { class: 'ui-table' });
        const tr = el('tr');
        headers.forEach(h => tr.append(el('th', { text: h })));
        t.append(el('thead', {}, tr));
        const tbody = el('tbody');
        rows.forEach(r => {
            const row = el('tr');
            r.forEach(c => row.append(el('td', {}, typeof c === 'string' ? document.createTextNode(c) : c)));
            tbody.append(row);
        });
        t.append(tbody);
        return el('div', { class: 'ui-table-wrap' }, t);
    }

    // Tick boxes for a list of names. -> {node, values()}
    function checks(names, chosen, prefix) {
        const picked = new Set(chosen || []);
        const boxes = names.map(n => el('input', { type: 'checkbox', value: n, checked: picked.has(n) }));
        return { node: el('div', { class: 'settings-checks' },
                     ...boxes.map(cb => el('label', { class: 'settings-check' }, cb, el('span', { text: prefix + cb.value })))),
                 values: () => boxes.filter(cb => cb.checked).map(cb => cb.value) };
    }

    // A field row around a control that lives in #settings-bar
    function barField(id, label, hint) {
        const c = borrow(document.getElementById(id));
        return c ? field(label, c, { hint }) : null;
    }

    // ------------------------------------------------------------ Appearance, Room (settings-bar controls)
    function renderAppearance(body) {
        heading(body, 'Theme and layout', 'Colours, type and layout of this room. Changes save at once.');
        const sel = document.getElementById('setting-theme');   // stays in the bar; chat.js saves on its change event
        const buttons = [];
        const swatch = (t) => {
            const b = el('button', { type: 'button', class: 'settings-swatch', 'aria-pressed': String(sel.value === t.id),
                onclick: () => {
                    sel.value = t.id;
                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                    buttons.forEach(x => x.setAttribute('aria-pressed', String(x === b)));
                } },
                // the mock takes its colours from its own data-theme
                el('span', { class: 'settings-swatch-mock', 'data-theme': t.id, 'aria-hidden': 'true' },
                    el('span', { class: 'sw-side' }),
                    el('span', { class: 'sw-main' }, el('span', { class: 'sw-line' }), el('span', { class: 'sw-line dim' }),
                        el('span', { class: 'sw-msg' }, el('span', { class: 'sw-accent' })))),
                el('span', { class: 'settings-swatch-name', text: t.label }));
            buttons.push(b);
            return b;
        };
        const set = (title, list) => el('div', { class: 'settings-swatch-set', role: 'group', 'aria-label': title + ' themes' },
            el('div', { class: 'settings-subhead', text: title }),
            el('div', { class: 'settings-swatches' }, ...list.map(swatch)));
        const themes = window.THEME_LIST || [];
        body.append(
            group('Theme', { id: 'settings-theme' }, set('Dark', themes.filter(t => !t.light)), set('Light', themes.filter(t => t.light))),
            group('Text', { id: 'settings-text' },
                barField('setting-font', 'Font', 'Typeface used across the app.'),
                barField('setting-contrast', 'Text contrast', 'High brightens secondary text.')),
            group('Layout', { id: 'settings-layout' },
                barField('setting-channel-sidebar', 'Channel list', 'Show channels in a bar above the chat or in a sidebar.')));
    }

    function renderNotifications(body) {
        heading(body, 'Notifications', 'Sounds for new messages, stored in this browser. Each agent can have its own sound on its page under Room agents.');
        const box = borrow(document.getElementById('sound-settings'));
        if (box) body.append(card(box));
    }

    function renderGeneral(body) {
        heading(body, 'General', 'Names shown in this room and how much history opens with it.');
        body.append(card(
            barField('setting-username', 'Your name', 'Shown on the messages sent from this browser.'),
            barField('setting-subtitle', 'Server label', 'Shown next to the room title and in the tab title, to tell servers apart.'),
            barField('setting-history', 'Messages loaded', 'How many past messages load when the room opens.')));
    }

    function renderTurns(body) {
        heading(body, 'Turn-taking', 'Who answers a message and how long agents may talk among themselves.');
        body.append(card(
            barField('setting-hops', 'Loop guard', 'Agent-to-agent replies allowed before the room pauses for a person. 0 turns it off.'),
            barField('setting-untagged', 'Untagged messages', 'Who answers a message that names nobody with @. Channel agents: everyone in the channel (nobody in #general). Model picks: the best-suited agent. Random agent: one of them.'),
            barField('setting-picker', 'Picking model', 'The API agent whose model chooses who answers when "Model picks" is set.'),
            barField('setting-name-mentions', 'Names without @', 'Whether a message that contains an agent\'s name is addressed to that agent.'),
            barField('setting-rules-refresh', 'Rule refresh', 'How often the active rules are sent to agents again.')));
    }

    function renderHistory(body) {
        heading(body, 'History', 'Move this room between servers or keep a backup.');
        const btns = borrow(document.querySelector('#settings-bar .settings-history-btns'));
        if (btns) body.append(card(field('Room history', btns,
            { hint: 'Export saves messages, jobs, rules and summaries as a zip. Import adds the contents of such a zip; duplicates are skipped.' })));
    }

    // ------------------------------------------------------------ Emotion engine (emotion_engine.py)
    async function renderEmotion(body) {
        heading(body, 'Emotion engine', 'Feelings for the agents that have it switched on. Each agent has its own personality and values: Agents > Room agents > an agent > Personality and emotion.');
        const [conf, servers, list] = await load(body, Promise.all([api('GET', '/api/emotion/config'),
            api('GET', '/api/admin/llms'), api('GET', '/api/managed-agents')]));
        const note = el('small', { class: 'settings-status', role: 'status', text: 'Loading models…' });
        const save = async (change) => {
            try { Object.assign(conf, await api('POST', '/api/emotion/config', change)); note.textContent = 'Saved'; return true; }
            catch (err) { note.textContent = err.message; return false; }
        };
        const model = el('select', {}, el('option', { value: '', text: 'Each agent\'s own model' }));
        const want = conf.server ? `${conf.server}|${conf.model}` : '';
        Promise.all(servers.map(x => api('POST', `/api/admin/llms/${encodeURIComponent(x.id)}/test`).then(r => r.models).catch(() => []))).then(lists => {
            servers.forEach((x, i) => {
                let host = x.base_url;
                try { host = new URL(x.base_url).host; } catch (_) { /* as typed */ }
                lists[i].forEach(m => model.append(el('option', { value: `${x.id}|${m}`, text: `${x.name} (${host}) · ${m}` })));
            });
            if (want && ![...model.options].some(o => o.value === want)) model.append(el('option', { value: want, text: `${conf.server} · ${conf.model} (unreachable)` }));
            model.value = want;
            note.textContent = '';
        });
        model.addEventListener('change', () => { const [server, m] = model.value ? model.value.split('|') : ['', '']; save({ server, model: m }); });
        const modelRow = field('Emotion model', model, { stack: true,
            hint: 'Judges how each thing said to an agent, and each event reported to it, lands: three numbers and a short reason. A small, fast model is enough. The models come from Providers > LLMs. Agents that run a coding CLI need a model picked here.' });
        modelRow.querySelector('.ui-field-control').append(note);
        const morning = el('select', {}, ...Array.from({ length: 24 }, (_, h) => el('option', { value: String(h), text: `${String(h).padStart(2, '0')}:00` })));
        morning.value = String(conf.morning);
        const zone = el('input', { value: conf.timezone, placeholder: 'e.g. America/Chicago', spellcheck: 'false', autocomplete: 'off' });
        const clock = el('small', { class: 'ui-field-hint', text: `The engine's clock reads ${conf.now}.` });
        const hours = () => Array.from({ length: 24 }, (_, h) => el('option', { value: String(h), text: `${String(h).padStart(2, '0')}:00` }));
        const nightAt = el('select', {}, ...hours());
        nightAt.value = String(conf.night);
        nightAt.addEventListener('change', () => save({ night: Number(nightAt.value) }));
        morning.addEventListener('change', () => save({ morning: Number(morning.value) }));
        zone.addEventListener('change', async () => { if (await save({ timezone: zone.value.trim() })) clock.textContent = `The engine's clock reads ${conf.now}.`; });
        const zoneRow = field('Time zone', zone, { hint: 'An IANA name. Blank: the clock of the machine AgentHub runs on.' });
        zoneRow.querySelector('.ui-field-text').append(clock);
        const feeling = list.filter(a => (a.emotion || {}).enabled);
        const rows = el('div', { class: 'settings-stack' });
        const draw = async () => {
            const states = await Promise.all(feeling.map(a => api('GET', `/api/managed-agents/${encodeURIComponent(a.name)}/emotion`).catch(() => ({ off: true }))));
            if (!rows.isConnected) return;
            rows.textContent = '';
            feeling.forEach((a, i) => rows.append(field(a.label || a.name, el('span', { class: 'settings-value',
                text: states[i].off ? (a.enabled === false ? 'Agent is off' : 'Starting…') : states[i].feeling }),
                { hint: states[i].off || !states[i].why.length ? `@${a.name}` : `@${a.name} · ${states[i].why[states[i].why.length - 1]}` })));
        };
        body.append(
            group('Model', { id: 'settings-emotion-model' }, modelRow),
            group('The day', { id: 'settings-emotion-day' },
                field('Morning starts at', morning, { hint: 'A new day begins at this hour: fresh starts happen and each agent\'s morning shift is drawn.' }),
                field('Night starts at', nightAt, { hint: 'From this hour until the morning no loneliness or boredom builds: a quiet night is not neglect.' }),
                zoneRow),
            group('Agents with feelings', { id: 'settings-emotion-agents', count: feeling.length },
                feeling.length ? rows : empty('No agent has the engine on', 'Open Agents > Room agents, edit an agent, and switch on Emotion engine under Personality and emotion.',
                    btn('Open Room agents', () => show('agents')))));
        if (feeling.length) {
            await draw();
            const timer = setInterval(() => { if (!rows.isConnected) clearInterval(timer); else draw(); }, 5000);
        }
    }

    // ------------------------------------------------------------ Senses (senses.py)
    const WHAT = { ring: 'The doorbell rings', package: 'A package is left', person: 'A person', vehicle: 'A vehicle', animal: 'An animal',
        smoke: 'A smoke alarm is heard', co: 'A carbon monoxide alarm is heard', glass: 'Glass breaking is heard',
        burglar: 'A burglar alarm is heard', siren: 'A siren is heard' };
    const REFLEX_ACTIONS = ['light.turn_on', 'light.turn_off', 'switch.turn_on', 'switch.turn_off', 'fan.turn_on', 'fan.turn_off',
        'scene.turn_on', 'script.turn_on', 'input_boolean.turn_on', 'input_boolean.turn_off', 'media_player.media_pause'];
    let sensesLists = 0;

    async function sensesData(body, withOptions) {
        const [s, agents, options] = await load(body, Promise.all([api('GET', '/api/admin/senses'), api('GET', '/api/managed-agents'),
            withOptions ? api('GET', '/api/admin/senses/options') : null]));
        return { conf: s.settings, state: s.state, options: options || { entities: [], speakers: [], notify: [], actions: [], cameras: [], errors: {} },
            feel: agents.filter(a => (a.emotion || {}).enabled).map(a => a.name),
            speak: agents.filter(a => (a.initiative || {}).enabled).map(a => a.name) };
    }
    const sensesSave = (id, change) => guard(async () => { await api('POST', '/api/admin/senses', change()); refresh(id); });
    const tick = (on) => el('input', { type: 'checkbox', checked: !!on });
    const numIn = (v, min, max) => el('input', { type: 'number', min: String(min), max: String(max), value: String(v) });
    function pick(options, v, none) {   // options: [value, label]; a stored value no longer offered stays selectable
        const sel = el('select', {}, none !== undefined ? el('option', { value: '', text: none }) : null,
            ...options.map(([value, text]) => el('option', { value, text })));
        if (v && !options.some(o => o[0] === v)) sel.append(el('option', { value: v, text: `${v} (not found)` }));
        sel.value = v || '';
        return sel;
    }
    function entityIn(v, list, placeholder) {   // type to search: an entity id, shown with its name
        const id = 'senses-list-' + (++sensesLists);
        return el('span', { class: 'senses-entity' },
            el('input', { value: v || '', list: id, placeholder, spellcheck: 'false', autocomplete: 'off' }),
            el('datalist', { id }, ...list.map(x => el('option', { value: x.id, label: x.kind ? `${x.name} (${x.kind})` : x.name }))));
    }
    const entityRead = (node) => node.querySelector('input').value.trim();
    function agentChecks(names, chosen) {
        const boxes = names.map(n => ({ n, cb: tick(chosen.includes(n)) }));
        const node = names.length ? el('div', { class: 'senses-checks' }, ...boxes.map(b => el('label', {}, b.cb, el('span', { text: '@' + b.n }))))
            : el('span', { class: 'settings-value', text: 'No agent has the emotion engine on' });
        return { node, read: () => boxes.filter(b => b.cb.checked).map(b => b.n) };
    }
    const AGENT_HINT = 'Agents with the emotion engine on (Agents > an agent > Personality and emotion). They feel what this part senses and see it in their prompt.';
    const SPEAKER_HINT = 'The one agent that may say it aloud. It needs Initiative on (Agents > an agent > Initiative).';
    // A list the owner edits row by row. cols: [{key, label, make(row) -> control, read(control), wide}]
    function rowsEditor(rows, cols, blank, addLabel, emptyText) {
        const box = el('div', { class: 'senses-rows' });
        const none = el('p', { class: 'settings-note', text: emptyText });
        const items = [];
        const sync = () => { none.hidden = items.length > 0; };
        const draw = (row) => {
            const controls = {};
            const item = { read: () => Object.fromEntries(cols.map(c => [c.key, c.read(controls[c.key])])) };
            const line = el('div', { class: 'senses-row' }, ...cols.map(c => {
                controls[c.key] = c.make(row);
                return el('label', { class: 'senses-cell' + (c.wide ? ' wide' : '') + (controls[c.key].type === 'checkbox' ? ' check' : '') },
                    el('span', { class: 'senses-cell-label', text: c.label }), controls[c.key]);
            }), el('button', { type: 'button', class: 'ui-btn ghost sm', text: 'Remove', onclick: () => { items.splice(items.indexOf(item), 1); line.remove(); sync(); } }));
            items.push(item);
            box.append(line);
            sync();
        };
        rows.forEach(draw);
        sync();
        return { node: el('div', {}, none, box, actions(btn(addLabel, () => draw({ ...blank })))), read: () => items.map(i => i.read()) };
    }
    const warnIf = (body, text) => { if (text) body.append(el('p', { class: 'settings-warn', role: 'status', text })); };

    async function renderWho(body) {
        heading(body, 'Voice recognition', 'A voice heard by a device is matched with the people who enrolled theirs, so an agent knows who is talking. No audio is kept: a person is a set of numbers learned from a few sentences.');
        const [s, devices] = await load(body, Promise.all([api('GET', '/api/admin/speakers'), api('GET', '/api/admin/voice-devices')]));
        warnIf(body, s.error && `The speaker model could not run: ${s.error}`);
        const on = tick(s.enabled);
        const model = el('input', { value: s.model, placeholder: '/path/to/speaker-model.onnx', spellcheck: 'false' });
        const th = numIn(s.threshold, 0.3, 0.95);
        th.step = '0.01';
        const post = (b) => api('POST', '/api/admin/speakers', b);
        const send = (body2) => guard(async () => { await post(body2()); refresh('voice-who'); });

        // Guided enrollment: a name and a device, then the passage is read to that device and followed word by word.
        const wizard = el('div', { class: 'enroll' });
        const idle = () => wizard.replaceChildren(actions(btn('Enroll a voice', form, 'primary sm')));
        function form() {
            const name = el('input', { placeholder: 'Name' });
            const device = pick(devices.map(d => [d.name, `${d.name} (#${d.channel})`]), devices.length === 1 ? devices[0].name : '', 'Select a device');
            const note = el('small', { class: 'settings-status', role: 'status' });
            wizard.replaceChildren(
                field('Name', name, { hint: 'Who this voice belongs to. An existing name gets more samples.' }),
                field('Device', device, { hint: devices.length ? 'The device to read to. While this runs, what it hears goes to no agent.' : 'No voice device is set up: add one under Devices first.' }),
                actions(btn('Start', async () => {
                    try { reading((await post({ enroll: { name: name.value.trim(), device: device.value } })).enroll); }
                    catch (err) { note.textContent = err.message; }
                }, 'primary sm'), btn('Cancel', idle, 'ghost sm')), note);
            name.focus();
        }
        function reading(st) {
            const text = el('p', { class: 'enroll-text', 'aria-label': 'Text to read aloud' });
            const status = el('p', { class: 'settings-status', role: 'status' });
            const heard = el('p', { class: 'enroll-heard', 'aria-live': 'polite' });
            const skip = btn('Skip word', async () => draw((await post({ enroll: 'skip' })).enroll), 'sm');
            const end = guard(async () => { await post({ enroll: 'cancel' }); refresh('voice-who'); });
            const cancel = btn('Cancel', end, 'ghost sm');
            wizard.replaceChildren(
                el('p', { class: 'settings-note', text: `Read this aloud to ${st.device}, in your normal voice and at your normal pace. If the device waits for a wake word, say it first. A word is marked once it is heard. A word struck through in red was not heard: say it again and read on.` }),
                text, heard, status, actions(skip, cancel));
            function draw(now) {
                if (!now) return idle();   // it ended elsewhere, or nothing was heard for three minutes
                const skipped = new Set(now.skipped);
                let i = 0, total = 0;      // the server counts words the same way: runs of letters and apostrophes
                text.replaceChildren(...now.text.split(/([A-Za-z']+)/).map((part, n) => {
                    if (n % 2 === 0) return part;
                    const at = i++;
                    total = i;
                    const cls = skipped.has(at) ? 'skip' : at < now.pos ? 'ok' : at === now.pos ? (now.stuck ? 'bad' : 'next') : '';
                    return el('span', { class: ('enroll-word ' + cls).trim(), text: part });
                }));
                heard.textContent = now.heard ? `Heard: ${now.heard}` : 'Nothing heard yet.';
                skip.disabled = !now.stuck;
                if (!now.done) { status.textContent = `${now.pos} of ${total} words.`; return; }
                status.textContent = now.error || `${now.name} is enrolled from ${now.samples} sentence${now.samples === 1 ? '' : 's'}.`;
                status.classList.toggle('settings-warn', !!now.error);
                cancel.replaceWith(btn('Done', end, 'primary sm'));
                skip.remove();
                return true;
            }
            (async function poll() {   // stops when the page is left or the reading is over
                while (wizard.isConnected && text.isConnected) {
                    try { if (draw((await api('GET', '/api/admin/speakers')).enroll) || !text.isConnected) return; } catch { /* next round */ }
                    await new Promise(r => setTimeout(r, 250));
                }
            })();
            draw(st);
            wizard.scrollIntoView({ block: 'start' });   // the text to read is the page now
        }
        if (s.enroll && !s.enroll.done) reading(s.enroll); else idle();

        const people = el('div', { class: 'settings-stack' });
        for (const p of s.people) people.append(field(p.name, btn('Forget', send(() => ({ forget: p.name })), 'ghost sm'), { hint: `${p.samples} sample${p.samples === 1 ? '' : 's'}` }));
        body.append(
            card(field('Recognize voices', on, { hint: 'Off: every voice is the room\'s user.' }),
                field('Model file', model, { hint: 'A speaker-embedding ONNX file on the server, run by sherpa-onnx on the CPU.' }),
                field('How sure (0.30 to 0.95)', th, { hint: 'Below this a voice is nobody in particular. Higher: fewer wrong names, more unknowns.' })),
            actions(btn('Save', send(() => ({ enabled: on.checked, model: model.value.trim(), threshold: Number(th.value) || 0.65 })), 'primary')),
            group('People', { id: 'settings-who', count: s.people.length }, s.people.length ? people : empty('Nobody is enrolled', 'Until somebody is, no voice is given a name.'), wizard));
    }

    async function renderGames(body) {
        heading(body, 'Games', 'An agent hosts a game for the people in a channel or a room, by voice or in chat. It brings the questions; AgentHub keeps the players, the turn and the score.');
        const [g, list] = await load(body, Promise.all([api('GET', '/api/admin/games'), api('GET', '/api/managed-agents')]));
        const on = tick(g.enabled);
        const names = list.filter(a => a.kind === 'api' && a.permissions !== 'none').map(a => a.name);
        const boxes = names.map(n => ({ n, cb: tick(g.hosts.includes(n)) }));
        const rows = rowsEditor(g.games, [
            { key: 'name', label: 'Game', make: r => el('input', { value: r.name }), read: n => n.value.trim() },
            { key: 'rounds', label: 'Rounds', make: r => numIn(r.rounds, 1, 100), read: n => Number(n.value) || 10 },
            { key: 'rules', label: 'Rules, in plain words', wide: true, make: r => { const t = el('textarea', { rows: 3 }); t.value = r.rules; return t; }, read: n => n.value.trim() },
        ], { name: '', rounds: 10, rules: '' }, 'Add a game', 'No games.');
        body.append(
            card(field('Games', on, { hint: 'Off: no agent offers or hosts a game.' }),
                field('Hosts', names.length ? el('div', { class: 'senses-checks' }, ...boxes.map(b => el('label', {}, b.cb, el('span', { text: '@' + b.n })))) : el('span', { class: 'settings-value', text: 'No agent with tools' }),
                    { stack: true, hint: 'Agents that may host. Ask one to start a game and say who is playing.' })),
            group('Games', { id: 'settings-games', count: g.games.length }, rows.node),
            actions(btn('Save', guard(async () => { await api('POST', '/api/admin/games', { enabled: on.checked, hosts: boxes.filter(b => b.cb.checked).map(b => b.n), games: rows.read() }); refresh('games'); }), 'primary')));
        // The trivia question bank (trivia.py): nothing is included; each source is downloaded here
        const bank = el('div', { class: 'settings-stack' });
        const levels = el('p', { class: 'settings-note' });
        const mine = el('div', { class: 'settings-stack' });
        const post = async (b) => drawBank(await api('POST', '/api/admin/trivia', b));
        function drawBank(t) {
            bank.textContent = '';
            for (const s of t.sources) {
                const n = Object.values(s.counts).reduce((a, b) => a + b, 0);
                const job = s.job || {};
                const state = job.state === 'running' ? `Downloading… ${job.got} new so far` : job.state === 'failed' ? `Failed: ${job.error}` : n ? `${n} questions` : 'Not downloaded';
                bank.append(field(s.name, el('span', { class: 'senses-entity' }, btn(n ? 'Download again' : 'Download', guard(() => post({ download: s.id })), 'sm')),
                    { hint: `${s.about} ${state}.` }));
            }
            levels.textContent = `In the bank: level 1 (easy) ${t.levels[1]}, level 2 ${t.levels[2]}, level 3 ${t.levels[3]}, level 4 (hardest) ${t.levels[4]}. Set aside as wrong: ${t.flagged}.`;
            mine.textContent = '';
            for (const q of t.own) mine.append(field(`Level ${q.level}${q.category ? ' · ' + q.category : ''}`, btn('Delete', guard(() => post({ delete: q.id })), 'ghost sm'), { hint: `${q.question} (${q.answer})` }));
            return t;
        }
        const t = drawBank(await api('GET', '/api/admin/trivia'));
        const q = el('input', { placeholder: 'The question' }), ans = el('input', { placeholder: 'Its answer' }), cat = el('input', { placeholder: 'Category (optional)' });
        const lvl = pick([['1', 'Level 1 (easy)'], ['2', 'Level 2'], ['3', 'Level 3'], ['4', 'Level 4 (hardest)']], '2');
        body.append(group('Trivia questions', { id: 'settings-trivia' },
            el('p', { class: 'settings-note', text: 'A bank of questions with their answers, so the host reads them out instead of making them up. No questions come with AgentHub: download the sources you want. They are kept on this server and used offline. Say the level when a game starts ("trivia, level 3").' }),
            bank, levels,
            field('Your own question', q, { stack: true }), field('Answer', ans), field('Category', cat), field('Level', lvl),
            actions(btn('Add the question', guard(async () => { await post({ add: { question: q.value, answer: ans.value, category: cat.value, level: Number(lvl.value) } }); q.value = ans.value = ''; }))),
            mine));
        const timer = setInterval(async () => {
            if (!bank.isConnected) return clearInterval(timer);
            if ([...bank.querySelectorAll('.ui-field-hint')].some(h => h.textContent.includes('Downloading'))) { try { drawBank(await api('GET', '/api/admin/trivia')); } catch (_) { /* next round */ } }
        }, 4000);
    }

    async function renderSenses(body) {
        heading(body, 'Senses', 'What the agents know of the house and how they are heard in it. Each part is set up on its own page; nothing is said aloud unless a row there says so.');
        const d = await sensesData(body, false);
        const on = tick(d.conf.enabled);
        const called = el('input', { value: d.conf.called, placeholder: 'e.g. Alex (blank: the room\'s user name)' });
        const muted = tick(d.state.muted);
        muted.addEventListener('change', guard(async () => { await api('POST', '/api/admin/senses/mute', { on: muted.checked }); }));
        const PARTS = [['ha', 'Home Assistant', d.conf.ha.enabled], ['cameras', 'Cameras', d.conf.cameras.enabled],
            ['mqtt', 'Desk display and events in', d.conf.display.enabled || d.conf.events.enabled]];
        const status = el('div', { class: 'settings-stack' });
        const log = el('div', { class: 'senses-log' });
        const draw = (st) => {
            status.textContent = '';
            for (const [key, label, enabled] of PARTS) {
                const text = !d.conf.enabled || !enabled ? 'Off' : (st.status[key] || 'Starting');
                const ago = (st.heard || {})[key];
                const last = ago === undefined ? 'Nothing heard since it connected.' : `Last heard ${ago < 90 ? ago + ' s' : Math.round(ago / 60) + ' min'} ago.`;
                status.append(field(label, window.UIKit.badge(text.split(':')[0], text === 'connected' ? 'ok' : text === 'Off' ? '' : 'warn'),
                    { hint: text.includes(':') ? text.slice(text.indexOf(':') + 2) : text === 'connected' && key !== 'mqtt' ? last : '' }));
            }
            status.append(field('Somebody is home', el('span', { class: 'settings-value', text: st.people_home ? 'Yes' : 'No' }),
                { hint: st.open.length ? `Open or unlocked now: ${st.open.join(', ')}` : 'Nothing that is watched is open.' }));
            log.textContent = '';
            if (!st.activity.length) log.append(empty('Nothing has been sensed yet', 'Each thing a part senses is listed here with what became of it: felt, said, or held back and why.'));
            for (const a of st.activity) {
                log.append(el('div', { class: 'senses-log-row' },
                    el('time', { text: new Date(a.at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) }),
                    el('span', { class: 'senses-log-part', text: a.part }),
                    el('span', { class: 'senses-log-text', text: a.text }),
                    el('span', { class: 'senses-log-did', text: a.did })));
            }
        };
        draw(d.state);
        body.append(
            card(field('Senses', on, { hint: 'Off: no part runs and the agents are told nothing.' }),
                field('The people here are called', called, { hint: 'Used when an agent is handed something to tell them.' }),
                field('Mute', muted, { hint: 'Nothing is said unprompted while this is on. A long press of the desk display\'s knob does the same.' })),
            actions(btn('Save', sensesSave('senses', () => ({ enabled: on.checked, called: called.value.trim() })), 'primary')),
            group('Parts', { id: 'senses-parts' }, status),
            group('Activity', { id: 'senses-activity' }, log));
        const timer = setInterval(async () => {
            if (!log.isConnected) return clearInterval(timer);
            try { draw(await api('GET', '/api/admin/senses/state')); } catch (_) { /* shown on the next round */ }
        }, 5000);
    }

    async function renderSensesHa(body) {
        heading(body, 'Home Assistant', 'Who is home, and the things that are watched. Only a row that says so is said aloud; every row is felt.');
        const d = await sensesData(body, true);
        const c = d.conf.ha;
        warnIf(body, d.options.errors.ha && `Home Assistant could not be read: ${d.options.errors.ha}`);
        const on = tick(c.enabled);
        const url = el('input', { value: c.url, placeholder: 'http://homeassistant.local:8123', spellcheck: 'false' });
        const token = el('input', { type: 'password', autocomplete: 'new-password', placeholder: c.has_token ? 'Stored. Leave blank to keep it.' : 'Blank: the token of the MCP server at the same address' });
        const agents = agentChecks(d.feel, c.agents);
        const speaker = pick(d.speak.map(n => [n, '@' + n]), c.speaker, 'Nobody: nothing is said');
        const notify = pick(d.options.notify.map(n => [n, n]), c.notify, 'None');
        const odd = tick(c.unusual);
        const L = d.state.learned || { days: 0, things: 0, ready: false };
        const things = d.options.entities.filter(e => !['motion', 'occupancy'].includes(e.kind));
        const watch = rowsEditor(c.watch, [
            { key: 'entity', label: 'Thing', wide: true, make: r => entityIn(r.entity, things, 'Type to search doors, windows, locks, leaks'), read: entityRead },
            { key: 'say', label: 'Say aloud', make: r => tick(r.say), read: n => n.checked },
            { key: 'remind_min', label: 'Bring up again after (min)', make: r => numIn(r.remind_min, 0, 1440), read: n => Number(n.value) || 0 },
            { key: 'reminders', label: 'How many times', make: r => numIn(r.reminders, 0, 9), read: n => Number(n.value) || 0 },
        ], { entity: '', say: true, remind_min: 10, reminders: 2 }, 'Add a thing to watch', 'Nothing is watched yet. Add the doors, windows, locks and leak sensors that matter; anything not listed is ignored.');
        body.append(
            card(field('Home Assistant', on, { hint: 'Read live. Nothing is changed there, except by a reflex.' }),
                field('Address', url), field('Token', token, { hint: 'A long-lived access token. Write-only.' }),
                field('Agents', agents.node, { stack: true, hint: AGENT_HINT }),
                field('Says it aloud', speaker, { hint: SPEAKER_HINT }),
                field('Phone', notify, { hint: 'A notify service for alarms and leaks when nobody is home, or it is the agent\'s quiet hours.' }),
                field('Weigh what is unusual', odd, { hint: `What is usual is learned by the hour of the week: when people come and go and when each watched thing opens. So far: ${L.days} days over ${L.things} things${L.ready ? '' : ' (nothing is called unusual before 7 days)'}. Every unusual event is marked as such for the agents. On: one on a row that is not said aloud, or a person coming or going at an hour they never have, is handed to the agent to weigh and say only if it is worth it.` })),
            group('Watched things', { id: 'senses-watch', count: c.watch.length }, watch.node,
                el('p', { class: 'settings-note', text: 'Say aloud: its opening is said once. Bring up again: while it stays open, as many times as set, then it is let be. An answer to the agent, or a press of the knob, ends that until it has closed and opened again.' })),
            actions(btn('Save', sensesSave('senses-ha', () => ({ ha: { enabled: on.checked, url: url.value.trim(), token: token.value,
                agents: agents.read(), speaker: speaker.value, notify: notify.value, unusual: odd.checked, watch: watch.read() } })), 'primary')));
    }

    async function renderSensesCameras(body) {
        heading(body, 'Cameras', 'UniFi Protect. A trigger is one explicit thing on one camera, said once per visit. Anything without a row is ignored.');
        const d = await sensesData(body, true);
        const c = d.conf.cameras;
        warnIf(body, d.options.errors.cameras && `The cameras could not be read: ${d.options.errors.cameras}`);
        const on = tick(c.enabled);
        const url = el('input', { value: c.url, placeholder: 'https://192.168.1.1', spellcheck: 'false' });
        const key = el('input', { type: 'password', autocomplete: 'new-password', placeholder: c.has_key ? 'Stored. Leave blank to keep it.' : 'An API key made on the console' });
        const agents = agentChecks(d.feel, c.agents);
        const speaker = pick(d.speak.map(n => [n, '@' + n]), c.speaker, 'Nobody: nothing is said');
        const every = numIn(c.describe_min, 0, 1440);
        const gap = numIn(c.gap_min, 0, 1440);
        const model = el('select', {}, el('option', { value: '', text: 'None: the cameras are not described' }));
        const want = c.vision.server ? `${c.vision.server}|${c.vision.model}` : '';
        if (want) { model.append(el('option', { value: want, text: `${c.vision.model || 'default model'} (loading…)` })); model.value = want; }
        api('GET', '/api/admin/llms').then(servers => Promise.all(servers.map(x => api('POST', `/api/admin/llms/${encodeURIComponent(x.id)}/test`)
            .then(r => r.models).catch(() => []))).then(lists => {
            model.textContent = '';
            model.append(el('option', { value: '', text: 'None: the cameras are not described' }));
            servers.forEach((x, i) => lists[i].forEach(m => model.append(el('option', { value: `${x.id}|${m}`, text: `${x.name} · ${m}` }))));
            if (want && ![...model.options].some(o => o.value === want)) model.append(el('option', { value: want, text: `${c.vision.model} (unreachable)` }));
            model.value = want;
        }), () => {});
        const cams = d.options.cameras.map(x => [x.name, x.ok ? x.name : `${x.name} (offline)`]);
        const triggers = rowsEditor(c.triggers, [
            { key: 'camera', label: 'Camera', make: r => pick(cams, r.camera, 'Any camera'), read: n => n.value },
            { key: 'what', label: 'What sets it off', make: r => pick(Object.entries(WHAT), r.what), read: n => n.value },
            { key: 'stay_s', label: 'Only if it stays (s)', make: r => numIn(r.stay_s, 0, 600), read: n => Number(n.value) || 0 },
            { key: 'say', label: 'Said aloud', make: r => pick([['always', 'Always'], ['judge', 'The agent looks and decides']], r.say === true ? 'always' : r.say || '', 'No: only felt'), read: n => n.value },
            { key: 'again_min', label: 'Not again for (min)', make: r => numIn(r.again_min, 1, 1440), read: n => Number(n.value) || 10 },
            { key: 'when', label: 'Worth saying when (for "looks and decides")', wide: true, make: r => el('input', { value: r.when || '', placeholder: 'e.g. a delivery, or a visitor waiting at the door; not a neighbour walking past' }), read: n => n.value.trim() },
        ], { camera: '', what: 'ring', stay_s: 0, say: '', when: '', again_min: 10 }, 'Add a trigger', 'No triggers: the cameras set nothing off.');
        const can = d.options.cameras.filter(x => x.ok).map(x => `${x.name}: ${[x.doorbell ? 'doorbell' : '', ...x.smart, ...(x.audio.length ? ['sounds'] : [])].filter(Boolean).join(', ') || 'no detections'}`);
        body.append(
            card(field('Cameras', on, { hint: 'Events are read live. A frame is held in memory for one look; it is saved only when an agent is asked for a picture and attaches it to its reply.' }),
                field('Console address', url, { hint: 'The console signs its own certificate; it is used unchecked for this address.' }),
                field('API key', key, { hint: 'Made on the console under Control Plane > Integrations. Write-only.' }),
                field('Agents', agents.node, { stack: true, hint: AGENT_HINT + ' They can also take a fresh look through a camera, and attach the picture when asked. Never on a turn started by someone other than the owner on Discord.' }),
                field('Says it aloud', speaker, { hint: SPEAKER_HINT }),
                field('Describes the cameras', model, { stack: true, hint: 'A model that reads images. It writes one sentence per camera for the agents, and says what is there when a trigger is said aloud.' }),
                field('Describe every (minutes)', every, { hint: '0: only when an agent looks or a trigger is said.' }),
                field('One spoken camera event per (minutes)', gap, { hint: 'After one is said, the others are only felt for this long, whatever the camera: one visit, one announcement.' })),
            group('Triggers', { id: 'senses-triggers', count: c.triggers.length }, triggers.node,
                el('p', { class: 'settings-note', text: 'Only if it stays: the person or vehicle must still be there after this many seconds, so one walking or driving past sets nothing off. 0: at once. Said aloud "No": the agents only feel it, and never bring it up by themselves. "The agent looks and decides": it is handed what the camera shows (the kind of person, what they are doing, the type and colour of a vehicle) and says it only if it is worth it; write what is worth saying in the last field.' }),
                can.length ? el('p', { class: 'settings-note', text: 'What each camera can report. ' + can.join('. ') + '.' }) : null),
            actions(btn('Save', sensesSave('senses-cameras', () => {
                const [server, m] = model.value ? model.value.split('|') : ['', ''];
                return { cameras: { enabled: on.checked, url: url.value.trim(), api_key: key.value, agents: agents.read(), speaker: speaker.value,
                    describe_min: Number(every.value) || 0, gap_min: Number(gap.value) || 0, vision: { server, model: m }, triggers: triggers.read() } };
            }), 'primary')));
    }

    async function renderSensesOut(body) {
        heading(body, 'Speakers and display', 'Where an agent is heard and seen, and events other services send in.');
        const d = await sensesData(body, true);
        const sp = d.conf.speakers, dis = d.conf.display, ev = d.conf.events;
        warnIf(body, d.options.errors.ha && `Home Assistant could not be read: ${d.options.errors.ha}`);
        const spOn = tick(sp.enabled);
        const publicUrl = el('input', { value: sp.public_url || location.origin, spellcheck: 'false' });
        const speakers = d.options.speakers.map(x => [x.id, x.ok ? x.name : `${x.name} (offline)`]);
        const rooms = rowsEditor(sp.rooms, [
            { key: 'name', label: 'Room', make: r => el('input', { value: r.name, placeholder: 'e.g. office' }), read: n => n.value.trim() },
            { key: 'speaker', label: 'Speaker', wide: true, make: r => pick(speakers, r.speaker, 'Pick a speaker'), read: n => n.value },
            { key: 'motion', label: 'Its motion sensors contain', make: r => el('input', { value: r.motion, placeholder: 'e.g. office', spellcheck: 'false' }), read: n => n.value.trim() },
        ], { name: '', speaker: '', motion: '' }, 'Add a room', 'No rooms: nothing can be spoken.');
        const fallback = el('input', { value: sp.default_room, placeholder: 'A room name from the list' });
        const testRoom = pick(sp.rooms.map(r => [r.name, r.name]), (sp.rooms[0] || {}).name);
        const testNote = el('small', { class: 'settings-status', role: 'status' });
        const testRow = field('Test a speaker', testRoom, { hint: 'Speaks one line in the agent\'s voice in a saved room.' });
        testRow.querySelector('.ui-field-control').append(btn('Test', async () => {
            testNote.textContent = 'Speaking…';
            try { const r = await api('POST', '/api/admin/senses/test-room', { room: testRoom.value }); testNote.textContent = `Sent as @${r.agent} (${r.seconds} s).`; }
            catch (err) { testNote.textContent = err.message; }
        }, 'sm'), testNote);
        const disOn = tick(dis.enabled);
        const prefix = el('input', { value: dis.prefix, placeholder: 'e.g. hazel', spellcheck: 'false' });
        const disAgent = pick(d.feel.map(n => [n, '@' + n]), dis.agent, 'Pick an agent');
        const evOn = tick(ev.enabled);
        const topics = el('input', { value: ev.topics.join(', '), placeholder: 'e.g. house/event', spellcheck: 'false' });
        const evAgents = agentChecks(d.feel, ev.agents);
        const evSpeaker = pick(d.speak.map(n => [n, '@' + n]), ev.speaker, 'Nobody: nothing is said');
        body.append(
            group('Speakers', { id: 'senses-speakers' },
                field('Speak in the house', spOn, { hint: 'What an agent says unprompted is spoken in its own voice, in the room with the latest motion.' }),
                field('AgentHub\'s address', publicUrl, { hint: 'As Home Assistant reaches it: the speaker fetches the speech from here.' }),
                rooms.node,
                field('Room without recent motion', fallback, { hint: 'Used when no room had motion in the last half hour.' }),
                sp.rooms.length ? testRow : null),
            group('Desk display', { id: 'senses-display' },
                field('Desk display', disOn, { hint: 'A face over MQTT (the broker of Providers > MQTT): its resting face follows the agent\'s feelings, its mouth the agent\'s speech.' }),
                field('Topic prefix', prefix, { hint: 'Publishes <prefix>/state, /gesture and /mute; reads <prefix>/knob (press: acknowledge, long press: mute).' }),
                field('Shows', disAgent)),
            group('Events in', { id: 'senses-events' },
                field('Events from other services', evOn, { hint: 'MQTT topics that carry {"description": "...", "action": "speak"}. Without the action an event is only felt.' }),
                field('Topics', topics, { hint: 'Comma-separated, no wildcards.' }),
                field('Agents', evAgents.node, { stack: true, hint: AGENT_HINT }),
                field('Says it aloud', evSpeaker, { hint: SPEAKER_HINT })),
            actions(btn('Save', sensesSave('senses-speakers', () => ({
                speakers: { enabled: spOn.checked, public_url: publicUrl.value.trim(), rooms: rooms.read(), default_room: fallback.value.trim() },
                display: { enabled: disOn.checked, prefix: prefix.value.trim(), agent: disAgent.value },
                events: { enabled: evOn.checked, topics: topics.value.split(',').map(t => t.trim()).filter(Boolean), agents: evAgents.read(), speaker: evSpeaker.value },
            })), 'primary')));
    }

    async function renderSensesReflexes(body) {
        heading(body, 'Reflexes', 'A fixed question asked the moment something happens, and one action for yes. No model takes part, so it is immediate.');
        const d = await sensesData(body, true);
        warnIf(body, d.options.errors.ha && `Home Assistant could not be read: ${d.options.errors.ha}`);
        warnIf(body, !(d.conf.speakers.enabled && d.conf.speakers.rooms.length) && 'A reflex is asked on a room speaker: set up Speakers and display first.');
        const rows = rowsEditor(d.conf.reflexes, [
            { key: 'enabled', label: 'On', make: r => tick(r.enabled), read: n => n.checked },
            { key: 'name', label: 'Name', make: r => el('input', { value: r.name, placeholder: 'e.g. Entry lights' }), read: n => n.value.trim() },
            { key: 'entity', label: 'When this turns on or opens', wide: true, make: r => entityIn(r.entity, d.options.entities, 'Type to search'), read: entityRead },
            { key: 'question', label: 'Ask', wide: true, make: r => el('input', { value: r.question, placeholder: 'Front door open. Shall I turn the lights on?' }), read: n => n.value.trim() },
            { key: 'service', label: 'On yes', make: r => pick(REFLEX_ACTIONS.map(a => [a, a]), r.service, 'Pick an action'), read: n => n.value },
            { key: 'target', label: 'Acting on', wide: true, make: r => entityIn(r.target, d.options.actions, 'Type to search lights, switches, scenes'), read: entityRead },
            { key: 'again_min', label: 'Not again for (min)', make: r => numIn(r.again_min, 1, 1440), read: n => Number(n.value) || 10 },
        ], { enabled: false, name: '', entity: '', state: 'on', question: '', service: '', target: '', again_min: 10 }, 'Add a reflex', 'No reflexes yet.');
        body.append(
            group('Reflexes', { id: 'senses-reflexes', count: d.conf.reflexes.length }, rows.node,
                el('p', { class: 'settings-note', text: 'The question is asked in the voice of the agent that speaks for Home Assistant, on the speaker of the room with the latest motion. "Yes" runs the action; "no" or silence does nothing. Doors, locks, covers and alarms are never a reflex. Not asked while muted, in the agent\'s quiet hours, or when nobody is home.' })),
            actions(btn('Save', sensesSave('senses-reflexes', () => ({ reflexes: rows.read().map(r => ({ ...r, state: 'on' })) })), 'primary')));
    }

    function renderAgents(body) {
        if (!window.AgentsSection) throw new Error('The agents page did not load (agents-admin.js)');
        return window.AgentsSection.mount(body);
    }

    // ------------------------------------------------------------ Agent invites
    async function renderConnections(body) {
        heading(body, 'Agent invites', 'Agents on the network join by themselves: they register and receive their own key.');
        const st = await load(body, api('GET', '/api/admin/registration'));

        body.append(secretBox('Join instructions for agents', st.llms_txt,
            'Say to any agent: "Read ' + st.llms_txt + ' and join the room as <name>."'));

        const openReg = el('input', { type: 'checkbox', checked: st.open_registration });
        openReg.addEventListener('change', guard(async () => {
            await api('POST', '/api/admin/registration', { open_registration: openReg.checked });
            refresh('connections');
        }));
        body.append(card(field('Open registration', openReg,
            { hint: 'Any agent on the allowed networks can join without an invite.' })));

        body.append(group('Joined agents', { id: 'settings-joined', count: st.agents.length },
            !st.agents.length
                ? empty('No agent has joined yet', 'Agents that register with the join instructions are listed here, with a control to revoke each one.')
                : table(['Agent', 'Status', 'Joined', ''], st.agents.map(a => [
                    `@${a.name}${a.description ? ' · ' + a.description : ''}`,
                    a.online ? 'online' : 'offline', a.joined || '',
                    btn('Revoke', guard(async () => {
                        if (!confirm(`Remove ${a.name} and invalidate its key?`)) return;
                        await api('DELETE', `/api/admin/remote-agents/${encodeURIComponent(a.name)}`);
                        refresh('connections');
                    }), 'danger sm')]))));

        const label = el('input', { placeholder: 'e.g. Hermes on laptop' });
        const ttl = el('input', { type: 'number', min: '1', value: '24' });
        const out = el('div');
        const create = btn('Create invite', guard(async () => {
            const r = await api('POST', '/api/admin/invites', { label: label.value, ttl_hours: Number(ttl.value) });
            out.textContent = '';
            out.append(secretBox('Invite code', r.code));
        }), 'primary');
        body.append(group('Invites', { id: 'settings-invites', count: st.invites.length },
            st.invites.length
                ? table(['Label', 'Status', 'Created', 'Used by', ''], st.invites.map(i => [
                    i.label || '(no label)', i.status, i.created, i.used_by || '',
                    btn('Delete', guard(async () => {
                        await api('DELETE', `/api/admin/invites/${i.id}`);
                        refresh('connections');
                    }), 'danger sm')]))
                : empty('No invites', 'An invite is a one-time code for joining while open registration is off. It works once.'),
            el('div', { class: 'settings-subhead', text: 'New invite' }),
            field('Label', label, { hint: 'A note on who the invite is for.' }),
            field('Hours valid', ttl),
            actions(create), out));
    }

    // ------------------------------------------------------------ API keys
    async function renderApiKeys(body, created) {
        heading(body, 'API keys', 'Keys for OpenAI-compatible clients such as OpenCode or Hermes.');
        if (created) body.append(secretBox(`Key "${created.name}"`, created.key));
        const name = el('input', { placeholder: 'What connects, e.g. opencode' });
        const form = el('form', {},
            field('Name', name, { hint: 'One key per client, named after what connects.' }),
            actions(el('button', { type: 'submit', class: 'ui-btn primary', text: 'Create key' })));
        form.addEventListener('submit', guard(async (ev) => {
            ev.preventDefault();
            const r = await api('POST', '/api/admin/v1-keys', { name: name.value });
            if (current === 'apikeys') mount(b => renderApiKeys(b, r));
        }));
        const keys = await load(body, api('GET', '/api/admin/v1-keys'));
        const add = group('New key', { open: false }, form);
        body.append(
            group('Keys', { id: 'settings-apikeys', count: keys.length },
                keys.length
                    ? table(['Name', 'Created', 'Last used', ''], keys.map(k => [
                        k.name, k.created, k.last_used || 'never',
                        btn('Revoke', guard(async () => {
                            if (!confirm(`Revoke key "${k.name}"? Clients using it stop working.`)) return;
                            await api('DELETE', `/api/admin/v1-keys/${encodeURIComponent(k.name)}`);
                            refresh('apikeys');
                        }), 'danger sm')]))
                    : empty('No API keys', 'A key lets an outside client talk to the room or to one agent through the OpenAI-style API.',
                        btn('Create key', () => { add.open = true; name.focus(); }, 'primary'))),
            add,
            group('Connection details', { id: 'settings-apikeys-how' },
                field('Base URL', value(location.origin + '/v1')),
                field('Models', value('agenthub-room'), { hint: 'agenthub-room reaches every agent. An agent name as the model reaches that agent only.' })));
    }

    // ------------------------------------------------------------ Secrets
    async function renderSecrets(body, editing) {
        const e = editing || {};
        heading(body, e.name ? `Edit ${e.name}` : 'Secrets',
            'Tokens for outside services, given to chosen agents as an environment variable. Values are write-only.');
        const [secrets, agents] = await load(body, Promise.all([api('GET', '/api/admin/secrets'), api('GET', '/api/managed-agents')]));
        const name = el('input', { value: e.name || '', placeholder: 'e.g. GitHub (Claude commits)', readonly: !!e.name });
        const env = el('input', { value: e.env || '', placeholder: 'GH_TOKEN' });
        const val = el('input', { type: 'password', autocomplete: 'off',
            placeholder: e.has_value ? 'Stored. Leave blank to keep it.' : 'Token value' });
        const grants = checks(agents.map(a => a.name), e.grants, '@');   // every agent can use secrets in its commands
        const form = el('form', {},
            field('Name', name),
            field('Environment variable', env, { hint: 'The name agents read the value under in the commands they run.' }),
            field('Value', val),
            field('Agents allowed to use it', agents.length ? grants.node : note('No agents yet. Add one under Agents > Room agents.'), { stack: true }),
            actions(e.name ? btn('Cancel', () => show('secrets')) : null,
                el('button', { type: 'submit', class: 'ui-btn primary', text: 'Save secret' })));
        form.addEventListener('submit', guard(async (ev) => {
            ev.preventDefault();
            await api('POST', '/api/admin/secrets', { name: name.value.trim(), env: env.value.trim(), value: val.value,
                grants: grants.values() });
            refresh('secrets');
        }));
        if (e.name) { body.append(card(form)); return; }
        const add = group('New secret', { open: false }, form);
        body.append(
            group('Secrets', { id: 'settings-secrets', count: secrets.length },
                secrets.length
                    ? table(['Name', 'Variable', 'Allowed agents', 'Updated', ''], secrets.map(x => [
                        x.name, x.env, (x.grants || []).map(g => '@' + g).join(', ') || 'nobody', x.updated || x.created,
                        actions(
                            btn('Edit', () => mount(b => renderSecrets(b, x)), 'sm'),
                            btn('Delete', guard(async () => {
                                if (!confirm(`Delete secret "${x.name}"? Agents lose ${x.env} from their next turn.`)) return;
                                await api('DELETE', `/api/admin/secrets/${encodeURIComponent(x.name)}`);
                                refresh('secrets');
                            }), 'danger sm'))]))
                    : empty('No secrets', 'A secret is a token for an outside service, such as GitHub or a cloud CLI, handed only to the agents ticked for it.',
                        btn('Add a secret', () => { add.open = true; name.focus(); }, 'primary'))),
            add,
            note('Limit: all agents run as the same Linux user on this server. Claude Code agents are blocked from reading the AgentHub data folder with their file tools, but an agent with full access could still find a secret through the shell. Scope tokens narrowly, for example a fine-grained GitHub token for one repository.'));
    }

    // ------------------------------------------------------------ Voice
    const voiceConf = (body) => load(body, api('GET', '/api/voice/config'));
    // One endpoint holds providers, default provider, saved-audio size and speech-to-text:
    // each voice page sends the whole set, with the parts it does not show unchanged.
    const saveVoice = (conf, part) => api('POST', '/api/voice/config', {
        providers: conf.providers.map(p => ({ ...p, parameters: { ...(p.parameters || {}) } })), default_provider: conf.default_provider, cache_mb: String(conf.cache_mb ?? 512),
        stt: { wyoming: (conf.stt.wyoming || '').trim(), language: conf.stt.language || 'en' }, ...part });

    function renderVoice(body) {
        heading(body, 'Conversation', 'How hands-free talk and the mic button behave. Each agent picks its provider, voice and speed on its own page under Room agents.');
        // Per browser, saved at once
        const local = (key, fallback) => {
            const cb = el('input', { type: 'checkbox' });
            try { cb.checked = JSON.parse(localStorage.getItem(key) ?? fallback); } catch (_) { cb.checked = fallback === 'true'; }
            cb.addEventListener('change', () => { try { localStorage.setItem(key, JSON.stringify(cb.checked)); } catch (_) {} });
            return cb;
        };
        const barge = local('agenthub-voice-bargein', 'false');
        // Voice by name (voice_gate.py): hands-free speech reaches an agent only when said TO it
        const named = local('agenthub-voice-named', 'true');
        const check = el('select');
        const checkNote = el('small', { class: 'settings-status', role: 'status', text: 'Loading models…' });
        (async () => {
            try {
                const [servers, cur] = await Promise.all([api('GET', '/api/admin/llms'), api('GET', '/api/voice/check-model')]);
                check.append(el('option', { value: '', text: 'None (named voice ignores everything)' }));
                const lists = await Promise.all(servers.map(x => api('POST', `/api/admin/llms/${encodeURIComponent(x.id)}/test`)
                    .then(r => r.models).catch(() => [])));
                servers.forEach((x, i) => {
                    let host = x.base_url;
                    try { host = new URL(x.base_url).host; } catch (_) { /* as typed */ }
                    lists[i].forEach(m => check.append(el('option', { value: `${x.id}|${m}`, text: `${x.name} (${host}) · ${m}` })));
                });
                const want = cur.server ? `${cur.server}|${cur.model}` : '';
                if (want && ![...check.options].some(o => o.value === want)) check.append(el('option', { value: want, text: `${cur.server} · ${cur.model} (unreachable)` }));
                check.value = want;
                checkNote.textContent = '';
            } catch (err) { checkNote.textContent = err.message; }
        })();
        check.addEventListener('change', async () => {
            const [server, model] = check.value ? check.value.split('|') : ['', ''];
            try { await api('POST', '/api/voice/check-model', { server, model }); checkNote.textContent = 'Saved'; }
            catch (err) { checkNote.textContent = err.message; }
        });
        const typing = el('select', {},
            el('option', { value: 'box', text: 'Type into the message box (Send is pressed by hand)' }),
            el('option', { value: 'auto', text: 'Type into the box and send when the talking stops' }),
            el('option', { value: 'live', text: 'Show live in the chat and send when the talking stops' }));
        try { typing.value = localStorage.getItem('agenthub-voice-typing') || 'box'; } catch (_) { /* private mode */ }
        typing.addEventListener('change', () => { try { localStorage.setItem('agenthub-voice-typing', typing.value); } catch (_) {} });
        const checkRow = field('Voice check model', check, { stack: true,
            hint: 'Decides whether a sentence was said to an agent. The models come from Providers > LLMs.' });
        checkRow.querySelector('.ui-field-control').append(checkNote);
        // Answer without the name (voice_gate.UNNAMED): saved on the server, for every device and browser
        const unnamed = el('input', { type: 'checkbox' });
        api('GET', '/api/voice/unnamed').then(r => { unnamed.checked = r.on; }, () => {});
        unnamed.addEventListener('change', async () => {
            try { unnamed.checked = (await api('POST', '/api/voice/unnamed', { on: unnamed.checked })).on; }
            catch (err) { unnamed.checked = !unnamed.checked; checkNote.textContent = err.message; }
        });
        // Passphrase for doors, locks and covers (passphrase.py): only a salted hash is kept
        const pass = el('input', { type: 'password', autocomplete: 'new-password', placeholder: 'Not set' });
        const passNote = el('small', { class: 'ui-field-hint', text: '' });
        const passSet = (on) => { pass.placeholder = on ? 'Set (type a new one to change it)' : 'Not set'; };
        api('GET', '/api/admin/voice-passphrase').then(r => passSet(r.set), err => { passNote.textContent = err.message; });
        const passRow = field('Passphrase or PIN', pass, { stack: true, hint: 'An agent asks for this before it opens, closes or unlocks part of the house (garage doors, gates, locks, blinds, alarms). Said within 30 s, the action runs; the words are never posted or kept. Saved empty: no passphrase.' });
        passRow.querySelector('.ui-field-control').append(btn('Save', async () => {
            try {
                const r = await api('POST', '/api/admin/voice-passphrase', { passphrase: pass.value });
                pass.value = '';
                passSet(r.set);
                passNote.textContent = r.set ? 'Saved' : 'Removed';
            } catch (err) { passNote.textContent = err.message; }
        }));
        passRow.querySelector('.ui-field-text').append(passNote);
        body.append(
            group('Hands-free talk', { id: 'settings-voice-talk' },
                field('Interrupt by talking over an agent', barge, { hint: 'Barge-in. Use with headphones: with speakers the microphone hears the agent. Off: the microphone pauses while an agent speaks. Stored in this browser.' }),
                field('Answer only when named', named, { hint: 'No wake word: an agent answers only when its name is said anywhere in the sentence, then follow-ups for 30 s without the name. Talking about an agent is ignored. Ignored speech is not posted or kept. Stored in this browser.' }),
                field('Answer without the name', unnamed, { hint: 'The voice check model also judges sentences with no name in them, and the agent with a voice answers the ones plainly meant for an assistant (a command for the house, a question asked of it). People talking to each other, about the agent, or the TV are ignored. Every sentence heard goes to the check model. Off: the name is needed to start.' }),
                checkRow),
            group('Mic button', { id: 'settings-voice-mic' },
                field('Voice typing', typing, { stack: true, hint: 'What the mic button next to the message box does. Stored in this browser.' })),
            group('Passphrase', { id: 'settings-voice-pass' }, passRow));
    }

    async function renderVoiceProviders(body) {
        heading(body, 'Speech providers', 'Services that read messages aloud. Any OpenAI-style /audio/speech service works.');
        const conf = await voiceConf(body);
        const rows = conf.providers.map(p => ({ ...p, parameters: { ...(p.parameters || {}) },
            tags: Array.isArray(p.tags) ? (p.tags.length ? p.tags.join(', ') : 'none') : '' }));
        const list = el('div', { class: 'settings-stack' });
        const fieldsOf = [['name', 'Name'], ['base_url', 'Base URL', 'Ends in /v1.'], ['model', 'Model'], ['format', 'Format'],
            ['voices_url', 'Voices list URL'], ['clone_url', 'Voice upload URL', 'For cloning voices.'], ['default_voice', 'Default voice'],
            ['sample_rate', 'Sample rate', 'For streaming.'], ['capabilities_url', 'Parameters URL', 'Blank: the service\'s openapi.json.'],
            ['tags', 'Emotion tags', 'Tags the engine performs inline, such as [laugh]. Agents with this voice are told to use them; they are spoken, not shown in chat. Unedited: the list the service reports. none: off.']];
        // Engine parameters: each service says which settings it takes; controls are drawn from that
        const caps = {};  // provider name -> promise of [{name, type, min, max, ...}]
        const paramControl = (spec, values) => {
            const set = (v) => { if (v === '' || v == null) delete values[spec.name]; else values[spec.name] = v; };
            const dflt = spec.default == null ? '' : ` (${spec.default})`;
            let input;
            if (spec.enum || spec.type === 'boolean') {
                const opts = spec.enum ? spec.enum.map(String) : ['true', 'false'];
                input = el('select', {}, el('option', { value: '', text: `Engine default${dflt}` }),
                    ...opts.map(o => el('option', { value: o, text: spec.enum ? o : (o === 'true' ? 'On' : 'Off') })));
                input.value = values[spec.name] == null ? '' : String(values[spec.name]);
                input.addEventListener('change', () => set(input.value));
            } else if (spec.type !== 'string' && spec.min != null && spec.max != null) {
                const out = el('output');
                const say = () => { out.textContent = values[spec.name] == null ? `default${dflt}` : String(values[spec.name]); };
                const range = el('input', { type: 'range', min: spec.min, max: spec.max,
                    step: spec.step || (spec.type === 'integer' ? 1 : 0.01), 'aria-label': spec.name });
                range.value = values[spec.name] ?? spec.default ?? spec.min;
                range.addEventListener('input', () => { set(Number(range.value)); say(); });
                say();
                input = el('div', { class: 'voice-param-range' }, range, out);
            } else {
                input = el('input', { type: spec.type === 'string' ? 'text' : 'number', step: spec.type === 'integer' ? 1 : 'any',
                    min: spec.min, max: spec.max, value: values[spec.name] ?? '', placeholder: `Engine default${dflt}` });
                input.addEventListener('input', () => set(input.value));
            }
            return field(spec.name, input, { hint: spec.description });
        };
        const drawParams = async (box, r) => {
            box.textContent = '';
            if (!conf.providers.some(p => p.name === r.name)) return;  // not saved yet: nothing to ask
            let specs;
            try {
                caps[r.name] = caps[r.name] || api('GET', '/api/voice/capabilities?provider=' + encodeURIComponent(r.name));
                specs = (await caps[r.name]).parameters;
            } catch (_) { return; }  // the service lists no parameters: nothing to set
            if (!specs.length) return;
            r.parameters = r.parameters || {};
            const adv = specs.filter(sp => sp.advanced);
            box.append(group('Engine parameters', { open: true, note: 'Sent with every request. Unset values use the engine default.' },
                ...specs.filter(sp => !sp.advanced).map(sp => paramControl(sp, r.parameters)),
                adv.length ? group('Advanced', { open: false }, ...adv.map(sp => paramControl(sp, r.parameters))) : null,
                actions(btn('Reset to defaults', () => { r.parameters = {}; drawParams(box, r); }))));
        };
        // A dropdown of known values; a saved value that is not in the list stays selectable
        const choice = (cur0, opts, blank, onpick) => {
            const cur = cur0 == null ? '' : String(cur0);
            const all = cur && !opts.includes(cur) ? [cur, ...opts] : opts;
            const sel = el('select', {}, ...(blank ? [el('option', { value: '', text: blank })] : []),
                ...all.map(o => el('option', { value: o, text: o })));
            sel.value = cur;
            sel.addEventListener('change', () => onpick(sel.value));
            return sel;
        };
        const FORMATS = ['mp3', 'wav', 'opus', 'flac', 'aac', 'pcm'];
        const RATES = ['16000', '22050', '24000', '44100', '48000'];
        const def = el('select');
        const openRows = new Set(rows.length === 1 ? rows : []);   // which provider groups are expanded
        const draw = () => {
            list.textContent = '';
            rows.forEach((r, i) => {
                const known = conf.voices[r.name] || {};
                const err = known.error;
                const count = known.voices?.length;
                const box = group(r.name || 'New provider', { open: openRows.has(r),
                    note: err ? 'Unreachable' : count != null ? `${count} voices` : '' });
                box.addEventListener('toggle', () => { if (box.open) openRows.add(r); else openRows.delete(r); });
                const lists = { format: [FORMATS, ''], sample_rate: [RATES, 'As sent by the service'],
                    default_voice: [known.voices || [], 'None'], model: [known.models || [], 'None'] };
                for (const [k, label, hint] of fieldsOf) {
                    const [opts, blank] = lists[k] || [[], ''];
                    let inp;
                    if (opts.length) {  // the service (or the format list) says what can be chosen
                        inp = choice(r[k] || (k === 'format' ? 'mp3' : ''), opts, blank, (v) => { r[k] = v; });
                    } else {            // URLs and names; also voice and model while the service cannot be asked
                        // Emotion tags: with no list of its own, the field shows what the service reports
                        const live = k === 'tags' && !r.tags ? ((conf.tags_live || {})[r.name] || []).join(', ') : '';
                        inp = el('input', { value: r[k] || live });
                        inp.addEventListener('input', () => { r[k] = inp.value; });
                    }
                    box.body.append(field(label, inp, { hint, stack: k === 'tags' }));
                }
                const st = el('input', { type: 'checkbox', checked: !!r.stream });
                st.addEventListener('change', () => { r.stream = st.checked; });
                const key = el('input', { type: 'password', placeholder: r.has_key ? 'Stored. Leave blank to keep it.' : 'None', autocomplete: 'off' });
                key.addEventListener('input', () => { r.api_key = key.value; });
                const params = el('div');
                drawParams(params, r);
                box.body.append(
                    field('Streams raw PCM', st, { hint: 'Speech starts on the first chunk.' }),
                    field('API key', key, { hint: 'Write-only.' }),
                    err ? el('p', { class: 'settings-warn', text: `Unreachable: ${err}` }) : '',   // '' not null: append() would print "null"
                    params,
                    actions(btn('Remove provider', () => { rows.splice(i, 1); draw(); }, 'danger')));
                list.append(box);
            });
            if (!rows.length) list.append(empty('No speech providers', 'A provider is a text-to-speech service that turns agent messages into audio.',
                btn('Add provider', addRow, 'primary')));
            def.textContent = '';
            def.append(...rows.filter(r => r.name).map(r => el('option', { value: r.name, text: r.name })));
            def.value = conf.default_provider;
        };
        const addRow = () => { const r = { name: '', base_url: '', format: 'mp3' }; rows.push(r); openRows.add(r); draw(); };
        draw();
        const cacheMb = el('input', { type: 'number', min: '0', max: '100000', value: conf.cache_mb ?? 512 });
        // Tags added without the agent choosing one (voice.rule_tag)
        const tr = conf.tag_rules || {};
        const everyTag = [...new Set(Object.values(conf.tags_live || {}).flat())];
        const tagPick = (cur) => choice(cur || '', everyTag, 'None', () => {});
        const trFailed = tagPick(tr.failed), trQuiet = tagPick(tr.quiet);
        const hour = (v) => el('input', { type: 'number', min: '0', max: '23', value: v, 'aria-label': 'Hour' });
        const qFrom = hour(tr.quiet_from ?? 22), qTo = hour(tr.quiet_to ?? 7);
        const rulesGroup = group('Emotion rules', { id: 'settings-voice-tagrules', open: false, note: 'Tags added when the agent chose none' },
            field('After a failed action', trFailed, { hint: 'Put in front of a reply whose last tool call failed.' }),
            field('In quiet hours', trQuiet, { hint: 'Put in front of every reply during the hours below.' }),
            field('Quiet hours', el('div', { class: 'ui-actions' }, qFrom, el('span', { text: 'to' }), qTo), { hint: 'Hours of the day, 0 to 23, server time.' }));
        body.append(
            card(field('Default provider', def, { hint: 'Used by agents that have not picked one.' }),
                field('Saved audio (MB)', cacheMb, { hint: 'Sentences read aloud are kept on the server, so a replay plays at once. The oldest are removed when this is full. 0 turns it off.' })),
            list,
            rulesGroup,
            actions(btn('Add provider', addRow),
                btn('Save speech providers', guard(async () => {
                    await saveVoice(conf, { providers: rows, default_provider: def.value, cache_mb: cacheMb.value,
                        tag_rules: { failed: trFailed.value, quiet: trQuiet.value, quiet_from: qFrom.value, quiet_to: qTo.value } });
                    refresh('voice-providers');
                }), 'primary')));
    }

    async function renderVoiceStt(body) {
        heading(body, 'Speech to text', 'The server that turns speech into text for voice typing and hands-free talk.');
        const conf = await voiceConf(body);
        const stt = el('input', { value: conf.stt.wyoming, placeholder: 'host:port' });
        const langName = new Intl.DisplayNames(['en'], { type: 'language' });
        const lang = el('select', {}, ...[...new Set([conf.stt.language || 'en', 'en', 'es', 'fr', 'de', 'it',
            'pt', 'nl', 'sv', 'da', 'no', 'fi', 'pl', 'cs', 'uk', 'ru', 'tr', 'ar', 'he', 'hi', 'ja', 'ko', 'zh'])]
            .map(c => el('option', { value: c, text: `${langName.of(c)} (${c})` })));
        lang.value = conf.stt.language || 'en';
        const status = el('small', { class: 'settings-status', role: 'status' });
        body.append(
            card(field('Server', stt, { hint: 'Address of a Wyoming Whisper server.' }),
                field('Language', lang, { hint: 'The language spoken to the room.' })),
            actions(btn('Save speech to text', guard(async () => {
                await saveVoice(conf, { stt: { wyoming: stt.value.trim(), language: lang.value.trim() } });
                conf.stt = { wyoming: stt.value.trim(), language: lang.value.trim() };
                status.textContent = 'Saved';
            }), 'primary'), status));
    }

    // Always-listening devices: voice_devices.py, /voice-devices.txt
    async function renderVoiceDevices(body, newKey) {
        heading(body, 'Devices', 'Always-listening devices send what they hear to the room and speak the replies, under the "answer only when named" rule.');
        const [devices, channels] = await load(body, Promise.all([api('GET', '/api/admin/voice-devices'),
            api('GET', '/api/settings').then(s => s.channels || []).catch(() => [])]));
        if (newKey) body.append(secretBox(`Key for ${newKey.name}`, newKey.key,
            'Put this key in the device\'s configuration. It is not shown again.'));
        const chanSelect = (cur) => {
            const sel = el('select', { 'aria-label': 'Channel' });
            const all = [...new Set([...channels, cur || 'home'])];
            all.forEach(c => sel.append(el('option', { value: c, text: '#' + c })));
            sel.value = cur || 'home';
            return sel;
        };
        const ago = (t) => {
            if (!t) return 'never';
            const s = Math.max(0, Math.round(Date.now() / 1000 - t));
            return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : s < 86400 ? `${Math.round(s / 3600)}h ago` : `${Math.round(s / 86400)}d ago`;
        };
        const again = (key) => { if (current === 'voice-devices') mount(b => renderVoiceDevices(b, key)); };
        const name = el('input', { placeholder: 'e.g. office-dot' });
        const chan = chanSelect('home');
        const form = el('form', {},
            field('Name', name),
            field('Channel', chan, { hint: 'The device posts into this channel. Add the agents it should reach to that channel.' }),
            actions(el('button', { type: 'submit', class: 'ui-btn primary', text: 'Add device' })));
        form.addEventListener('submit', guard(async (ev) => {
            ev.preventDefault();
            const r = await api('POST', '/api/admin/voice-devices', { name: name.value.trim().toLowerCase(), channel: chan.value });
            again({ name: r.device.name, key: r.key });
        }));
        // Dots on the network (voice_dots.py): found by mDNS or address, adopted, set up as devices
        const dotBox = el('div');
        const dotHost = el('input', { placeholder: 'IP address (optional)', 'aria-label': 'Dot address' });
        const dotNote = el('small', { class: 'ui-field-hint', text: '' });
        let hubURL = '';
        const dotAction = async (label, fn) => {
            dotNote.textContent = label + '…';
            try { await fn(); dotNote.textContent = ''; findDots(); }
            catch (err) { dotNote.textContent = err.message; }
        };
        async function findDots(host) {
            dotNote.textContent = 'Looking…';
            dotBox.textContent = '';
            let dots = [];
            try {
                const r = await api('GET', '/api/admin/voice-dots');
                dots = r.dots; hubURL = r.hub;
                if (host) {
                    const p = await api('POST', '/api/admin/voice-dots/probe', { host });
                    if (!dots.some(d => d.mac && d.mac === p.mac)) dots.push(p);
                }
                dotNote.textContent = dots.length ? '' : 'No Dots found.';
            } catch (err) { dotNote.textContent = err.message; return; }
            if (!dots.length) return;
            dotBox.append(table(['Dot', 'Address', 'State', ''], dots.map(d => {
                const acts = el('span', { class: 'ui-actions' });
                if (d.state === 'ready' || d.state === 'open') acts.append(btn('Adopt', () => {
                    if (!confirm(`Adopt ${d.name}? It is taken from whatever had it (Home Assistant loses it).`)) return;
                    dotAction('Adopting', () => api('POST', '/api/admin/voice-dots/adopt', { host: d.host, port: d.port }));
                }, 'sm'));
                if (d.state === 'owned') acts.append(btn('Use its key', () => {
                    const key = prompt(`${d.name}'s API encryption key (Home Assistant: Settings > Devices > ESPHome > the Dot > Configure):`);
                    if (key) dotAction('Checking the key', () => api('POST', '/api/admin/voice-dots/share', { host: d.host, port: d.port, key: key.trim() }));
                }, 'sm'));
                if (d.state === 'adopted') {
                    acts.append(btn('Set up', () => {
                        const channel = prompt('Channel for this Dot (its room):', d.channel || 'home');
                        if (!channel) return;
                        const hub = prompt('This AgentHub\'s address, as the Dot reaches it:', hubURL);
                        if (!hub) return;
                        const listen = confirm('Also listen without a wake word? (OK: yes, Cancel: no)');
                        dotAction('Setting up', async () => {
                            const r = await api('POST', `/api/admin/voice-dots/${encodeURIComponent(d.mac)}/configure`,
                                { hub, channel: channel.trim().toLowerCase(), listen, brain: 'AgentHub' });
                            if (!r.ok) throw new Error('Set up, but the Dot did not read back every setting: ' + JSON.stringify(r.dot));
                            again();
                        });
                    }, 'sm'));
                    acts.append(btn('Forget', () => {
                        if (confirm(`Forget ${d.name}? AgentHub drops its key; the Dot keeps its settings.`))
                            dotAction('Forgetting', () => api('DELETE', `/api/admin/voice-dots/${encodeURIComponent(d.mac)}`));
                    }, 'danger sm'));
                }
                return [d.name, d.host ? `${d.host}:${d.port}` : '', d.state + (d.device ? ` (#${d.channel})` : ''), acts];
            })));
        }
        const dotsGroup = group('Dots on the network', { id: 'settings-voice-dots', open: false },
            el('p', { class: 'settings-note' }, 'TECHO5 Echo Dots found on the network. Ready: waiting to be adopted (open its adoption window on the Dot first). Owned: it already has a key, usually from Home Assistant; enter that key to share it. Adopted: set it up as a voice device here.'),
            actions(btn('Find Dots', () => findDots()), dotHost, btn('Check address', () => findDots(dotHost.value.trim())), dotNote),
            dotBox);
        const add = group('New device', { open: false }, form);
        body.append(
            group('Devices', { id: 'settings-voice-devices', count: devices.length },
                devices.length
                    ? table(['Name', 'Channel', 'Last heard', ''], devices.map(d => {
                        const sel = chanSelect(d.channel);
                        sel.addEventListener('change', guard(() =>
                            api('PATCH', `/api/admin/voice-devices/${encodeURIComponent(d.name)}`, { channel: sel.value })));
                        return [d.name, sel, ago(d.last_seen), btn('Remove', guard(async () => {
                            if (!confirm(`Remove device "${d.name}"? Its key stops working.`)) return;
                            await api('DELETE', `/api/admin/voice-devices/${encodeURIComponent(d.name)}`);
                            again();
                        }), 'danger sm')];
                    }))
                    : empty('No devices', 'A device is a speaker with a microphone that listens all the time. Each one gets its own key and channel.',
                        btn('Add device', () => { add.open = true; name.focus(); }, 'primary'))),
            add,
            dotsGroup,
            el('p', { class: 'settings-note' }, 'Protocol for device firmware: ',
                el('a', { href: '/voice-devices.txt', target: '_blank', rel: 'noopener', text: '/voice-devices.txt' }), '.'));
    }

    // Clone a voice from a short sample
    async function renderVoiceClone(body) {
        heading(body, 'Cloned voices', 'Add a voice to a speech provider from a short recording.');
        const conf = await voiceConf(body);
        const cloneable = conf.providers.filter(p => (conf.voices[p.name] || {}).clone);
        if (!cloneable.length) {
            body.append(empty('No provider can clone voices', 'Cloning needs a speech provider with a voice upload URL that is reachable.',
                btn('Open speech providers', () => show('voice-providers'))));
            return;
        }
        const cp = el('select', {}, ...cloneable.map(p => el('option', { value: p.name, text: p.name })));
        const cn = el('input', { placeholder: 'e.g. Narrator' });
        const cf = el('input', { type: 'file', accept: 'audio/*' });
        const go = btn('Clone voice', null, 'primary');
        const status = el('small', { class: 'settings-status', role: 'status' });
        go.onclick = async () => {
            if (!cf.files[0] || !cn.value.trim()) { status.textContent = 'Give the voice a name and pick a sample.'; return; }
            const form = new FormData();
            form.append('provider', cp.value); form.append('name', cn.value.trim()); form.append('file', cf.files[0]);
            go.disabled = true; status.textContent = 'Uploading…';
            try {
                const res = await fetch('/api/voice/clone', { method: 'POST', headers: { 'X-Session-Token': window.SESSION_TOKEN }, body: form });
                const data = await res.json();
                if (!res.ok) throw new Error(data.error || 'cloning failed');
                status.textContent = `Added "${data.name}". It can now be picked on an agent's page.`;
            } catch (err) { status.textContent = err.message; } finally { go.disabled = false; }
        };
        body.append(
            card(field('Provider', cp),
                field('Voice name', cn),
                field('Sample', cf, { hint: '10 to 30 seconds of one person speaking clearly, without music or background noise. Only clone voices with permission to use them.' })),
            actions(go, status));
    }

    // ------------------------------------------------------------ Rulebooks
    async function renderRulebooks(body, editing) {
        const e = editing || null;
        heading(body, e ? (e.name ? `Edit "${e.name}"` : 'New rulebook') : 'Rulebooks',
            'Guides for kinds of work, such as story writing, code or docs. Agents open one only when asked, so unused rulebooks cost no context.');
        const [books, agents, access] = await load(body, Promise.all([api('GET', '/api/admin/rulebooks'), api('GET', '/api/managed-agents'), api('GET', '/api/admin/rulebook-access')]));
        const fine = access.fine_grained;
        const channels = (typeof channelList !== 'undefined' && channelList.length) ? channelList : ['general'];
        if (e) {
            const name = el('input', { value: e.name || '', placeholder: 'e.g. Story writing' });
            const desc = el('input', { value: e.description || '', placeholder: 'One line: what it is for' });
            const text = el('textarea', { rows: 14, placeholder: 'The rules, one per line or in short paragraphs.' });
            text.value = e.text || '';
            const names = agents.map(a => a.name);
            const chans = checks(channels, e.channels, '#');
            const ags = checks(names, e.agents, '@');
            const rds = checks(names, e.readers, '@');
            const form = el('form', {},
                field('Name', name),
                field('Description', desc),
                field('Rules', text, { stack: true }),
                field('Follow it in these channels', chans.node, { stack: true, hint: 'Agents in these channels follow it every turn.' }),
                field('Agents that always follow it', ags.node, { stack: true }),
                fine ? field('Agents that can open it on request', rds.node, { stack: true, hint: 'Fine-grained access is on.' }) : null,
                actions(btn('Cancel', () => show('rulebooks')),
                    el('button', { type: 'submit', class: 'ui-btn primary', text: 'Save rulebook' })));
            form.addEventListener('submit', guard(async (ev) => {
                ev.preventDefault();
                await api('POST', '/api/admin/rulebooks', { original: e.name || null, name: name.value.trim(),
                    description: desc.value.trim(), text: text.value,
                    channels: chans.values(), agents: ags.values(), readers: rds.values() });
                refresh('rulebooks');
            }));
            body.append(card(form));
            name.focus();
            return;
        }
        const create = () => mount(b => renderRulebooks(b, {}));
        const fineBox = el('input', { type: 'checkbox', checked: fine, onchange: guard(async () => {
            await api('POST', '/api/admin/rulebook-access', { fine_grained: fineBox.checked });
            refresh('rulebooks');
        }) });
        body.append(
            group('Rulebooks', { id: 'settings-rulebooks', count: books.length },
                books.length
                    ? table(['Rulebook', 'Applies to', ''], books.map(b => [
                        el('div', {}, el('strong', { text: b.name }), el('div', { class: 'settings-cell-sub', text: b.description || '' })),
                        ([...(b.channels || []).map(c => '#' + c), ...(b.agents || []).map(a => '@' + a)].join(' ') || 'on request only')
                            + (fine ? ' · can open: ' + ((b.readers || []).map(a => '@' + a).join(' ') || 'attached only') : ''),
                        actions(
                            btn('Edit', () => mount(p => renderRulebooks(p, b)), 'sm'),
                            btn('Delete', guard(async () => {
                                if (!confirm(`Delete rulebook "${b.name}"?`)) return;
                                await api('DELETE', `/api/admin/rulebooks/${encodeURIComponent(b.name)}`);
                                refresh('rulebooks');
                            }), 'danger sm'))]))
                    : empty('No rulebooks', 'A rulebook is a set of rules for one kind of work that agents read on request or follow every turn.',
                        btn('New rulebook', create, 'primary')),
                books.length ? actions(btn('New rulebook', create, 'primary')) : null),
            group('Access', { open: fine },
                field('Fine-grained access', fineBox, { hint: 'On: agents can open only rulebooks attached to them or their channel, or that list them as a reader. Off: every agent can open every rulebook.' })),
            group('How to use', { id: 'settings-rulebooks-how', open: false },
                field('Open on request', value('"use the story rulebook"'), { hint: 'Say it in a message. The agent opens that rulebook for the task.' }),
                field('Follow every turn', value('Attach to channels or agents'), { hint: 'Edit a rulebook and tick the channels or agents.' }),
                field('Ask for a review', value('/review <rulebook> @agent <text or file path>'), { stack: true })));
    }

    // ------------------------------------------------------------ LLM servers
    async function renderLlms(body, editing) {
        const e = editing || { api_format: 'openai' };
        heading(body, e.id ? `Edit ${e.name}` : 'LLMs',
            'The LLM servers that API agents use. Each is set up once, with its key and a default model. Keys are write-only.');
        const servers = await load(body, api('GET', '/api/admin/llms'));
        const presets = (window.AgentsSection && window.AgentsSection.PROVIDERS) || {};
        const preset = el('select');
        preset.append(el('option', { value: '', text: 'Choose a provider…' }));
        for (const [k, p] of Object.entries(presets)) preset.append(el('option', { value: k, text: p.label }));
        const name = el('input', { value: e.name || '', placeholder: 'e.g. Local router' });
        const baseUrl = el('input', { value: e.base_url || '', placeholder: 'http://127.0.0.1:4000/v1' });
        const format = el('select');
        format.append(el('option', { value: 'openai', text: 'OpenAI-compatible' }), el('option', { value: 'anthropic', text: 'Anthropic' }));
        format.value = e.api_format || 'openai';
        const key = el('input', { type: 'password', autocomplete: 'off',
            placeholder: e.has_api_key ? 'Stored. Leave blank to keep it.' : 'Blank for local servers' });
        const def = el('select');
        const defNote = el('small', { class: 'settings-status', role: 'status' });
        const fillDefaults = (models, cur) => {
            def.textContent = '';
            def.append(el('option', { value: '', text: 'None (agents must pin a model)' }));
            const all = cur && !models.includes(cur) ? [cur, ...models] : models;
            all.forEach(m => def.append(el('option', { value: m, text: m })));
            def.value = cur || '';
        };
        fillDefaults([], e.default_model || '');
        if (e.id) {
            defNote.textContent = 'Loading models…';
            api('POST', `/api/admin/llms/${encodeURIComponent(e.id)}/test`)
                .then(r => { fillDefaults(r.models, r.default_model || e.default_model || ''); defNote.textContent = `${r.models.length} models`; })
                .catch(err => { defNote.textContent = err.message; });
        }
        preset.addEventListener('change', () => {
            const p = presets[preset.value];
            if (!p) return;
            if (p.base_url) baseUrl.value = p.base_url;
            format.value = p.api_format;
            if (!name.value) name.value = p.label;
        });
        const defRow = field('Default model', def, { stack: true,
            hint: e.id ? 'Used by agents that pin no model. A server with a single model uses that one.'
                       : 'Save the server first, then edit it to pick from its models.' });
        defRow.querySelector('.ui-field-control').append(defNote);
        if (!e.id) def.disabled = true;
        const form = el('form', {},
            e.id ? null : field('Start from', preset, { hint: 'Fills the address and format of a known provider.' }),
            field('Name', name),
            field('Base URL', baseUrl),
            field('API format', format),
            field('API key', key, { hint: 'Write-only. Changing the host without a new key clears the stored one.' }),
            defRow,
            actions(e.id ? btn('Cancel', () => show('llms')) : null,
                el('button', { type: 'submit', class: 'ui-btn primary', text: 'Save server' })));
        form.addEventListener('submit', guard(async (ev) => {
            ev.preventDefault();
            const payload = { id: e.id || '', name: name.value.trim(), base_url: baseUrl.value.trim(),
                api_format: format.value, api_key: key.value };
            // only when changed: an untouched default keeps following a one-model server
            if (e.id && def.value !== (e.default_model || '')) payload.default_model = def.value;
            await api('POST', '/api/admin/llms', payload);
            refresh('llms');
        }));
        if (e.id) { body.append(card(form)); return; }
        const add = group('New LLM server', { open: false }, form);
        body.append(
            group('Servers', { id: 'settings-llms', count: servers.length },
                servers.length
                    ? table(['Name', 'Host', 'Format', 'Default model', 'Models', 'Agents', ''], servers.map(x => {
                        const count = el('span', { text: 'checking…' });
                        const defCell = el('span', { text: x.default_model || 'none' });
                        api('POST', `/api/admin/llms/${encodeURIComponent(x.id)}/test`)
                            .then(r => { count.textContent = String(r.models.length); defCell.textContent = r.default_model || 'none'; })
                            .catch(() => { count.textContent = 'unreachable'; });
                        let host = x.base_url;
                        try { host = new URL(x.base_url).host; } catch (_) { /* keep as typed */ }
                        const linked = (x.agents || []).length;
                        const names = (x.agents || []).map(n => '@' + n).join(', ');
                        const who = el('span', { text: names ? String(linked) : '—', title: names });
                        const acts = x.readonly ? el('small', { class: 'settings-status', text: 'from config.toml' }) : actions(
                            btn('Edit', () => mount(b => renderLlms(b, x)), 'sm'),
                            linked ? btn('Unlink agents', guard(async () => {
                                if (!confirm(`Unlink ${linked} agents from ${x.name}? They go offline until each is edited and given a server.`)) return;
                                await api('POST', `/api/admin/llms/${encodeURIComponent(x.id)}/unlink`);
                                refresh('llms');
                            }), 'sm') : null,
                            el('button', { type: 'button', class: 'ui-btn danger sm', text: linked ? `Remove (${linked} linked)` : 'Remove',
                                disabled: !!linked, title: linked ? 'Unlink its agents first' : null, onclick: guard(async () => {
                                    if (!confirm(`Remove LLM server "${x.name}"?`)) return;
                                    await api('DELETE', `/api/admin/llms/${encodeURIComponent(x.id)}`);
                                    refresh('llms');
                                }) }));
                        return [x.name, host, x.api_format === 'anthropic' ? 'Anthropic' : 'OpenAI', defCell, count, who, acts];
                    }))
                    : empty('No LLM servers', 'An LLM server is an API endpoint, such as OpenRouter or a local router, that API agents send their requests to.',
                        btn('Add a server', () => { add.open = true; name.focus(); }, 'primary'))),
            add);
    }

    // ------------------------------------------------------------ MCP servers
    async function renderMcp(body, editing) {
        const e = editing || { transport: 'http', enabled: true };
        heading(body, e.name ? `Edit ${e.name}` : 'MCP servers',
            'Tool servers for agents. Each server is offered only to the agents ticked for it.');
        const [servers, agents] = await load(body, Promise.all([api('GET', '/api/admin/mcp'), api('GET', '/api/managed-agents')]));
        const name = el('input', { value: e.name || '', placeholder: 'e.g. netlab', readonly: !!e.name });
        const transport = el('select');
        transport.append(el('option', { value: 'http', text: 'HTTP (URL)' }), el('option', { value: 'stdio', text: 'Local command (stdio)' }));
        transport.value = e.transport || 'http';
        const url = el('input', { value: e.url || '', placeholder: 'http://192.168.1.50:8097/mcp' });
        const headers = el('textarea', { rows: 2, placeholder: (e.header_names || []).length
            ? (e.header_names.map(h => h + ':  (stored; leave the value blank to keep it)').join('\n'))
            : 'Authorization: Bearer ...  (one per line, optional)' });
        headers.value = (e.header_names || []).map(h => h + ': ').join('\n');
        const command = el('input', { value: e.command || '', placeholder: 'npx' });
        const args = el('input', { value: (e.args || []).join(' '), placeholder: '-y @modelcontextprotocol/server-filesystem /data/projects' });
        const env = el('textarea', { rows: 2, placeholder: 'KEY=value (one per line, optional)' });
        env.value = (e.env_names || []).map(k => k + '=').join('\n');
        const httpBox = el('div', { class: 'settings-rows' },
            field('URL', url),
            field('Headers', headers, { stack: true, hint: 'One per line. Values are write-only.' }));
        const stdioBox = el('div', { class: 'settings-rows' },
            field('Command', command),
            field('Arguments', args, { stack: true, hint: 'Separated by spaces.' }),
            field('Environment', env, { stack: true, hint: 'One KEY=value per line. Values are write-only.' }));
        const timeout = el('input', { type: 'number', min: '1', max: '3600', value: e.timeout || '', placeholder: '120' });
        const sync = () => { httpBox.hidden = transport.value !== 'http'; stdioBox.hidden = transport.value !== 'stdio'; };
        transport.addEventListener('change', sync);
        sync();
        const allowed = agents.filter(a => a.kind !== 'agy').map(a => a.name);
        const grants = checks(allowed, e.grants, '@');
        const form = el('form', {},
            field('Name', name),
            field('Type', transport),
            httpBox, stdioBox,
            field('Tool timeout (seconds)', timeout, { hint: 'How long one tool call may run.' }),
            field('Agents allowed to use it', allowed.length ? grants.node
                : note('No Claude Code or API agents yet. Add one under Agents > Room agents.'),
                { stack: true, hint: 'API agents get the tools through AgentHub. Claude Code agents get the server in their MCP config. Antigravity keeps its own global MCP settings.' }),
            actions(e.name ? btn('Cancel', () => show('mcp')) : null,
                el('button', { type: 'submit', class: 'ui-btn primary', text: 'Save server' })));
        form.addEventListener('submit', guard(async (ev) => {
            ev.preventDefault();
            await api('POST', '/api/admin/mcp', { name: name.value.trim(), transport: transport.value,
                url: url.value.trim(), headers: headers.value, command: command.value.trim(),
                args: args.value.trim() ? args.value.trim().split(/\s+/) : [], env: env.value,
                grants: grants.values(), enabled: true,
                timeout: timeout.value ? Number(timeout.value) : null });
            refresh('mcp');
        }));
        if (e.name) { body.append(card(form)); return; }
        // OAuth sign-in: the provider only redirects to 127.0.0.1, so the browser lands on a
        // dead page whose address holds the code; the user pastes that address back here.
        const oauthSlot = el('div');
        async function oauthBox(x) {
            oauthSlot.textContent = '';
            const status = el('p', { class: 'settings-note', role: 'status', text: 'Contacting the server…' });
            const box = group(`Sign in to ${x.name}`, { open: true }, status);
            oauthSlot.append(box);
            box.scrollIntoView({ block: 'nearest' });
            let link;
            try { link = (await api('POST', `/api/admin/mcp/${encodeURIComponent(x.name)}/oauth/start`)).url; }
            catch (err) { status.textContent = `${err.message}. Check the server address, then choose Sign in again.`; return; }
            const landed = el('input', { placeholder: 'http://127.0.0.1:8765/callback?code=…&state=…' });
            const done = el('button', { type: 'submit', class: 'ui-btn primary', text: 'Finish sign-in' });
            const step2 = el('form', {},
                field('2. Paste the address the browser landed on', landed, { stack: true,
                    hint: 'After approval the new tab shows "can\'t connect" at 127.0.0.1. Copy that whole address.' }),
                actions(btn('Cancel', () => { oauthSlot.textContent = ''; }), done));
            step2.addEventListener('submit', async (ev) => {
                ev.preventDefault();
                done.disabled = true;
                try {
                    const r = await api('POST', `/api/admin/mcp/${encodeURIComponent(x.name)}/oauth/finish`, { url: landed.value.trim() });
                    alert(r.message);
                    refresh('mcp');
                } catch (err) { status.textContent = err.message; done.disabled = false; }
            });
            status.textContent = '';
            status.append('1. ', el('a', { href: link, target: '_blank', rel: 'noopener noreferrer',
                text: 'Open the sign-in page in a new tab' }), ' and approve access.');
            box.body.append(step2);
        }
        const add = group('New MCP server', { open: false }, form);
        body.append(
            group('Servers', { id: 'settings-mcp', count: servers.length },
                servers.length
                    ? table(['Name', 'Where', 'Allowed agents', 'Tools', ''], servers.map(x => {
                        const tools = el('span', { text: '—' });
                        return [x.name, x.transport === 'stdio' ? `${x.command} ${(x.args || []).join(' ')}` : x.url,
                            (x.grants || []).map(g => '@' + g).join(', ') || 'nobody', tools,
                            actions(
                                btn('Test', async () => {
                                    tools.textContent = 'connecting…';
                                    try {
                                        const r = await api('POST', `/api/admin/mcp/${encodeURIComponent(x.name)}/test`);
                                        tools.textContent = `${r.tools.length}: ${r.tools.slice(0, 8).join(', ')}${r.tools.length > 8 ? ', …' : ''}`;
                                    } catch (err) { tools.textContent = err.message; }
                                }, 'sm'),
                                x.transport === 'stdio' ? null : btn(x.oauth ? 'Sign in again' : 'Sign in (OAuth)', () => oauthBox(x), 'sm'),
                                btn('Edit', () => mount(b => renderMcp(b, x)), 'sm'),
                                btn('Delete', guard(async () => {
                                    if (!confirm(`Delete MCP server "${x.name}"?`)) return;
                                    await api('DELETE', `/api/admin/mcp/${encodeURIComponent(x.name)}`);
                                    refresh('mcp');
                                }), 'danger sm'))];
                    }))
                    : empty('No MCP servers', 'An MCP server gives agents extra tools, such as file access, a browser or an outside API.',
                        btn('Add a server', () => { add.open = true; name.focus(); }, 'primary'))),
            oauthSlot, add);
    }

    // ------------------------------------------------------------ Usage
    async function renderMetrics(body, hours = 24) {
        heading(body, 'Usage', 'Every model call by API agents: tokens, prompt cache hits, speed and cost.');
        const again = (h) => { if (current === 'metrics') mount(b => renderMetrics(b, h)); };
        const pick = el('select', { 'aria-label': 'Period', onchange: () => again(+pick.value) });
        [[1, 'Last hour'], [24, 'Last 24 hours'], [168, 'Last 7 days'], [720, 'Last 30 days']].forEach(([h, t]) =>
            pick.append(el('option', { value: String(h), text: t, selected: h === hours })));
        body.append(actions(pick, btn('Refresh', () => again(hours))));
        const m = await load(body, api('GET', `/api/admin/metrics?hours=${hours}`));
        const n = (x) => (x == null ? '—' : Number(x).toLocaleString());
        if (!m.rows.length) {
            body.append(empty('No model calls in this period', 'Calls by API agents are recorded here as they happen. Pick a longer period to look further back.'));
            return;
        }
        body.append(
            group('By agent and model', { id: 'settings-usage-totals', note: 'Tokens/s counts the whole call, including the time spent reading the prompt.' },
                table(['Agent', 'Model', 'Calls', 'Prompt tokens', 'Cached', 'Cache hit', 'Output tokens', 'Tokens/s', 'Avg call', 'Cost'],
                    m.rows.map(r => [r.agent, r.model, n(r.calls), n(r.prompt_tokens), n(r.cached_tokens), `${r.cache_hit_pct}%`,
                        n(r.completion_tokens), r.tokens_per_s == null ? '—' : String(r.tokens_per_s), `${r.avg_seconds} s`,
                        r.cost == null ? '—' : `$${r.cost.toFixed(4)}`]))),
            group('Latest calls', { id: 'settings-usage-latest', count: m.recent.length },
                table(['Time', 'Agent', 'Channel', 'Prompt', 'Cached', 'Output', 'Seconds', 'Tokens/s', 'Cost'],
                    m.recent.map(r => [new Date(r.ts * 1000).toLocaleTimeString(), r.agent, r.channel ? '#' + r.channel : '',
                        n(r.prompt_tokens), n(r.cached_tokens), n(r.completion_tokens), String(r.seconds),
                        r.tokens_per_s == null ? '—' : String(r.tokens_per_s), r.cost == null ? '—' : `$${Number(r.cost).toFixed(4)}`]))));
    }

    // ------------------------------------------------------------ IRC
    async function renderIrc(body) {
        heading(body, 'IRC', 'Use the room from any IRC client. Channels are #channels and agents are nicks; a working agent shows as away.');
        const irc = await load(body, api('GET', '/api/admin/irc'));
        if (!irc.enabled) {
            body.append(empty('The IRC gateway is off', 'Enable it under [irc] in config.local.toml, then restart the server.'));
            return;
        }
        const pw = el('input', { type: 'password', readonly: true, value: irc.password, class: 'settings-copy-input' });
        const reveal = btn('Show', () => {
            pw.type = pw.type === 'password' ? 'text' : 'password';
            reveal.textContent = pw.type === 'password' ? 'Show' : 'Hide';
        });
        const pwRow = field('Server password', pw);
        pwRow.querySelector('.ui-field-control').append(reveal);
        body.append(
            card(field('Server', value(irc.host)), field('Port', value(String(irc.port))),
                field('TLS', value('Off'), { hint: 'The connection is plain text: use it on a trusted network only.' }),
                pwRow),
            actions(btn('Generate new password', guard(async () => {
                if (!confirm('Generate a new IRC password? Connected clients keep working until they reconnect.')) return;
                await api('POST', '/api/admin/irc/password', {});
                refresh('irc');
            }))));
    }

    // ------------------------------------------------------------ Discord bots (discord_gateway.py)
    async function renderDiscord(body) {
        heading(body, 'Discord bots', 'Each bot links a Discord application to one room agent. Every Discord server it is in becomes a channel group named after the server, with a channel for each Discord channel it joined. The agent sees the whole server, answers when tagged, and only in the channel it was tagged in. Your messages in those channels go to Discord as the bot.');
        const [data, agents] = await load(body, Promise.all([api('GET', '/api/admin/discord'),
            api('GET', '/api/managed-agents').catch(() => [])]));
        const pick = (list, cur, label) => {
            const sel = el('select', { 'aria-label': label });
            [...new Set([...list, ...(cur ? [cur] : [])])].forEach(v => sel.append(el('option', { value: v, text: v })));
            if (cur) sel.value = cur;
            return sel;
        };
        const agentNames = agents.map(a => a.name);
        const STATE = { online: ['Online', 'ok'], connecting: ['Connecting', 'accent'], error: ['Error', 'danger'], stopped: ['Stopped', ''] };
        const form = (b) => {
            const name = el('input', { value: b ? b.id : '', placeholder: 'e.g. hazel', readonly: !!b });
            const token = el('input', { type: 'password', autocomplete: 'new-password',
                placeholder: b && b.has_token ? 'Stored. Leave blank to keep it.' : 'Bot token from the Developer Portal' });
            const agent = pick(agentNames, b && b.agent, 'Agent');
            const guilds = el('textarea', { rows: 2, placeholder: 'Server ids, one per line', text: b ? b.guilds.join('\n') : '' });
            const voiceOn = el('input', { type: 'checkbox', checked: !!(b && b.voice) });
            const holdOn = el('input', { type: 'checkbox', checked: !b || b.hold !== false });
            const followS = el('input', { type: 'number', min: '0', max: '600', value: String(b ? (b.follow_s ?? 60) : 60) });
            const note = el('p', { class: 'settings-note', role: 'status' });
            const save = guard(async () => {
                await api('POST', '/api/admin/discord', { id: name.value.trim().toLowerCase(), token: token.value,
                    agent: agent.value, guilds: guilds.value, voice: voiceOn.checked, hold: holdOn.checked, follow_s: Number(followS.value) || 0 });
                refresh('discord');
            });
            return el('div', {},
                field('Name', name, { hint: b ? '' : 'Lowercase letters, digits, - or _.' }),
                field('Bot token', token, { hint: 'Write-only. Developer Portal > your application > Bot: turn off "Public Bot" (only you can add it) and turn on "Message Content Intent" (it reads the channels it joined).' }),
                field('Agent', agent, { hint: 'Answers when the bot is tagged. Give it no tools: anyone in the Discord server can talk to it.' }),
                field('Allowed servers', guilds, { stack: true, hint: 'Discord server ids (Discord: Settings > Advanced > Developer Mode, then right-click the server > Copy Server ID). The bot leaves any other server. A removed server is left at the next start.' }),
                field('Holding line', holdOn, { hint: 'When a reply takes more than ten seconds (three in voice) the bot says "Let me think about that." first. Off: it stays silent until the reply.' }),
                field('Conversation without a tag (seconds)', followS, { hint: 'For this long after the bot answers, what anyone says next in that channel reaches the agent without a tag. It answers only what is meant for it and stays silent otherwise; after twelve such messages a tag is needed again. 0: a tag is always needed. It never starts a conversation by itself.' }),
                field('Allow voice', voiceOn, { hint: 'Someone in a voice channel tags the bot with "join" and it joins; "leave" and it leaves. In voice it answers everything said, in the agent\'s own voice. Off: it refuses to join, and leaves at once.' }),
                note, actions(btn(b ? 'Save' : 'Add bot', save, 'primary')));
        };
        // Smilies: pictures of the agent per emotion, posted with its replies (they belong to the agent)
        const smileyGroup = (agent) => {
            const box = el('div', { class: 'discord-smilies' });
            const msg = el('small', { class: 'ui-field-hint', role: 'status' });
            const raw = (method, path, bodyData, name) => fetch(path, { method, body: bodyData,
                headers: { 'X-Session-Token': window.SESSION_TOKEN, ...(name ? { 'X-Filename': name } : {}) } });
            const show = async () => {
                box.textContent = '';
                const r = await api('GET', `/api/admin/discord-smilies/${encodeURIComponent(agent)}`);
                if (!r.smilies.length) { box.append(note('None yet.')); return; }
                for (const s of r.smilies) {
                    const img = el('img', { alt: s.emotion, width: 64, height: 64, loading: 'lazy' });
                    raw('GET', `/api/admin/discord-smilies/${encodeURIComponent(agent)}/${encodeURIComponent(s.emotion)}`)
                        .then(res => res.ok ? res.blob() : null).then(bl => { if (bl) img.src = URL.createObjectURL(bl); });
                    box.append(el('figure', { class: 'discord-smiley' }, img, el('figcaption', { text: s.emotion }),
                        el('button', { type: 'button', class: 'ui-btn ghost sm', text: 'Remove', 'aria-label': `Remove ${s.emotion}`,
                            onclick: guard(async () => { await api('DELETE', `/api/admin/discord-smilies/${encodeURIComponent(agent)}/${encodeURIComponent(s.emotion)}`); show(); }) })));
                }
            };
            const input = el('input', { type: 'file', accept: '.gif,.png,.zip,image/gif,image/png,application/zip', multiple: true, 'aria-label': 'Add smilies' });
            input.addEventListener('change', async () => {
                msg.textContent = 'Uploading…';
                try {
                    const added = [];
                    for (const f of input.files) {
                        const res = await raw('POST', `/api/admin/discord-smilies/${encodeURIComponent(agent)}`, f, f.name);
                        const r = await res.json();
                        if (!res.ok) throw new Error(`${f.name}: ${r.error}`);
                        added.push(...r.added);
                    }
                    msg.textContent = `Added: ${added.join(', ')}`;
                } catch (err) { msg.textContent = err.message; }
                input.value = '';
                show();
            });
            show().catch(err => { box.textContent = err.message; });
            return group(`Smilies of @${agent}`, { id: 'discord-smilies-' + agent, open: false },
                note('One picture per emotion, named after it (hazel_happy.gif is "happy"). The agent is told which emotions it has and ends a reply that carries one with [face:<emotion>]; the bot posts the picture. They belong to the agent, so every bot linked to it uses them.'),
                box, actions(input, msg));
        };
        const ctl = (b, action) => guard(async () => { await api('POST', `/api/admin/discord/${encodeURIComponent(b.id)}/${action}`); refresh('discord'); });
        const cards = data.bots.map(b => {
            const [label, tone] = STATE[b.state] || [b.state, ''];
            const acts = b.state === 'stopped' || b.state === 'error'
                ? [btn('Start', ctl(b, 'start'), 'primary')]
                : [btn('Restart', ctl(b, 'restart')), btn('Stop', ctl(b, 'stop'))];
            acts.push(btn('Delete', guard(async () => {
                if (!confirm(`Delete the ${b.id} bot? It stops and its settings and token are removed. It stays in its Discord servers until you kick it there.`)) return;
                await api('DELETE', `/api/admin/discord/${encodeURIComponent(b.id)}`); refresh('discord');
            }), 'danger'));
            const servers = b.servers.length
                ? table(['Server', 'Id', ''], b.servers.map(s => [s.name, s.id, b.guilds.includes(s.id) ? UIKit.badge('Allowed', 'ok') : UIKit.badge('Leaving', 'warn')]))
                : note(b.state === 'online' ? 'Not in any server yet.' : 'Servers show while the bot is online.');
            const invites = Object.entries(b.invites || {}).map(([g, url]) =>
                field(`Invite to ${g}`, el('a', { href: url, target: '_blank', rel: 'noopener', text: 'Add to this server' })));
            const groupsBox = (b.groups || []).length
                ? table(['Server', 'Channels here'], b.groups.map(g => [g.title,
                    g.channels.map(c => `${c.label} → #${c.name}${c.left ? ' (left)' : ''}`).join(', ') || '—']))
                : note('None yet. When the bot is online in an allowed server, that server appears in the channel list with one channel; type /join there to see the others.');
            return group(`${b.id} → @${b.agent}`, { id: 'discord-' + b.id },
                card(field('State', el('span', {}, UIKit.badge(label, tone), b.error ? ` ${b.error}` : '')),
                    ...(() => {
                        const ag = agents.find(a => a.name === b.agent);
                        return ag && (ag.permissions || 'read') !== 'none'
                            ? [field('Tools', UIKit.badge('Has tools', 'warn'), { hint: `People in Discord can make @${b.agent} use them (read files, search the web). Room agents > ${b.agent} > Permissions > No tools makes it chat only.` })]
                            : [];
                    })(),
                    field('Discord account', value(b.user || '—')),
                    field('Start with AgentHub', value(b.autostart ? 'Yes' : 'No'), { hint: 'Start and Stop set this.' }),
                    field('Voice', b.voice_channels && b.voice_channels.length
                        ? el('span', { class: 'ui-actions' }, value('In ' + b.voice_channels.join(', ')), btn('Leave voice', ctl(b, 'leave-voice'), 'sm'))
                        : value(b.voice ? 'Allowed, not in a voice channel' : 'Off')),
                    ...invites),
                actions(...acts),
                group('Servers it is in', { open: true }, servers),
                group('Channel groups', { open: true }, groupsBox,
                    note('In a group channel: /show channels, /join (lists), /join <channel>, /voice <channel>, /leave [channel].')),
                smileyGroup(b.agent),
                group('Settings', { open: false }, form(b), note('A new token or server list is used from the next start.')));
        });
        body.append(...(cards.length ? cards : [empty('No Discord bots', 'Create an application at discord.com/developers, add a bot to it, then add it here.')]),
            group('New bot', { open: !cards.length }, form(null)));
    }

    // Agent state and reply emotion for displays and automations (mqtt_out.py)
    async function renderMqtt(body) {
        heading(body, 'MQTT', 'Publishes each agent\'s state and the emotion of its replies to a broker, for displays and automations.');
        const m = await load(body, api('GET', '/api/admin/mqtt'));
        const on = el('input', { type: 'checkbox', checked: m.enabled });
        const host = el('input', { value: m.host, placeholder: 'e.g. 192.168.1.10' });
        const port = el('input', { type: 'number', min: '1', max: '65535', value: m.port });
        const user = el('input', { value: m.username, autocomplete: 'off' });
        const pass = el('input', { type: 'password', autocomplete: 'new-password', placeholder: m.has_password ? 'Stored. Leave blank to keep it.' : 'None' });
        const prefix = el('input', { value: m.prefix });
        const note = el('p', { class: m.error ? 'settings-warn' : 'settings-note', role: 'status', text: m.error ? `Last publish failed: ${m.error}` : '' });
        const send = (test) => api('POST', '/api/admin/mqtt', { enabled: on.checked, host: host.value, port: port.value,
            username: user.value, password: pass.value, prefix: prefix.value, test });
        const topics = el('code', { class: 'settings-value' });
        const showTopics = () => { const p = prefix.value.trim() || 'agenthub'; topics.textContent = `${p}/<agent>/state · ${p}/<agent>/emotion · ${p}/<agent>/status · ${p}/<agent>/speaking · ${p}/<agent>/mood`; };
        prefix.addEventListener('input', showTopics);
        showTopics();
        body.append(
            card(field('Publish to MQTT', on, { hint: 'Off: nothing is sent.' }),
                field('Broker address', host), field('Port', port),
                field('User name', user, { hint: 'Blank: no sign-in.' }), field('Password', pass, { hint: 'Write-only.' }),
                field('Topic prefix', prefix),
                field('Topics', topics, { stack: true, hint: 'state: idle, busy or offline. emotion: the tag of the last reply without brackets, or neutral. status: both as JSON. speaking: true or false. mood: what an agent with the emotion engine feels, as JSON (feeling, word, face, pleasure, arousal, dominance). All retained.' })),
            note,
            actions(btn('Send a test message', async () => {
                    note.className = 'settings-note'; note.textContent = 'Sending…';
                    try {
                        const r = await send(true);
                        note.className = r.test === 'ok' ? 'settings-note' : 'settings-warn';
                        note.textContent = r.test === 'ok' ? `Saved. The broker accepted a message on ${r.prefix}/hub.` : `Saved, but the broker refused: ${r.test}`;
                    } catch (err) { note.className = 'settings-warn'; note.textContent = err.message; }
                }),
                btn('Save', guard(async () => { await send(false); refresh('mqtt'); }), 'primary')));
    }

    window.SettingsModal = { open, close };
    window.toggleSettings = open;  // gear icon (inline onclick) opens this window
    window.openSettings = open;
})();
