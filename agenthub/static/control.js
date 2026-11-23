// control.js -- Stop agents, emoji reactions and message threads.
// chat.js calls Control.decorate(el, msg) for each chat bubble and Control.threadReply(msg)
// for thread replies (kept out of the main timeline). Everything else arrives over Hub.

const Control = (() => {
    const REACTIONS = [
        ['👍', 'Thumbs up'], ['❤️', 'Heart'], ['😂', 'Laughing'], ['😮', 'Surprised'],
        ['😢', 'Sad'], ['😠', 'Angry'], ['😒', 'Unamused'], ['🙄', 'Eye roll'],
        ['🤪', 'Zany'], ['🎉', 'Party'], ['👀', 'Eyes'], ['🔥', 'Fire'],
        ['🤔', 'Thinking'], ['💀', 'Skull'], ['🤡', 'Clown'],
    ];
    const ICON = {
        smile: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M8.5 14.5c.9 1.2 2.1 1.8 3.5 1.8s2.6-.6 3.5-1.8"/><path d="M9 9.5h.01M15 9.5h.01"/></svg>',
        thread: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20.5l1.4-5A8 8 0 1 1 21 12z"/><path d="M8.5 10.5h7M8.5 14h4.5"/></svg>',
        clip: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m21 12-8.6 8.6a5.5 5.5 0 0 1-7.8-7.8l9.2-9.2a3.7 3.7 0 0 1 5.2 5.2l-9.2 9.2a1.8 1.8 0 0 1-2.6-2.6l8.5-8.5"/></svg>',
        stop: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="1.5"/></svg>',
        close: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>',
        send: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
    };
    const messages = {};   // id -> message (parents and replies we have seen)
    const replies = {};    // parent id -> [reply messages]
    const bubbles = {};    // id -> timeline bubble (history renders into a fragment, so no document lookups)
    let openThread = null; // parent id shown in the thread panel
    let picker = null;

    const api = (path, body) => fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Session-Token': window.SESSION_TOKEN },
        body: JSON.stringify(body),
    }).then(r => r.json()).catch(() => null);
    const me = () => (typeof username === 'string' ? username : 'user');

    // ---------------- Reactions ----------------

    function reactionsHtml(msg) {
        const chips = Object.entries(msg.reactions || {}).map(([emoji, names]) => {
            const mine = names.some(n => n.toLowerCase() === me().toLowerCase());
            const who = names.join(', ');
            return `<button type="button" class="ctl-chip${mine ? ' mine' : ''}" aria-pressed="${mine}"
                data-emoji="${escapeHtml(emoji)}" data-who="${escapeHtml(who)}"
                aria-label="${escapeHtml(emoji)} ${names.length}: ${escapeHtml(who)}${mine ? ' (click to remove yours)' : ''}"
                onclick="Control.toggle(${msg.id}, this.dataset.emoji, event)"><span class="ctl-chip-emoji">${escapeHtml(emoji)}</span><span class="ctl-chip-count">${names.length}</span></button>`;
        }).join('');
        const add = chips ? `<button type="button" class="ctl-chip ctl-chip-add" aria-label="Add reaction" title="Add reaction"
                onclick="Control.openPicker(${msg.id}, this, event)">${ICON.smile}</button>` : '';
        return chips + add;
    }

    function renderReactions(id) {
        const msg = messages[id];
        if (!msg) return;
        const bars = [...(bubbles[id]?.querySelectorAll(':scope > .ctl-reactions') || []),
                      ...document.querySelectorAll(`#ctl-thread-panel .ctl-reactions[data-id="${id}"]`)];
        for (const bar of bars) {
            bar.innerHTML = reactionsHtml(msg);
            bar.hidden = !msg.reactions || !Object.keys(msg.reactions).length;
        }
    }

    function toggle(id, emoji, event) {
        if (event) event.stopPropagation();
        closePicker();
        api(`/api/messages/${id}/react`, { emoji });  // the broadcast updates every view
    }

    function openPicker(id, anchor, event) {
        if (event) event.stopPropagation();
        if (picker && picker.dataset.id === String(id)) { closePicker(); return; }
        closePicker();
        picker = document.createElement('div');
        picker.className = 'ctl-picker';
        picker.dataset.id = id;
        picker.setAttribute('role', 'dialog');
        picker.setAttribute('aria-label', 'Add reaction');
        picker.innerHTML = REACTIONS.map(([e, name]) =>
            `<button type="button" class="ctl-pick" aria-label="${name}" title="${name}" data-emoji="${e}">${e}</button>`).join('');
        picker.addEventListener('click', (e) => {
            const b = e.target.closest('.ctl-pick');
            if (b) { toggle(id, b.dataset.emoji, e); anchor.focus(); }
        });
        picker.addEventListener('keydown', (e) => {   // arrow keys move through the grid
            const items = [...picker.querySelectorAll('.ctl-pick')];
            const i = items.indexOf(document.activeElement);
            const step = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: 5, ArrowUp: -5 }[e.key];
            if (step && i >= 0) { e.preventDefault(); items[Math.max(0, Math.min(items.length - 1, i + step))].focus(); }
        });
        document.body.appendChild(picker);
        const r = anchor.getBoundingClientRect();
        const w = picker.offsetWidth, h = picker.offsetHeight;
        const top = r.top - h - 6 > 8 ? r.top - h - 6 : r.bottom + 6;
        picker.style.top = `${top}px`;
        picker.style.left = `${Math.max(8, Math.min(window.innerWidth - w - 8, r.left + r.width / 2 - w / 2))}px`;
        picker._anchor = anchor;
        picker.querySelector('.ctl-pick').focus();
    }

    function closePicker() {
        if (picker) { picker.remove(); picker = null; }
    }

    Hub.on('reaction', (ev) => {
        const msg = messages[ev.id];
        if (!msg) return;
        msg.reactions = ev.reactions;
        renderReactions(ev.id);
    });

    // ---------------- Message decoration ----------------

    function decorate(el, msg) {
        messages[msg.id] = msg;
        const bubble = el.querySelector('.chat-bubble');
        if (!bubble) return;
        const bar = document.createElement('div');
        bar.className = 'ctl-reactions';
        bar.dataset.id = msg.id;
        bubble.appendChild(bar);
        const summary = document.createElement('button');
        summary.type = 'button';
        summary.className = 'ctl-thread-summary';
        summary.dataset.id = msg.id;
        summary.onclick = (e) => { e.stopPropagation(); open(msg.id); };
        bubble.appendChild(summary);
        const tools = document.createElement('div');
        tools.className = 'ctl-toolbar';
        tools.innerHTML = `<button type="button" class="ctl-tool" aria-label="Add reaction" title="Add reaction">${ICON.smile}</button>`
            + `<button type="button" class="ctl-tool" aria-label="Reply in thread" title="Reply in thread">${ICON.thread}</button>`;
        const [reactBtn, threadBtn] = tools.children;
        reactBtn.onclick = (e) => openPicker(msg.id, reactBtn, e);
        threadBtn.onclick = (e) => { e.stopPropagation(); open(msg.id, true); };
        const speak = window.Voice?.button(msg);  // read aloud (agent messages)
        if (speak) tools.appendChild(speak);
        const keep = window.Voice?.saveButton?.(msg);  // the message as an audio file
        if (keep) tools.appendChild(keep);
        bubble.appendChild(tools);
        bubbles[msg.id] = bubble;
        renderReactions(msg.id);
        renderSummary(msg.id);
    }

    // ---------------- Threads ----------------

    function renderSummary(id) {
        const list = replies[id] || [];
        for (const s of bubbles[id]?.querySelectorAll(':scope > .ctl-thread-summary') || []) {
            s.hidden = !list.length;
            if (!list.length) continue;
            const last = list[list.length - 1];
            s.innerHTML = `${ICON.thread}<span class="ctl-thread-count">${list.length} ${list.length === 1 ? 'reply' : 'replies'}</span>`
                + `<span class="ctl-thread-last">Last reply from <span style="color: ${getColor(last.sender)}">${escapeHtml(last.sender)}</span></span>`;
            s.setAttribute('aria-label', `${list.length} ${list.length === 1 ? 'reply' : 'replies'}, last from ${last.sender}. Open thread`);
        }
    }

    function threadReply(msg) {
        messages[msg.id] = msg;
        const list = replies[msg.thread] || (replies[msg.thread] = []);
        if (list.some(m => m.id === msg.id)) return;
        list.push(msg);
        renderSummary(msg.thread);
        if (openThread === msg.thread) {
            const body = panel().querySelector('.ctl-thread-body');
            const stick = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
            body.appendChild(item(msg, false));
            if (stick) body.scrollTop = body.scrollHeight;
        }
    }

    function item(msg, isParent) {
        const div = document.createElement('div');
        div.className = 'ctl-thread-msg' + (isParent ? ' parent' : '');
        div.id = `thread-msg-${msg.id}`;
        const atts = (msg.attachments || []).map(a => attachmentMarkup(a)).join('');
        div.innerHTML = `<div class="ctl-thread-meta"><span class="ctl-thread-sender" style="color: ${getColor(msg.sender)}">${escapeHtml(msg.sender)}</span>`
            + `<span class="msg-time">${escapeHtml(msg.time || '')}</span><span class="msg-num">#${msg.id}</span></div>`
            + `<div class="msg-text">${renderMarkdown(msg.text || '')}</div>${atts ? `<div class="msg-attachments">${atts}</div>` : ''}`
            + `<div class="ctl-thread-actions"><div class="ctl-reactions" data-id="${msg.id}"></div>`
            + `<button type="button" class="ctl-tool" aria-label="Add reaction" title="Add reaction">${ICON.smile}</button></div>`;
        const btn = div.querySelector('.ctl-tool');
        btn.onclick = (e) => openPicker(msg.id, btn, e);
        return div;
    }

    function panel() {
        let p = document.getElementById('ctl-thread-panel');
        if (p) return p;
        p = document.createElement('aside');
        p.id = 'ctl-thread-panel';
        p.className = 'hidden';
        p.setAttribute('role', 'dialog');
        p.setAttribute('aria-label', 'Thread');
        p.innerHTML = `<div class="ctl-thread-header"><span class="ctl-thread-title">Thread</span><span class="ctl-thread-channel"></span>
                <button type="button" class="ctl-icon-btn ctl-thread-close" aria-label="Close thread" title="Close (Esc)">${ICON.close}</button></div>
            <div class="ctl-thread-body"></div>
            <div class="ctl-thread-files"></div>
            <div class="ctl-thread-mentions mention-menu hidden" role="listbox" aria-label="Mention"></div>
            <form class="ctl-thread-composer">
                <button type="button" class="ctl-icon-btn ctl-thread-attach" aria-label="Attach files" title="Attach files">${ICON.clip}</button>
                <input type="file" multiple hidden>
                <textarea rows="1" placeholder="Reply in thread..." aria-label="Reply in thread"></textarea>
                <button type="submit" class="ctl-icon-btn ctl-thread-send" aria-label="Send reply">${ICON.send}</button>
            </form>`;
        p.querySelector('.ctl-thread-close').onclick = close;
        const ta = p.querySelector('textarea');
        const form = p.querySelector('form');
        const picker = p.querySelector('input[type=file]');
        p.querySelector('.ctl-thread-attach').onclick = () => picker.click();
        picker.onchange = () => { addFiles(Array.from(picker.files)); picker.value = ''; };
        form.onsubmit = (e) => { e.preventDefault(); send(ta); };
        ta.addEventListener('keydown', (e) => {
            if (mentionKey(e, ta)) return;
            if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(ta); }
        });
        ta.addEventListener('input', () => {
            ta.style.height = 'auto'; ta.style.height = `${Math.min(ta.scrollHeight, 160)}px`;
            mentionMenu(ta);
        });
        ta.addEventListener('blur', () => setTimeout(() => hideMentions(), 150));
        (document.getElementById('content-area') || document.body).appendChild(p);
        return p;
    }

    function open(id, focus) {
        const parent = messages[id];
        if (!parent) return;
        closePicker();
        openThread = id;
        const p = panel();
        p.querySelector('.ctl-thread-channel').textContent = `#${parent.channel || 'general'}`;
        const body = p.querySelector('.ctl-thread-body');
        body.innerHTML = '';
        body.appendChild(item(parent, true));
        const list = replies[id] || [];
        const divider = document.createElement('div');
        divider.className = 'ctl-thread-divider';
        divider.textContent = list.length ? `${list.length} ${list.length === 1 ? 'reply' : 'replies'}` : 'No replies yet';
        body.appendChild(divider);
        for (const m of list) body.appendChild(item(m, false));
        for (const m of [parent, ...list]) renderReactions(m.id);
        p.classList.remove('hidden');
        body.scrollTop = body.scrollHeight;
        if (focus || window.matchMedia('(hover: hover)').matches) p.querySelector('textarea').focus();
    }

    function close() {
        const p = document.getElementById('ctl-thread-panel');
        if (!p || p.classList.contains('hidden')) return false;
        p.classList.add('hidden');
        openThread = null;
        return true;
    }

    // ---- thread composer: attachments ----
    let threadFiles = [], uploading = 0;

    function renderFiles() {
        const box = document.querySelector('#ctl-thread-panel .ctl-thread-files');
        if (!box) return;
        box.innerHTML = '';
        threadFiles.forEach((att, i) => box.appendChild(createAttachmentPreview(att, () => {
            threadFiles.splice(i, 1); renderFiles();
        })));
        if (uploading) box.append(Object.assign(document.createElement('span'), { className: 'ctl-thread-uploading', textContent: 'Uploading…' }));
    }

    async function addFiles(files) {
        for (const f of files) {
            uploading++; renderFiles();
            try { threadFiles.push(await uploadAttachment(f)); }
            catch (err) { alert(`${f.name}: ${err.message}`); }
            finally { uploading--; renderFiles(); }
        }
    }

    // ---- thread composer: @-autocomplete (same candidates as the main composer) ----
    let mentionAt = -1, mentionIdx = 0, mentionMatches = [];

    function hideMentions() {
        document.querySelector('#ctl-thread-panel .ctl-thread-mentions')?.classList.add('hidden');
        mentionMatches = [];
    }

    function mentionMenu(ta) {
        const menu = document.querySelector('#ctl-thread-panel .ctl-thread-mentions');
        const m = /(^|[^\w])@([\w-]*)$/.exec(ta.value.slice(0, ta.selectionStart));
        const all = typeof getMentionCandidates === 'function' ? getMentionCandidates() : [];
        mentionMatches = m ? all.filter(c => c.name.toLowerCase().startsWith(m[2].toLowerCase())
                                         || c.label.toLowerCase().startsWith(m[2].toLowerCase())) : [];
        if (!mentionMatches.length) return hideMentions();
        mentionAt = ta.selectionStart - m[2].length - 1;
        mentionIdx = Math.min(mentionIdx, mentionMatches.length - 1);
        menu.innerHTML = '';
        mentionMatches.forEach((c, i) => {
            const row = document.createElement('div');
            row.className = 'mention-item' + (i === mentionIdx ? ' active' : '');
            row.setAttribute('role', 'option');
            row.innerHTML = `<span class="mention-dot" style="background: ${c.color}"></span><span class="mention-name">${escapeHtml(c.label)}</span>`;
            row.addEventListener('mousedown', (e) => { e.preventDefault(); pickMention(ta, c.name); });
            menu.appendChild(row);
        });
        menu.classList.remove('hidden');
    }

    function pickMention(ta, name) {
        const tag = name === 'all agents' ? '@all ' : `@${name} `;
        ta.value = ta.value.slice(0, mentionAt) + tag + ta.value.slice(ta.selectionStart);
        ta.selectionStart = ta.selectionEnd = mentionAt + tag.length;
        hideMentions();
        ta.focus();
    }

    function mentionKey(e, ta) {
        if (!mentionMatches.length) return false;
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            mentionIdx = (mentionIdx + (e.key === 'ArrowDown' ? 1 : -1) + mentionMatches.length) % mentionMatches.length;
            mentionMenu(ta);
        } else if (e.key === 'Enter' || e.key === 'Tab') {
            pickMention(ta, mentionMatches[mentionIdx].name);
        } else if (e.key === 'Escape') {
            hideMentions();
        } else return false;
        e.preventDefault();
        e.stopPropagation();
        return true;
    }

    function send(ta) {
        const text = ta.value.trim();
        const parent = messages[openThread];
        if ((!text && !threadFiles.length) || uploading || !parent || !ws || ws.readyState !== WebSocket.OPEN) return;
        ws.send(JSON.stringify({ type: 'message', text, sender: me(), channel: parent.channel || 'general',
                                 thread: parent.id, attachments: threadFiles }));
        ta.value = '';
        ta.style.height = 'auto';
        threadFiles = [];
        renderFiles();
    }

    // Paste/drop into an open thread lands in the thread composer (chat.js asks first)
    window.ThreadComposer = {
        owns: (node) => openThread !== null && !!(node && node.closest && node.closest('#ctl-thread-panel')),
        add: addFiles,
    };

    Hub.on('delete', (ev) => {
        for (const id of ev.ids || []) {
            const m = messages[id];
            delete messages[id];
            delete bubbles[id];
            if (openThread === id) close();
            if (m && m.thread != null && replies[m.thread]) {
                replies[m.thread] = replies[m.thread].filter(r => r.id !== id);
                renderSummary(m.thread);
                document.getElementById(`thread-msg-${id}`)?.remove();
            }
        }
    });

    // ---------------- Stop agents ----------------

    let working = [], lastStatus = {};
    // Only agents working in the channel you're in (managed agents report their channel;
    // others count when they are members of it)
    const here = ([n, i]) => {
        const ch = typeof activeChannel === 'string' ? activeChannel : 'general';
        return i.busy_channel ? i.busy_channel === ch
            : (typeof agentInActiveChannel !== 'function' || agentInActiveChannel(n));
    };
    window.refreshWorking = () => {
        working = Object.entries(lastStatus).filter(([n, i]) => n !== 'paused' && i && i.busy).filter(here);
        renderWorking();
    };

    function renderWorking() {
        let bar = document.getElementById('ctl-working');
        if (!bar) {
            const footer = document.querySelector('footer');
            if (!footer) return;
            bar = document.createElement('div');
            bar.id = 'ctl-working';
            bar.setAttribute('role', 'status');
            bar.setAttribute('aria-live', 'polite');
            footer.prepend(bar);
        }
        bar.hidden = !working.length;
        bar.innerHTML = working.map(([name, info]) =>
            `<span class="ctl-working-agent"><span class="ctl-working-dot" style="background: ${info.color || 'var(--accent)'}"></span>`
            + `<span class="ctl-working-name">${escapeHtml(info.label || name)}</span><span class="ctl-working-state">working</span>`
            + `<button type="button" class="ctl-stop" data-name="${escapeHtml(name)}" aria-label="Stop ${escapeHtml(name)}" title="Stop @${escapeHtml(name)}">${ICON.stop}<span>Stop</span></button></span>`
        ).join('') + (working.length > 1
            ? `<button type="button" class="ctl-stop ctl-stop-all" aria-label="Stop all working agents">${ICON.stop}<span>Stop all</span></button>` : '');
    }

    function stop(name) {
        working = name ? working.filter(([n]) => n !== name) : [];
        renderWorking();
        api('/api/agents/stop', { name: name || '', channel: typeof activeChannel === 'string' ? activeChannel : 'general' });
    }

    Hub.on('status', (ev) => {
        lastStatus = ev.data || {};
        window.refreshWorking();
    });

    document.addEventListener('click', (e) => {
        const b = e.target.closest('.ctl-stop');
        if (b) { stop(b.classList.contains('ctl-stop-all') ? null : b.dataset.name); return; }
        if (picker && !picker.contains(e.target)) closePicker();
        const p = document.getElementById('ctl-thread-panel');
        if (p && !p.classList.contains('hidden') && !p.contains(e.target) && !e.target.closest('.ctl-thread-summary, .ctl-toolbar')) close();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        if (picker) { const a = picker._anchor; closePicker(); a?.focus(); e.stopPropagation(); return; }
        close();
    }, true);
    window.addEventListener('resize', closePicker);

    // ---------------- Thinking level (per channel, next reply) ----------------
    // Sent to models as reasoning_effort. "Default" leaves each model's own setting.
    const effortSel = document.createElement('select');
    effortSel.id = 'effort-select';
    effortSel.className = 'effort-select';
    effortSel.title = 'Thinking for agent replies in this channel (applies from the next reply)';
    effortSel.setAttribute('aria-label', 'Thinking level');
    effortSel.innerHTML = '<option value="">Think: default</option><option value="none">Think: off</option>'
        + '<option value="low">Think: low</option><option value="medium">Think: medium</option><option value="high">Think: high</option>';
    const chan = () => (typeof activeChannel !== 'undefined' ? activeChannel : 'general');
    async function loadEffort() {
        try {
            const r = await fetch(`/api/effort?channel=${encodeURIComponent(chan())}`, { headers: { 'X-Session-Token': window.SESSION_TOKEN } });
            if (r.ok) effortSel.value = (await r.json()).effort || '';
        } catch (_) { /* keep the shown value */ }
    }
    effortSel.addEventListener('change', async () => {
        const r = await fetch('/api/effort', { method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Session-Token': window.SESSION_TOKEN },
            body: JSON.stringify({ channel: chan(), effort: effortSel.value }) });
        if (!r.ok && typeof showToast === 'function') showToast('Could not change the thinking level', 'error');
    });
    Hub.on('effort', (ev) => { if (ev.channel === chan()) effortSel.value = ev.effort || ''; });
    window.addEventListener('agenthub-channel', loadEffort);
    const mountEffort = () => {
        const send = document.querySelector('#input-row .send-group');
        if (!send || effortSel.isConnected) return;
        send.before(effortSel);
        loadEffort();
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mountEffort); else mountEffort();

    return { decorate, threadReply, toggle, openPicker, open, close };
})();
window.Control = Control;
