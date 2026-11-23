// Voice: read agent messages aloud (play button, auto-speak per channel) and talk to agents
// hands-free (voice activity detection) or push-to-talk. Speech-to-text runs on the server
// (Whisper over Wyoming); text-to-speech uses each agent's voice from its configuration page.
// The microphone needs a secure page (https or localhost).
const Voice = (() => {
    const ICON = {
        speak: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 5 6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/></svg>',
        talk: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12h2M7 8v8M11 5v14M15 8v8M19 11v2"/></svg>',
    };
    const STOP_WORDS = /^(stop|wait|cancel|never ?mind|hold on|shut up)[.!]*$/i;

    const token = () => window.SESSION_TOKEN || window.__SESSION_TOKEN__;
    const post = (path, body, type = 'application/json') => fetch(path, {
        method: 'POST', headers: { 'Content-Type': type, 'X-Session-Token': token() },
        body: type === 'application/json' ? JSON.stringify(body) : body,
    });
    const store = {
        get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
        set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
    };
    const isAgent = (name) => typeof agentConfig !== 'undefined' && !!agentConfig[(name || '').toLowerCase()]
        || (typeof resolveAgent === 'function' && !!resolveAgent((name || '').toLowerCase()));

    // ---------------- Playback ----------------
    // One queue of sentences. The next sentence is fetched while the current one plays, across
    // messages too, so streamed sentences (agent_stream) play back to back.
    let queue = [];        // [{agent, text, id, clip?}] (clip: see startClip)
    let audio = null;      // the playing <audio>
    let playing = null;    // message id being read
    let gen = 0;           // bumps on stop: pending fetches are thrown away
    let running = false;   // the player loop is active
    let quietUntil = 0;    // the mic ignores sound until then (the speaker's echo dies down)
    let spoken = [];       // [{t, words}] sentences read aloud lately, to drop their echo
    const TAIL_MS = 800;
    const bargeIn = () => store.get('agenthub-voice-bargein', false);  // Settings > Voice; for headphones
    // Settings > Voice, on by default: hands-free speech goes to the server's voice check, which answers
    // only speech said TO an agent by name (anywhere in the sentence) or a follow-up (voice_gate.py)
    const namedMode = () => store.get('agenthub-voice-named', true);
    let heard = '';  // short status after the check ignored a sentence

    // Is the room speaker talking (or just stopped)? While it is, the mic is not listened to
    // unless barge-in is on.
    function speakingNow() { return playing != null || Date.now() < quietUntil; }

    // Does a transcript mostly repeat what was read aloud in the last 30 s? (echo)
    function isEcho(text) {
        const words = (text || '').toLowerCase().match(/[a-z0-9']+/g) || [];
        if (!words.length) return false;
        spoken = spoken.filter(x => Date.now() - x.t < 30000);
        const said = new Set(spoken.flatMap(x => x.words));
        return words.filter(w => said.has(w)).length / words.length >= 0.6;
    }

    function sentences(text) {
        const t = (text || '').replace(/```[\s\S]*?```/g, ' (code block) ').replace(/\s+/g, ' ').trim();
        const parts = t.match(/[^.!?]+[.!?]+["')\]]*\s*|[^.!?]+$/g) || [];
        const out = [];
        for (const p of parts) {
            if (out.length && (out[out.length - 1].length < 60 || p.length < 20) && out[out.length - 1].length + p.length < 280) out[out.length - 1] += p;
            else out.push(p);
        }
        return out.map(s => s.trim()).filter(Boolean);
    }

    // A clip starts rendering at once. Providers that stream raw PCM (Settings > Voice) hand over
    // chunks as they are made; others return one file.
    function startClip(agent, text, id) {
        const clip = { chunks: [], done: false, err: null, url: null, rate: 24000, wake: null, ctrl: new AbortController() };
        const poke = () => { const w = clip.wake; clip.wake = null; if (w) w(); };
        (async () => {
            try {
                const r = await fetch('/api/voice/speak', { method: 'POST', signal: clip.ctrl.signal,
                    headers: { 'Content-Type': 'application/json', 'X-Session-Token': token() },
                    body: JSON.stringify({ agent, text, message_id: id }) });
                if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `speech failed (${r.status})`);
                if ((r.headers.get('Content-Type') || '').startsWith('audio/pcm')) {
                    clip.rate = Number(r.headers.get('X-Sample-Rate')) || 24000;
                    const reader = r.body.getReader();
                    let odd = null;  // a PCM16 sample can straddle two network chunks
                    for (;;) {
                        const { value, done } = await reader.read();
                        if (done) break;
                        let bytes = value;
                        if (odd) { bytes = new Uint8Array(odd.length + value.length); bytes.set(odd); bytes.set(value, odd.length); odd = null; }
                        if (bytes.length % 2) { odd = bytes.slice(-1); bytes = bytes.slice(0, -1); }
                        if (bytes.length) { clip.chunks.push(bytes); poke(); }
                    }
                } else {
                    clip.url = URL.createObjectURL(await r.blob());
                }
            } catch (err) {
                if (err.name !== 'AbortError') clip.err = err;
            }
            clip.done = true;
            poke();
            prefetch();  // the voice server is free: start the next sentence
        })();
        return clip;
    }

    function dropClip(clip) {
        if (!clip) return;
        clip.ctrl.abort();
        if (clip.url) URL.revokeObjectURL(clip.url);
    }

    let actx = null;          // WebAudio context for streamed PCM
    let sources = [];         // scheduled PCM pieces (stopped by stop())

    async function playClip(clip, mine) {
        const wait = () => new Promise(r => { clip.wake = r; });
        while (!clip.done && !clip.chunks.length && !clip.url) await wait();
        if (mine !== gen) return;
        if (clip.err) { toast(clip.err.message); return; }
        if (clip.url) {  // a whole file
            await new Promise((resolve) => {
                audio = new Audio(clip.url);
                audio.onended = audio.onerror = () => { URL.revokeObjectURL(clip.url); resolve(); };
                audio.play().catch(() => { toast('The browser blocked audio: click anywhere on the page once, then try again.'); resolve(); });
            });
            audio = null;
            return;
        }
        actx = actx || new (window.AudioContext || window.webkitAudioContext)();
        if (actx.state === 'suspended') await actx.resume().catch(() => {});
        let at = actx.currentTime + 0.08;  // small cushion against network jitter
        for (;;) {
            if (mine !== gen) return;
            if (clip.chunks.length) {
                const b = clip.chunks.shift();
                const pcm = new Int16Array(b.buffer, b.byteOffset, b.length / 2);
                const buf = actx.createBuffer(1, pcm.length, clip.rate);
                const ch = buf.getChannelData(0);
                for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
                const src = actx.createBufferSource();
                src.buffer = buf;
                src.connect(actx.destination);
                at = Math.max(at, actx.currentTime + 0.02);
                src.start(at);
                at += buf.duration;
                sources.push(src);
                src.onended = () => { sources = sources.filter(x => x !== src); };
            } else if (clip.done) {
                break;
            } else {
                await wait();
            }
        }
        await new Promise(r => setTimeout(r, Math.max(0, (at - actx.currentTime) * 1000)));
    }

    function stop() {
        gen++;
        for (const q of queue) dropClip(q.clip);
        queue = [];
        if (audio) { audio.pause(); audio.src = ''; audio = null; }
        for (const src of sources) { try { src.stop(); } catch (_) { /* already ended */ } }
        if (sources.length || playing != null) quietUntil = Date.now() + TAIL_MS;
        sources = [];
        markPlaying(null);
    }

    function markPlaying(id) {
        document.querySelectorAll('.voice-speak-btn.playing').forEach(b => b.classList.remove('playing'));
        playing = id;
        if (id != null) document.querySelectorAll(`.voice-speak-btn[data-id="${id}"]`).forEach(b => b.classList.add('playing'));
        else document.querySelectorAll('.voice-part.playing').forEach(b => b.classList.remove('playing'));
        setState();
    }

    // Queue a message (or a streamed piece of one): split into sentences
    // Sentences render one at a time, back to back: each starts the moment the previous one is
    // rendered (not when it starts playing), so the voice server never idles and there are no gaps.
    // Never several at once: Chatterbox runs parallel requests interleaved and they all finish late.
    let current = null;  // the clip being played (may still be rendering)
    function prefetch() {
        if (current && !current.done) return;
        if (queue.some(q => q.clip && !q.clip.done)) return;
        const next = queue.find(q => !q.clip);
        if (next) next.clip = startClip(next.agent, next.text, next.id);
    }

    // texts: replay these sentences as they are. Without it the message is split now, and the
    // split is remembered: a replay asks for the same sentences, which the server has saved.
    const said = {};  // message id -> sentences as first read
    const partsOf = (msg) => said[msg.id] && said[msg.id].length ? said[msg.id] : sentences(msg.text || '');
    function enqueue(msg, texts) {
        const list = texts || sentences(msg.text || '');
        if (!texts) (said[msg.id] = said[msg.id] || []).push(...list);
        for (const text of list) queue.push({ agent: msg.sender, text, id: msg.id });
        prefetch();
        if (queue.length && !running) play();
    }

    async function play() {
        running = true;
        const mine = gen;
        while (queue.length && mine === gen) {
            const part = queue.shift();
            part.clip = part.clip || startClip(part.agent, part.text, part.id);
            current = part.clip;
            markPlaying(part.id);
            document.querySelectorAll('.voice-part').forEach(b => b.classList.toggle('playing', b.dataset.id === String(part.id) && b._text === part.text));
            spoken.push({ t: Date.now(), words: part.text.toLowerCase().match(/[a-z0-9']+/g) || [] });
            await playClip(part.clip, mine);
            if (mine === gen) quietUntil = Date.now() + TAIL_MS;
        }
        running = false;
        current = null;
        if (mine === gen) markPlaying(null);
        if (mine === gen && talking && namedMode())  // the follow-up window starts when the reply is heard
            post('/api/voice/played', { channel: activeChannel }).catch(() => {});
    }

    function toast(text) {
        if (typeof showToast === 'function') showToast(text, 'error'); else console.warn(text);
    }

    // Play button in each message's toolbar (control.js calls this)
    function button(msg) {
        if (!msg || !msg.text || !isAgent(msg.sender)) return null;
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'ctl-tool voice-speak-btn';
        b.dataset.id = msg.id;
        b.title = b.ariaLabel = 'Read aloud';
        b.innerHTML = ICON.speak;
        b.onclick = (e) => {
            e.stopPropagation();
            if (playing === msg.id) { stop(); return; }
            stop();
            enqueue(msg, partsOf(msg));
            showParts(b, msg);
        };
        return b;
    }

    // Download button in each message's toolbar: the whole message as one audio file, in the agent's voice
    function saveButton(msg) {
        if (!msg || !msg.text || !isAgent(msg.sender)) return null;
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'ctl-tool voice-save-btn';
        b.title = b.ariaLabel = 'Download as audio';
        b.innerHTML = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 2v8m0 0l-3-3m3 3l3-3M3 13h10"/></svg>';
        b.onclick = async (e) => {
            e.stopPropagation();
            b.disabled = true;
            try {
                const r = await fetch('/api/voice/speak', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Session-Token': token() },
                    body: JSON.stringify({ agent: msg.sender, text: msg.text, message_id: msg.id, file: true }) });
                if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `speech failed (${r.status})`);
                const name = (r.headers.get('Content-Disposition') || '').match(/filename="([^"]+)"/);
                const a = document.createElement('a');
                a.href = URL.createObjectURL(await r.blob());
                a.download = name ? name[1] : `${msg.sender}-${msg.id}.wav`;
                a.click();
                setTimeout(() => URL.revokeObjectURL(a.href), 30000);
                b.title = 'Download as audio';
            } catch (err) { b.title = `Download failed: ${err.message}`; }
            b.disabled = false;
        };
        return b;
    }

    // One small button per sentence under a message that was read aloud: replay just that one
    function showParts(btn, msg) {
        const bubble = btn.closest('.chat-bubble');
        const parts = partsOf(msg);
        if (!bubble || parts.length < 2 || bubble.querySelector('.voice-parts')) return;
        const row = document.createElement('div');
        row.className = 'voice-parts';
        row.setAttribute('role', 'group');
        row.setAttribute('aria-label', 'Replay one sentence');
        parts.forEach((text, i) => {
            const p = document.createElement('button');
            p.type = 'button';
            p.className = 'voice-part';
            p.dataset.id = msg.id;
            p._text = text;
            p.textContent = i + 1;
            p.title = text;
            p.setAttribute('aria-label', `Replay sentence ${i + 1}: ${text}`);
            p.onclick = (e) => { e.stopPropagation(); stop(); enqueue(msg, [text]); };
            row.appendChild(p);
        });
        bubble.appendChild(row);
    }

    // ---------------- Auto-speak (per channel, this browser) ----------------
    const autoKey = () => `agenthub-autospeak-${typeof activeChannel !== 'undefined' ? activeChannel : 'general'}`;
    const autoOn = () => store.get(autoKey(), false);

    const wanted = (agent) => autoOn() || (talking && (!target || agent.toLowerCase() === target));

    // The reply as the agent writes it (agent_runner streams it): speak each finished sentence
    // right away instead of waiting for the whole message.
    const streams = {};  // sid -> {base: text seen, done: chars already queued}
    Hub.on('agent_stream', (ev) => {
        if (ev.thread != null || (ev.channel || 'general') !== activeChannel || !ev.sid || !wanted(ev.agent || '')) return;
        const st = streams[ev.sid] || (streams[ev.sid] = { base: '', done: 0 });
        const text = ev.text || '';
        if (!text.startsWith(st.base.slice(0, st.done))) st.done = 0;  // a new segment (after a tool call)
        st.base = text;
        const rest = text.slice(st.done);
        const m = rest.match(/^[\s\S]*[.!?]["')\]]*\s+/);  // up to the last complete sentence
        if (!m || m[0].trim().length < 2) return;
        st.done += m[0].length;
        if (ev.agent.toLowerCase() === target) waiting = false;
        enqueue({ sender: ev.agent, text: m[0], id: ev.sid });
    });

    Hub.on('message', (ev) => {
        const m = ev.data;
        if (!m || m.type && m.type !== 'chat' || !m.text || m.thread != null) return;
        if ((m.channel || 'general') !== activeChannel || !isAgent(m.sender)) return;
        if (talking && target && m.sender.toLowerCase() === target) waiting = false;
        if (!wanted(m.sender) && !m.metadata?.echo) return;  // /echo lines are always heard
        const st = streams[m.metadata?.stream];
        if (st) {  // most of it was spoken while streaming: only the rest
            delete streams[m.metadata.stream];
            said[m.id] = said[m.metadata.stream] || [];  // the sentences read so far belong to this message
            delete said[m.metadata.stream];
            const done = st.base.slice(0, st.done);   // (named "said" it hid the map above, and the last sentence was never read)
            const rest = m.text.startsWith(done) ? m.text.slice(st.done) : (st.done ? '' : m.text);
            if (rest.trim()) enqueue({ sender: m.sender, text: rest, id: m.id });
            return;
        }
        enqueue(m);
    });

    // ---------------- Talking: hands-free and push-to-talk ----------------
    let talking = false;     // hands-free conversation on
    let ptt = false;         // push-to-talk held
    let target = '';         // agent the conversation is with
    let waiting = false;     // sent an utterance, reply not in yet
    let mic = null;          // {ctx, stream, node, rate}
    let frames = [], preroll = [], speaking = false, voiced = 0, silent = 0, floor = 0.01;
    let busy = false;        // transcribing
    const END_SILENCE_S = 0.6;  // this much quiet ends what you say

    async function openMic() {
        if (mic) return mic;
        if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
            throw new Error('The microphone needs the secure address (https://…:8788).');
        }
        const stream = await navigator.mediaDevices.getUserMedia({ audio: {
            echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const src = ctx.createMediaStreamSource(stream);
        const node = ctx.createScriptProcessor(2048, 1, 1);
        node.onaudioprocess = (e) => onFrame(new Float32Array(e.inputBuffer.getChannelData(0)), ctx.sampleRate);
        src.connect(node);
        node.connect(ctx.destination);  // ScriptProcessor only runs when connected; it outputs silence
        mic = { ctx, stream, node, rate: ctx.sampleRate };
        return mic;
    }

    function closeMic() {
        if (!mic) return;
        mic.node.disconnect();
        mic.stream.getTracks().forEach(t => t.stop());
        mic.ctx.close();
        mic = null;
    }

    // Voice activity detection on signal level, with an adaptive noise floor.
    // ~43 ms frames at 48 kHz: speech starts after 4 loud frames, ends after END_SILENCE_S of quiet.
    function onFrame(f, rate) {
        if (ptt) { frames.push(f); return; }
        if (!talking || busy) return;
        if (speakingNow() && !bargeIn() && !speaking) { voiced = 0; preroll = []; return; }  // half duplex: no self-hearing
        let sum = 0;
        for (let i = 0; i < f.length; i++) sum += f[i] * f[i];
        const rms = Math.sqrt(sum / f.length);
        const gate = Math.max(0.012, floor * 3) * (playing != null ? 2 : 1);  // stricter while a voice plays (echo)
        const loud = rms > gate;
        if (!speaking) {
            floor = 0.95 * floor + 0.05 * Math.min(rms, 0.05);
            preroll.push(f);
            if (preroll.length > 8) preroll.shift();
            voiced = loud ? voiced + 1 : 0;
            if (voiced >= 4) {
                speaking = true;
                silent = 0;
                frames = preroll.slice();
                preroll = [];
                if (playing != null) stop();  // barge-in: the owner talks over the agent
                setState();
            }
            return;
        }
        frames.push(f);
        silent = loud ? 0 : silent + 1;
        const secs = frames.length * f.length / rate;
        if (silent * f.length / rate > END_SILENCE_S || secs > 30) {
            speaking = false;
            const take = frames;
            frames = [];
            if (secs - silent * f.length / rate > 0.35) finish(take, rate);
            setState();
        }
    }

    function wav(chunks, rate) {
        // Downsample to 16 kHz mono 16-bit: what Whisper wants, and a quarter of the upload
        const total = chunks.reduce((n, c) => n + c.length, 0);
        const ratio = rate / 16000, n = Math.floor(total / ratio);
        const buf = new ArrayBuffer(44 + n * 2), dv = new DataView(buf);
        const all = new Float32Array(total);
        let o = 0;
        for (const c of chunks) { all.set(c, o); o += c.length; }
        const w = (p, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(p + i, s.charCodeAt(i)); };
        w(0, 'RIFF'); dv.setUint32(4, 36 + n * 2, true); w(8, 'WAVE'); w(12, 'fmt ');
        dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
        dv.setUint32(24, 16000, true); dv.setUint32(28, 32000, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
        w(36, 'data'); dv.setUint32(40, n * 2, true);
        for (let i = 0; i < n; i++) {
            const s = Math.max(-1, Math.min(1, all[Math.floor(i * ratio)]));
            dv.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
        }
        return new Blob([buf], { type: 'audio/wav' });
    }

    async function finish(chunks, rate) {
        busy = true;
        setState();
        try {
            const r = await post('/api/voice/transcribe', wav(chunks, rate), 'audio/wav');
            const data = await r.json();
            if (!r.ok) throw new Error(data.error || 'transcription failed');
            const text = (data.text || '').trim();
            if (!text || /^[\[(].*[\])]$/.test(text)) return;  // [BLANK_AUDIO], (noise)
            if (isEcho(text)) return;  // the mic heard the agent's own voice
            if (talking && namedMode()) {  // the server decides; it posts accepted speech itself
                const g = await (await post('/api/voice/utterance', { text, channel: activeChannel, sender: username, heard: data.heard })).json();
                if (g.error) throw new Error(g.error);
                if (g.accepted) { target = g.to; waiting = true; heard = ''; }
                else if (g.reason === 'stopped') { stop(); waiting = false; heard = `Stopped @${g.to}`; }
                else heard = g.notice || (g.reason === 'decision_unavailable' ? 'Voice check unavailable'
                    : g.open ? `Ignored (talking with @${g.open})` : 'Ignored: not said to an agent');
                if (g.notice) toast(g.notice);
                if (!g.accepted && g.open) target = g.open;
                return;
            }
            if (target && STOP_WORDS.test(text)) {
                stop();
                await post('/api/agents/stop', { name: target, channel: activeChannel });
                return;
            }
            const out = target && !/(^|\s)@[\w-]/.test(text) ? `@${target} ${text}` : text;
            if (typeof ws !== 'undefined' && ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'message', text: out, sender: username, channel: activeChannel, attachments: [] }));
                waiting = !!target;
            }
        } catch (err) {
            toast(err.message);
        } finally {
            busy = false;
            setState();
        }
    }

    async function startTalking() {
        try { await openMic(); } catch (err) { toast(err.message); return; }
        talking = true;
        speaking = false; voiced = 0; preroll = [];
        setState();
    }

    function stopTalking() {
        talking = false;
        speaking = false;
        if (!ptt) closeMic();
        stop();
        setState();
    }

    async function pttDown() {
        try { await openMic(); } catch (err) { toast(err.message); return; }
        stop();
        frames = [];
        ptt = true;
        setState();
    }

    function pttUp() {
        if (!ptt) return;
        ptt = false;
        const take = frames;
        frames = [];
        if (mic && take.length) finish(take, mic.rate);
        if (!talking) closeMic();
        setState();
    }

    // ---------------- Controls (next to the mic) ----------------
    let ui = null;

    function agentsHere() {
        if (typeof agentConfig === 'undefined') return [];
        return Object.keys(agentConfig).filter(n => typeof agentInActiveChannel !== 'function' || agentInActiveChannel(n)).sort();
    }

    function setState() {
        if (!ui) return;
        const state = ptt ? 'recording' : busy ? 'thinking' : speaking ? 'hearing' : playing != null ? 'speaking'
            : talking ? (waiting ? 'waiting' : 'listening') : 'off';
        ui.talk.dataset.state = state;
        ui.talk.setAttribute('aria-pressed', String(talking));
        ui.status.textContent = { off: '', listening: heard ? `Listening… (${heard})` : (namedMode() ? 'Listening for a name…' : 'Listening…'), hearing: 'Hearing you…', thinking: 'Transcribing…',
            waiting: `Waiting for ${target || 'a reply'}…`, speaking: 'Speaking… (talk to interrupt)', recording: 'Recording… release to send' }[state];
        ui.bar.hidden = !talking && !ptt && playing == null;
        ui.auto.setAttribute('aria-pressed', String(autoOn()));
        ui.auto.title = autoOn() ? 'Auto-speak is on in this channel' : 'Read new agent replies aloud in this channel';
    }

    function fillTargets() {
        const names = agentsHere();
        const keep = target || store.get('agenthub-voice-target', '');
        ui.target.innerHTML = '<option value="">Anyone (use @name)</option>'
            + names.map(n => `<option value="${escapeHtml(n)}">@${escapeHtml(n)}</option>`).join('');
        ui.target.value = names.includes(keep) ? keep : '';
        target = ui.target.value;
    }

    function mount() {
        const micBtn = document.getElementById('mic');
        if (!micBtn || ui) return;
        const talk = document.createElement('button');
        talk.type = 'button';
        talk.id = 'voice-talk';
        talk.className = 'voice-talk';
        talk.innerHTML = ICON.talk;
        talk.title = 'Talk: click for a hands-free conversation, hold to push-to-talk';
        talk.setAttribute('aria-label', 'Talk to agents');
        const auto = document.createElement('button');
        auto.type = 'button';
        auto.className = 'voice-auto';
        auto.innerHTML = ICON.speak;
        auto.setAttribute('aria-label', 'Auto-speak in this channel');
        micBtn.after(auto, talk);

        const bar = document.createElement('div');
        bar.className = 'voice-bar';
        bar.hidden = true;
        bar.innerHTML = '<span class="voice-dot" aria-hidden="true"></span><span class="voice-status" role="status" aria-live="polite"></span>'
            + '<label class="voice-target-label">Talking to <select class="voice-target" aria-label="Agent to talk to"></select></label>'
            + '<button type="button" class="voice-hush" title="Stop reading aloud">Stop voice</button>'
            + '<button type="button" class="voice-end" title="End the conversation">End</button>';
        const composer = document.getElementById('input-row') || micBtn.parentElement;
        composer.parentElement.insertBefore(bar, composer);
        ui = { talk, auto, bar, status: bar.querySelector('.voice-status'), target: bar.querySelector('.voice-target') };
        ui.target.onchange = () => { target = ui.target.value; store.set('agenthub-voice-target', target); };
        bar.querySelector('.voice-hush').onclick = stop;
        bar.querySelector('.voice-end').onclick = stopTalking;
        auto.onclick = () => { store.set(autoKey(), !autoOn()); if (!autoOn()) stop(); setState(); };

        // Click toggles hands-free; holding (> 350 ms) is push-to-talk
        let held = null, longPress = false;
        talk.addEventListener('pointerdown', (e) => {
            e.preventDefault();
            longPress = false;
            held = setTimeout(() => { longPress = true; pttDown(); }, 350);
        });
        const release = () => {
            clearTimeout(held);
            if (longPress) { pttUp(); return; }
        };
        talk.addEventListener('pointerup', release);
        talk.addEventListener('pointerleave', () => { if (longPress) pttUp(); clearTimeout(held); });
        talk.addEventListener('click', () => {
            if (longPress) { longPress = false; return; }
            if (talking) stopTalking(); else { fillTargets(); startTalking(); }
        });
        Hub.on('status', () => { if (ui && !talking) fillTargets(); });
        fillTargets();
        setState();
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount); else mount();
    window.addEventListener('agenthub-channel', () => { stop(); if (ui) { fillTargets(); setState(); } });

    return { button, saveButton, stop, speak: enqueue, sentences, speakingNow, isEcho, bargeIn };
})();
window.Voice = Voice;
