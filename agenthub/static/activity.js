// Agent activity in the chat. While an agent works, a live card shows its status, thinking,
// tool calls (with their output) and the reply as it is written. When the reply is posted the
// card goes away and the reply carries a collapsed "Worked for ..." section with the same steps
// (metadata.activity). Server events: agent_activity {sid, i, item} / {sid, done}, agent_stream.
(function () {
    'use strict';

    const live = {};  // sid -> {el, items, t0, agent, channel, text, tick}

    const esc = (s) => window.escapeHtml ? window.escapeHtml(String(s ?? '')) : String(s ?? '');

    function fmtSecs(s) {
        s = Math.max(0, Math.round(s));
        return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`;
    }

    // One line that says what a tool call did: the command, path, pattern or query when there is one.
    function toolBrief(item) {
        let args = {};
        try { args = JSON.parse(item.input || '{}') || {}; } catch (_) { return (item.input || '').slice(0, 120); }
        const v = args.command ?? args.file_path ?? args.path ?? args.pattern ?? args.query ?? args.url
            ?? args.description ?? args.prompt ?? Object.values(args).find(x => typeof x === 'string') ?? '';
        return String(v).replace(/\s+/g, ' ').slice(0, 140);
    }

    function firstLine(text, n = 110) {
        const t = String(text || '').replace(/\s+/g, ' ').trim();
        return t.length > n ? t.slice(0, n) + '…' : t;
    }

    function prettyInput(raw) {
        try { return JSON.stringify(JSON.parse(raw), null, 2); } catch (_) { return raw || ''; }
    }

    function stepEl(item) {
        if (item.k === 'status') {
            const d = document.createElement('div');
            d.className = 'act-status';
            d.textContent = item.text || '';
            return d;
        }
        if (item.k === 'text') {
            const d = document.createElement('div');
            d.className = 'act-note';
            d.innerHTML = window.renderMarkdown ? window.renderMarkdown(item.text || '') : esc(item.text);
            return d;
        }
        const det = document.createElement('details');
        det.className = 'act-step' + (item.sub ? ' act-sub' : '') + (item.err ? ' act-err' : '');
        const sum = document.createElement('summary');
        let body = '';
        if (item.k === 'thinking') {
            sum.innerHTML = `<span class="act-kind">Thinking</span><span class="act-brief">${esc(firstLine(item.text))}</span>`;
            body = `<div class="act-pre act-think">${esc(item.text)}</div>`;
        } else {
            const pending = item.out === undefined;
            sum.innerHTML = `<span class="act-kind">${esc(item.name)}</span><span class="act-brief">${esc(toolBrief(item))}</span>`
                + (pending ? '<span class="act-spin" aria-label="running"></span>' : '')
                + (item.err ? '<span class="act-flag">error</span>' : '');
            body = `<div class="act-label">Input</div><pre class="act-pre">${esc(prettyInput(item.input))}</pre>`
                + (pending ? '' : `<div class="act-label">Output</div><pre class="act-pre">${esc(item.out || '(empty)')}</pre>`);
        }
        det.appendChild(sum);
        det.insertAdjacentHTML('beforeend', `<div class="act-body">${body}</div>`);
        return det;
    }

    // Replace step i in a list, keeping it open if the reader opened it
    function putStep(list, i, item) {
        const old = list.children[i];
        const el = stepEl(item);
        if (old) {
            if (old.open) el.open = true;
            list.replaceChild(el, old);
        } else {
            list.appendChild(el);
        }
    }

    function card(ev) {
        const color = window.getColor ? window.getColor(ev.agent) : 'var(--accent)';
        const el = document.createElement('div');
        el.className = 'message other activity-live';
        el.dataset.channel = ev.channel || 'general';
        el.dataset.sid = ev.sid;
        el.innerHTML = `<div class="chat-bubble act-card" style="--bubble-color: ${color}">
            <div class="bubble-header"><span class="msg-sender" style="color: ${color}">${esc(ev.agent)}</span>
            <span class="act-live-state" role="status"><span class="act-dot"></span><span class="act-what">working</span>
            · <span class="act-clock">0s</span></span></div>
            <details class="act-wrap" open><summary class="act-wrap-sum">Steps</summary><div class="act-list"></div></details>
            <div class="act-text msg-text"></div></div>`;
        if ((ev.channel || 'general') !== window.activeChannel) el.style.display = 'none';
        const box = document.getElementById('messages');
        box?.appendChild(el);
        const st = { el, items: [], t0: Date.now(), agent: ev.agent, channel: ev.channel };
        st.tick = setInterval(() => {
            el.querySelector('.act-clock').textContent = fmtSecs((Date.now() - st.t0) / 1000);
        }, 1000);
        live[ev.sid] = st;
        return st;
    }

    function current(item) {
        if (!item) return 'working';
        if (item.k === 'thinking') return 'thinking';
        if (item.k === 'tool') return item.out === undefined ? `running ${item.name}` : 'working';
        if (item.k === 'status') return item.text;
        return 'writing';
    }

    function keepBottom(st) {
        const box = document.getElementById('messages');
        if (box && box.lastElementChild !== st.el) box.appendChild(st.el);  // stays below newer messages
        // autoScroll is a top-level let in chat.js: shared by name, not on window
        if (typeof autoScroll !== 'undefined' && autoScroll && window.scrollToBottom) window.scrollToBottom(true);
    }

    function end(sid) {
        const st = live[sid];
        if (!st) return;
        clearInterval(st.tick);
        st.el.remove();
        delete live[sid];
    }

    Hub.on('agent_activity', (ev) => {
        if (ev.thread != null || !ev.sid) return;  // thread replies live in the thread panel
        if (ev.done) { end(ev.sid); return; }
        const st = live[ev.sid] || card(ev);
        st.items[ev.i] = ev.item;
        putStep(st.el.querySelector('.act-list'), ev.i, ev.item);
        st.el.querySelector('.act-what').textContent = current(ev.item);
        keepBottom(st);
    });

    // The reply as it is written (API agents). <think> blocks are shown as a thinking step.
    Hub.on('agent_stream', (ev) => {
        if (ev.thread != null || !ev.sid) return;
        const st = live[ev.sid] || card(ev);
        const raw = ev.text || '';
        const think = raw.match(/<think>([\s\S]*?)(<\/think>|$)/);
        const shown = raw.replace(/<think>[\s\S]*?(<\/think>|$)/g, '').trim();
        if (think && think[1].trim()) {
            if (st.thinkAt === undefined) st.thinkAt = st.items.length;
            st.items[st.thinkAt] = { k: 'thinking', text: think[1].trim() };
            putStep(st.el.querySelector('.act-list'), st.thinkAt, st.items[st.thinkAt]);
        }
        st.el.querySelector('.act-text').innerHTML = window.renderMarkdown ? window.renderMarkdown(shown) : esc(shown);
        st.el.querySelector('.act-what').textContent = shown ? 'writing' : 'thinking';
        keepBottom(st);
    });

    // Timings under a message: the model calls of a reply, its speech, and how a spoken message was heard.
    // Called again when the server adds to them (message_update), e.g. once the speech has been made.
    const n1 = (v) => (Math.round(v * 10) / 10).toFixed(1);
    function metrics(el, msg) {
        const meta = msg.metadata || {};
        const lines = [];
        const u = meta.usage;
        if (u && u.out) lines.push(['Model', [
            u.tps ? `${u.tps} tokens/s` : '', `${u.out} tokens out`,
            `${u.prompt} in` + (u.cached ? ` (${u.cached} cached)` : ''),
            `${n1(u.secs || 0)} s` + (u.calls > 1 ? ` in ${u.calls} calls` : ''),
            u.turn_s != null ? `turn ${n1(u.turn_s)} s` : '',
            u.wait_s != null ? `answered ${n1(u.wait_s)} s after the message` : '', u.model || '']]);
        const sp = meta.speech;
        if (sp && sp.parts) lines.push(['Speech', [
            `first audio after ${n1(sp.first_s || 0)} s`, `${n1(sp.audio_s || 0)} s of audio`, `made in ${n1(sp.synth_s || 0)} s`,
            sp.parts > 1 ? `${sp.parts} parts` : '', sp.via || '']]);
        const h = meta.heard;
        if (h) lines.push(['Heard', [
            h.audio_s != null ? `${n1(h.audio_s)} s of audio` : '', h.stt_s != null ? `transcribed in ${n1(h.stt_s)} s` : '',
            h.check_s != null ? `checked in ${n1(h.check_s)} s` : '', h.reason || '', meta.device || '']]);
        let box = el.querySelector('.act-usage');
        if (!lines.length) { if (box) box.remove(); return; }
        if (!box) {
            box = document.createElement('div');
            box.className = 'act-usage';
            (el.querySelector('.msg-text') || el.lastElementChild)?.after(box);
        }
        box.textContent = '';
        for (const [label, parts] of lines) {
            const row = document.createElement('div');
            const b = document.createElement('span');
            b.className = 'act-usage-label';
            b.textContent = label;
            row.append(b, document.createTextNode(parts.filter(Boolean).join(' · ')));
            box.append(row);
        }
    }

    // Called by chat.js for each posted chat bubble
    function decorate(el, msg) {
        const meta = msg.metadata || {};
        if (meta.stream) end(meta.stream);
        metrics(el, msg);
        const a = meta.activity;
        if (!a || !(a.steps || []).length) return;
        const tools = a.steps.filter(s => s.k === 'tool').length;
        const det = document.createElement('details');
        det.className = 'act-wrap act-done';
        const parts = [`Worked for ${fmtSecs(a.secs || 0)}`];
        if (tools) parts.push(`${tools} tool call${tools === 1 ? '' : 's'}`);
        det.innerHTML = `<summary class="act-wrap-sum">${esc(parts.join(' · '))}</summary><div class="act-list"></div>`;
        const list = det.querySelector('.act-list');
        if (a.dropped) list.insertAdjacentHTML('beforeend', `<div class="act-status">${a.dropped} earlier steps not kept</div>`);
        a.steps.forEach(s => list.appendChild(stepEl(s)));
        el.querySelector('.msg-text')?.before(det);
    }

    window.Activity = { decorate, metrics };
})();
