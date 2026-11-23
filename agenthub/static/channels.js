// channels.js -- Channel tabs, switching, filtering, CRUD
// Extracted from chat.js PR 4.  Reads shared state via window.* bridges.

'use strict';

// ---------------------------------------------------------------------------
// State (local to channels)
// ---------------------------------------------------------------------------


// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function _getTopVisibleMsgId() {
    const scroll = document.getElementById('timeline');
    const container = document.getElementById('messages');
    if (!scroll || !container) return null;
    const rect = scroll.getBoundingClientRect();
    for (const el of container.children) {
        if (el.style.display === 'none' || !el.dataset.id) continue;
        const elRect = el.getBoundingClientRect();
        if (elRect.bottom > rect.top) return el.dataset.id;
    }
    return null;
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

function renderChannelTabs() {
    const container = document.getElementById('channel-tabs');
    if (!container) return;

    // Preserve inline create input if it exists
    const existingCreate = container.querySelector('.channel-inline-create');
    const scrollLeft = container.scrollLeft;
    container.innerHTML = '';

    for (const name of window.channelList) {
        const tab = document.createElement('button');
        tab.className = 'channel-tab' + (name === window.activeChannel ? ' active' : '');
        tab.dataset.channel = name;

        const label = document.createElement('span');
        label.className = 'channel-tab-label';
        label.textContent = '# ' + name;
        tab.appendChild(label);

        const unread = window.channelUnread[name] || 0;
        if (unread > 0 && name !== window.activeChannel) {
            const dot = document.createElement('span');
            dot.className = 'channel-unread-dot';
            dot.textContent = unread > 99 ? '99+' : unread;
            tab.appendChild(dot);
        }

        // Edit + delete icons for non-general tabs (visible on hover via CSS)
        if (name !== 'general') {
            const actions = document.createElement('span');
            actions.className = 'channel-tab-actions';

            const editBtn = document.createElement('button');
            editBtn.className = 'ch-edit-btn';
            editBtn.title = 'Rename';
            editBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M11.5 2.5l2 2L5 13H3v-2L11.5 2.5z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>';
            editBtn.onclick = (e) => { e.stopPropagation(); showChannelRenameDialog(name); };
            actions.appendChild(editBtn);

            const delBtn = document.createElement('button');
            delBtn.className = 'ch-delete-btn';
            delBtn.title = 'Delete';
            delBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M3 4h10M6 4V3h4v1M5 4v8.5h6V4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>';
            delBtn.onclick = (e) => { e.stopPropagation(); deleteChannel(name); };
            actions.appendChild(delBtn);

            tab.appendChild(actions);
        }

        tab.onclick = (e) => {
            if (e.target.closest('.channel-tab-actions')) return;
            if (name === window.activeChannel) {
                // Second click on active tab -- toggle edit controls
                tab.classList.toggle('editing');
            } else {
                // Clear any editing state, switch channel
                document.querySelectorAll('.channel-tab.editing').forEach(t => t.classList.remove('editing'));
                switchChannel(name);
            }
        };

        container.appendChild(tab);
    }

    // Re-append inline create if it was open
    if (existingCreate) {
        container.appendChild(existingCreate);
    }

    container.scrollLeft = scrollLeft;
    renderChannelSidebar();
}

// ---------------------------------------------------------------------------
// Sidebar (Discord/Slack-style vertical list)
// ---------------------------------------------------------------------------

function renderChannelSidebar() {
    const list = document.getElementById('channel-sidebar-list');
    if (!list) return;

    // Preserve inline create/rename if present
    const existingCreate = list.querySelector('.channel-inline-create');
    list.innerHTML = '';

    // Long lists get a filter box
    const filter = document.getElementById('channel-filter');
    const many = window.channelList.length > CHANNEL_FILTER_MIN;
    if (filter) {
        filter.classList.toggle('hidden', !many);
        if (!many) filter.value = '';
    }
    const query = filter ? filter.value.trim().toLowerCase() : '';
    let shown = 0, unreadTotal = 0;

    // One row; text: what it shows (a Discord channel shows its Discord name), left: no longer followed
    const addRow = (name, text, left, into = list) => {
        const row = document.createElement('button');
        row.className = 'channel-sidebar-row' + (name === window.activeChannel ? ' active' : '');
        row.dataset.channel = name;
        if (name === window.activeChannel) row.setAttribute('aria-current', 'page');
        row.hidden = !!query && !name.includes(query) && !text.toLowerCase().includes(query);
        if (!row.hidden) shown++;

        const label = document.createElement('span');
        label.className = 'channel-sidebar-row-label';
        label.textContent = text;
        if (left) { row.classList.add('left'); row.title = 'Left in Discord: /join to follow it again'; }
        row.appendChild(label);

        const unread = window.channelUnread[name] || 0;
        if (unread > 0 && name !== window.activeChannel) {
            unreadTotal += unread;
            const dot = document.createElement('span');
            dot.className = 'channel-sidebar-row-unread';
            dot.textContent = unread > 99 ? '99+' : unread;
            row.appendChild(dot);
        }

        if (name !== 'general') {
            const actions = document.createElement('span');
            actions.className = 'channel-sidebar-row-actions';

            const editBtn = document.createElement('button');
            editBtn.title = 'Rename';
            editBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M11.5 2.5l2 2L5 13H3v-2L11.5 2.5z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>';
            editBtn.onclick = (e) => { e.stopPropagation(); _showSidebarRenameDialog(name); };
            actions.appendChild(editBtn);

            const delBtn = document.createElement('button');
            delBtn.title = 'Delete';
            delBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M3 4h10M6 4V3h4v1M5 4v8.5h6V4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>';
            delBtn.onclick = (e) => { e.stopPropagation(); _sidebarConfirmDelete(name, row, label); };
            actions.appendChild(delBtn);

            row.appendChild(actions);
        }

        row.onclick = (e) => {
            if (e.target.closest('.channel-sidebar-row-actions')) return;
            if (name !== window.activeChannel) switchChannel(name);
        };

        into.appendChild(row);
        return row;
    };

    // Discord servers (discord_gateway.py): each its own section after Channels, named after the server
    const groups = window.channelGroups || [];
    const grouped = new Set(groups.flatMap(g => g.channels.map(c => c.name)));
    for (const name of window.channelList) if (!grouped.has(name)) addRow(name, '# ' + name, false);
    document.querySelectorAll('#channel-sidebar .nav-group.discord-server').forEach(n => n.remove());
    let after = list.closest('.nav-group');
    for (const g of groups) {
        const chans = g.channels.filter(c => window.channelList.includes(c.name));
        if (!chans.length || !after) continue;
        const section = document.createElement('section');
        section.className = 'nav-group discord-server';
        section.dataset.group = 'discord-' + g.guild;
        const toggle = document.createElement('button');
        toggle.type = 'button';
        toggle.className = 'nav-group-toggle';
        toggle.title = g.title + ' (Discord server)';
        const title = document.createElement('span');
        title.className = 'nav-group-title';
        title.textContent = g.title;
        toggle.appendChild(title);
        const head = document.createElement('div');
        head.className = 'nav-group-head';
        head.appendChild(toggle);
        const body = document.createElement('div');
        body.className = 'nav-group-body';
        section.append(head, body);
        const rows = chans.map(c => addRow(c.name, c.label.startsWith('voice ') ? '🔊 ' + c.label.slice(6) : '# ' + c.label.replace(/^#/, ''), c.left, body));
        section.hidden = rows.every(r => r.hidden);
        after.after(section);
        after = section;
        let open = true;
        try { const v = localStorage.getItem('agenthub-group-nav-' + section.dataset.group); if (v !== null) open = v === '1'; } catch (e) { /* private mode */ }
        _setNavGroupOpen(section, open, false);
        toggle.addEventListener('click', () => _setNavGroupOpen(section, section.classList.contains('collapsed')));
    }

    if (query && !shown) {
        const none = document.createElement('div');
        none.className = 'nav-empty';
        none.textContent = 'No matching channels';
        list.appendChild(none);
    }
    if (existingCreate) list.appendChild(existingCreate);

    // Shown on the Channels heading while that group is collapsed
    const total = document.getElementById('nav-channels-unread');
    if (total) {
        total.textContent = unreadTotal > 99 ? '99+' : unreadTotal;
        total.classList.toggle('hidden', !unreadTotal);
    }
}

function _showSidebarRenameDialog(oldName) {
    const list = document.getElementById('channel-sidebar-list');
    if (!list) return;
    list.querySelector('.channel-inline-create')?.remove();

    const targetRow = list.querySelector(`.channel-sidebar-row[data-channel="${oldName}"]`);

    const wrapper = document.createElement('div');
    wrapper.className = 'channel-inline-create';

    const prefix = document.createElement('span');
    prefix.className = 'channel-input-prefix';
    prefix.textContent = '#';
    wrapper.appendChild(prefix);

    const input = document.createElement('input');
    input.type = 'text';
    input.maxLength = 20;
    input.value = oldName;
    wrapper.appendChild(input);

    const cleanup = () => { wrapper.remove(); if (targetRow) targetRow.style.display = ''; };

    const confirm = document.createElement('button');
    confirm.className = 'confirm-btn';
    confirm.innerHTML = '&#10003;';
    confirm.title = 'Rename';
    confirm.onclick = () => {
        const newName = input.value.trim().toLowerCase();
        if (!newName || !/^[a-z0-9][a-z0-9\-]{0,19}$/.test(newName)) return;
        if (newName !== oldName) {
            window.ws.send(JSON.stringify({ type: 'channel_rename', old_name: oldName, new_name: newName }));
            if (window.activeChannel === oldName) {
                window._setActiveChannel(newName);
                localStorage.setItem('agentchattr-channel', newName);
                Store.set('activeChannel', newName);
            }
        }
        cleanup();
    };
    wrapper.appendChild(confirm);

    const cancel = document.createElement('button');
    cancel.className = 'cancel-btn';
    cancel.innerHTML = '&#10005;';
    cancel.title = 'Cancel';
    cancel.onclick = cleanup;
    wrapper.appendChild(cancel);

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); confirm.click(); }
        if (e.key === 'Escape') { e.stopPropagation(); cleanup(); }  // cancel the input only, keep the drawer open
    });
    input.addEventListener('input', () => {
        input.value = input.value.toLowerCase().replace(/[^a-z0-9\-]/g, '');
    });

    if (targetRow) {
        targetRow.style.display = 'none';
        targetRow.insertAdjacentElement('afterend', wrapper);
    } else {
        list.appendChild(wrapper);
    }
    input.select();
}

function _sidebarConfirmDelete(name, row, label) {
    if (name === 'general' || row.classList.contains('confirm-delete')) return;
    const actions = row.querySelector('.channel-sidebar-row-actions');
    const originalText = label.textContent;
    const originalOnclick = row.onclick;

    row.classList.add('confirm-delete');
    label.textContent = `delete #${name}?`;
    if (actions) actions.style.display = 'none';

    const confirmBar = document.createElement('span');
    confirmBar.className = 'channel-sidebar-row-actions';
    confirmBar.style.display = 'flex';

    const tickBtn = document.createElement('button');
    tickBtn.title = 'Confirm delete';
    tickBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M3 8.5l3.5 3.5 6.5-7" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';

    const crossBtn = document.createElement('button');
    crossBtn.title = 'Cancel';
    crossBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';

    confirmBar.appendChild(tickBtn);
    confirmBar.appendChild(crossBtn);
    row.appendChild(confirmBar);

    const revert = () => {
        row.classList.remove('confirm-delete');
        label.textContent = originalText;
        if (actions) actions.style.display = '';
        confirmBar.remove();
        row.onclick = originalOnclick;
        document.removeEventListener('click', outsideClick);
    };

    tickBtn.onclick = (e) => {
        e.stopPropagation();
        revert();
        window.ws.send(JSON.stringify({ type: 'channel_delete', name }));
        if (window.activeChannel === name) switchChannel('general');
    };
    crossBtn.onclick = (e) => { e.stopPropagation(); revert(); };
    row.onclick = (e) => { e.stopPropagation(); };

    const outsideClick = (e) => { if (!row.contains(e.target)) revert(); };
    setTimeout(() => document.addEventListener('click', outsideClick), 0);
}

// ---------------------------------------------------------------------------
// Switch / filter
// ---------------------------------------------------------------------------

function switchChannel(name) {
    if (name === window.activeChannel) return;
    const prevChannel = window.activeChannel;
    // Each channel keeps its own unsent text: a draft never follows you to another channel
    const box = document.getElementById('input');
    if (box) {
        const drafts = window._channelDrafts || (window._channelDrafts = {});
        drafts[prevChannel] = box.value;
        box.value = drafts[name] || '';
        box.dispatchEvent(new Event('input', { bubbles: true }));  // resize, slash/mention menus
    }
    window._setActiveChannel(name);
    // Swap the sticky @-mention toggles to this channel's remembered set
    if (window._onChannelSwitchMentions) window._onChannelSwitchMentions(prevChannel, name);
    window.channelUnread[name] = 0;
    localStorage.setItem('agentchattr-channel', name);
    filterMessagesByChannel();
    renderChannelTabs();
    if (window.renderMembers) window.renderMembers();  // member list is per channel
    if (window.refreshWorking) window.refreshWorking();  // the working/Stop bar is per channel too
    Store.set('activeChannel', name);
    document.querySelector('#channel-tabs .channel-tab.active')?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    // Always land on the newest message, instantly (the timeline's CSS smooth scroll
    // would otherwise animate through the whole channel)
    window.scrollToBottom(true);
    if (typeof dayFloatRefresh === 'function') dayFloatRefresh();
}

function filterMessagesByChannel() {
    const container = document.getElementById('messages');
    if (!container) return;

    for (const el of container.children) {
        const ch = el.dataset.channel || 'general';
        el.style.display = ch === window.activeChannel ? '' : 'none';
    }
}

// ---------------------------------------------------------------------------
// Create
// ---------------------------------------------------------------------------

function showChannelCreateDialog(fromSidebar) {
    // Route the inline create into the sidebar list when sidebar mode is on,
    // otherwise into the top-bar tabs — keeps the input visible either way.
    // Each add button says where it sits (on a phone the sidebar is a drawer in both modes).
    const inSidebar = typeof fromSidebar === 'boolean' ? fromSidebar
        : document.body.classList.contains('channels-in-sidebar');
    if (inSidebar) _setNavGroupOpen(document.querySelector('.nav-group[data-group="channels"]'), true);
    const tabs = inSidebar
        ? document.getElementById('channel-sidebar-list')
        : document.getElementById('channel-tabs');
    // Remove existing inline create if any
    tabs.querySelector('.channel-inline-create')?.remove();

    // Hide the + button while creating
    const addBtn = inSidebar
        ? document.getElementById('channel-sidebar-add')
        : document.getElementById('channel-add-btn');
    if (addBtn) addBtn.style.display = 'none';

    const wrapper = document.createElement('div');
    wrapper.className = 'channel-inline-create';

    const prefix = document.createElement('span');
    prefix.className = 'channel-input-prefix';
    prefix.textContent = '#';
    wrapper.appendChild(prefix);

    const input = document.createElement('input');
    input.type = 'text';
    input.maxLength = 20;
    input.placeholder = 'channel-name';
    wrapper.appendChild(input);

    const cleanup = () => { wrapper.remove(); if (addBtn) addBtn.style.display = ''; };

    const confirm = document.createElement('button');
    confirm.className = 'confirm-btn';
    confirm.innerHTML = '&#10003;';
    confirm.title = 'Create';
    confirm.onclick = () => { _submitInlineCreate(input, wrapper); if (addBtn) addBtn.style.display = ''; };
    wrapper.appendChild(confirm);

    const cancel = document.createElement('button');
    cancel.className = 'cancel-btn';
    cancel.innerHTML = '&#10005;';
    cancel.title = 'Cancel';
    cancel.onclick = cleanup;
    wrapper.appendChild(cancel);

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); _submitInlineCreate(input, wrapper); if (addBtn) addBtn.style.display = ''; }
        if (e.key === 'Escape') { e.stopPropagation(); cleanup(); }  // cancel the input only, keep the drawer open
    });
    input.addEventListener('input', () => {
        input.value = input.value.toLowerCase().replace(/[^a-z0-9\-]/g, '');
    });

    tabs.appendChild(wrapper);
    input.focus();
}

function _submitInlineCreate(input, wrapper) {
    const name = input.value.trim().toLowerCase();
    if (!name || !/^[a-z0-9][a-z0-9\-]{0,19}$/.test(name)) return;
    if (window.channelList.includes(name)) { input.focus(); return; }
    window._setPendingChannelSwitch(name);
    window.ws.send(JSON.stringify({ type: 'channel_create', name }));
    wrapper.remove();
}

// ---------------------------------------------------------------------------
// Rename
// ---------------------------------------------------------------------------

function showChannelRenameDialog(oldName) {
    const tabs = document.getElementById('channel-tabs');
    tabs.querySelector('.channel-inline-create')?.remove();

    // Find the tab being renamed so we can insert the input in its place
    const targetTab = tabs.querySelector(`.channel-tab[data-channel="${oldName}"]`);

    const wrapper = document.createElement('div');
    wrapper.className = 'channel-inline-create';

    const prefix = document.createElement('span');
    prefix.className = 'channel-input-prefix';
    prefix.textContent = '#';
    wrapper.appendChild(prefix);

    const input = document.createElement('input');
    input.type = 'text';
    input.maxLength = 20;
    input.value = oldName;
    wrapper.appendChild(input);

    const cleanup = () => {
        wrapper.remove();
        if (targetTab) targetTab.style.display = '';
    };

    const confirm = document.createElement('button');
    confirm.className = 'confirm-btn';
    confirm.innerHTML = '&#10003;';
    confirm.title = 'Rename';
    confirm.onclick = () => {
        const newName = input.value.trim().toLowerCase();
        if (!newName || !/^[a-z0-9][a-z0-9\-]{0,19}$/.test(newName)) return;
        if (newName !== oldName) {
            window.ws.send(JSON.stringify({ type: 'channel_rename', old_name: oldName, new_name: newName }));
            if (window.activeChannel === oldName) {
                window._setActiveChannel(newName);
                localStorage.setItem('agentchattr-channel', newName);
                Store.set('activeChannel', newName);
            }
        }
        cleanup();
    };
    wrapper.appendChild(confirm);

    const cancel = document.createElement('button');
    cancel.className = 'cancel-btn';
    cancel.innerHTML = '&#10005;';
    cancel.title = 'Cancel';
    cancel.onclick = cleanup;
    wrapper.appendChild(cancel);

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); confirm.click(); }
        if (e.key === 'Escape') { e.stopPropagation(); cleanup(); }  // cancel the input only, keep the drawer open
    });
    input.addEventListener('input', () => {
        input.value = input.value.toLowerCase().replace(/[^a-z0-9\-]/g, '');
    });

    // Insert inline next to the tab, hide the original tab
    if (targetTab) {
        targetTab.style.display = 'none';
        targetTab.insertAdjacentElement('afterend', wrapper);
    } else {
        tabs.appendChild(wrapper);
    }
    input.select();
}

// ---------------------------------------------------------------------------
// Delete
// ---------------------------------------------------------------------------

function deleteChannel(name) {
    if (name === 'general') return;
    const tab = document.querySelector(`.channel-tab[data-channel="${name}"]`);
    if (!tab || tab.classList.contains('confirm-delete')) return;

    const label = tab.querySelector('.channel-tab-label');
    const actions = tab.querySelector('.channel-tab-actions');
    const originalText = label.textContent;
    const originalOnclick = tab.onclick;

    tab.classList.add('confirm-delete');
    tab.classList.remove('editing');
    label.textContent = `delete #${name}?`;
    if (actions) actions.style.display = 'none';

    const confirmBar = document.createElement('span');
    confirmBar.className = 'channel-delete-confirm';

    const tickBtn = document.createElement('button');
    tickBtn.className = 'ch-confirm-yes';
    tickBtn.title = 'Confirm delete';
    tickBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M3 8.5l3.5 3.5 6.5-7" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';

    const crossBtn = document.createElement('button');
    crossBtn.className = 'ch-confirm-no';
    crossBtn.title = 'Cancel';
    crossBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';

    confirmBar.appendChild(tickBtn);
    confirmBar.appendChild(crossBtn);
    tab.appendChild(confirmBar);

    const revert = () => {
        tab.classList.remove('confirm-delete');
        label.textContent = originalText;
        if (actions) actions.style.display = '';
        confirmBar.remove();
        tab.onclick = originalOnclick;
        document.removeEventListener('click', outsideClick);
    };

    tickBtn.onclick = (e) => {
        e.stopPropagation();
        revert();
        window.ws.send(JSON.stringify({ type: 'channel_delete', name }));
        if (window.activeChannel === name) switchChannel('general');
    };

    crossBtn.onclick = (e) => {
        e.stopPropagation();
        revert();
    };

    tab.onclick = (e) => { e.stopPropagation(); };

    const outsideClick = (e) => {
        if (!tab.contains(e.target)) revert();
    };
    setTimeout(() => document.addEventListener('click', outsideClick), 0);
}

// ---------------------------------------------------------------------------
// Sidebar mode toggle + resize grip
// ---------------------------------------------------------------------------

const SIDEBAR_MODE_KEY = 'agentchattr-channel-sidebar-mode';
const SIDEBAR_WIDTH_KEY = 'agentchattr-channel-sidebar-w';

function setChannelSidebarMode(mode, persist = true) {
    const sidebar = document.getElementById('channel-sidebar');
    const top = document.getElementById('channel-sidebar-top');
    const updatePill = document.getElementById('update-pill');
    if (!sidebar) return;

    const on = mode === 'sidebar';
    document.body.classList.toggle('channels-in-sidebar', on);
    sidebar.classList.toggle('hidden', !on);

    // The update pill sits at the top of the sidebar when it is on, in the channel bar otherwise
    const rightBar = document.querySelector('#channel-bar .channel-bar-right');
    const pillHome = on ? top : rightBar;
    if (updatePill && pillHome && updatePill.parentElement !== pillHome) pillHome.appendChild(updatePill);

    if (persist) localStorage.setItem(SIDEBAR_MODE_KEY, mode);
    const setting = document.getElementById('setting-channel-sidebar');
    if (setting && setting.value !== mode) setting.value = mode;

    if (on) renderChannelSidebar();
    _placeNavTools();
}

// ---------------------------------------------------------------------------
// Navigation: tools (Jobs, Rules, Pinned, Scheduled, Settings, Help) and groups
// ---------------------------------------------------------------------------

const CHANNEL_FILTER_MIN = 10;   // the filter box appears above this many channels
const _navNarrow = window.matchMedia('(max-width: 900px)');   // same breakpoint as the drawers in style.css

// One set of tool buttons (other scripts find them by id). They live in the sidebar;
// in Top bar mode on a wide screen there is no sidebar, so they move to the header.
function _placeNavTools() {
    const workspace = document.getElementById('nav-workspace-tools');
    const app = document.getElementById('nav-app-tools');
    if (!workspace || !app) return;
    const inSidebar = document.body.classList.contains('channels-in-sidebar') || _navNarrow.matches;
    const homes = inSidebar
        ? [document.getElementById('nav-workspace-body'), document.getElementById('nav-bottom')]
        : [document.getElementById('header-tools'), document.getElementById('header-tools')];
    if (homes[0] && workspace.parentElement !== homes[0]) homes[0].appendChild(workspace);
    if (homes[1] && app.parentElement !== homes[1]) homes[1].appendChild(app);
}

function _setNavGroupOpen(group, open, persist = true) {
    if (!group) return;
    group.classList.toggle('collapsed', !open);
    group.querySelector('.nav-group-toggle')?.setAttribute('aria-expanded', String(open));
    if (persist) { try { localStorage.setItem('agenthub-group-nav-' + group.dataset.group, open ? '1' : '0'); } catch (e) { /* private mode */ } }
}

function _setupNav() {
    // Groups expand and collapse; the state is remembered in this browser
    document.querySelectorAll('#channel-sidebar .nav-group').forEach((group) => {
        let open = true;
        try { const v = localStorage.getItem('agenthub-group-nav-' + group.dataset.group); if (v !== null) open = v === '1'; } catch (e) { /* private mode */ }
        _setNavGroupOpen(group, open, false);
        group.querySelector('.nav-group-toggle')?.addEventListener('click', () => {
            _setNavGroupOpen(group, group.classList.contains('collapsed'));
        });
    });

    const filter = document.getElementById('channel-filter');
    if (filter) {
        filter.addEventListener('input', renderChannelSidebar);
        filter.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && filter.value) { e.stopPropagation(); filter.value = ''; renderChannelSidebar(); }
        });
    }

    // jobs.js, rules-panel.js and chat.js mark an open panel with .active on its button:
    // mirror that to aria-pressed
    const mirror = new MutationObserver((changes) => {
        for (const c of changes) c.target.setAttribute('aria-pressed', String(c.target.classList.contains('active')));
    });
    for (const id of ['jobs-toggle', 'rules-toggle', 'pins-toggle']) {
        const btn = document.getElementById(id);
        if (btn) mirror.observe(btn, { attributes: true, attributeFilter: ['class'] });
    }

    _navNarrow.addEventListener('change', _placeNavTools);
}

function setupChannelSidebarGrip() {
    const grip = document.getElementById('channel-sidebar-grip');
    const panel = document.getElementById('channel-sidebar');
    if (!grip || !panel) return;

    let dragging = false;
    let startX = 0;
    let startWidth = 0;

    grip.addEventListener('mousedown', (e) => {
        e.preventDefault();
        dragging = true;
        startX = e.clientX;
        startWidth = panel.offsetWidth;
        grip.classList.add('dragging');
        panel.style.transition = 'none';
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';
    });

    document.addEventListener('mousemove', (e) => {
        if (!dragging) return;
        // Sidebar is on the left, so dragging right grows it (positive delta).
        const delta = e.clientX - startX;
        const newWidth = Math.min(Math.max(startWidth + delta, 140), 400);
        panel.style.setProperty('--channel-sidebar-w', newWidth + 'px');
        panel.style.width = newWidth + 'px';
    });

    document.addEventListener('mouseup', () => {
        if (!dragging) return;
        dragging = false;
        grip.classList.remove('dragging');
        panel.style.transition = '';
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        localStorage.setItem(SIDEBAR_WIDTH_KEY, panel.offsetWidth);
    });
}

function _restoreSidebarState() {
    const savedWidth = parseInt(localStorage.getItem(SIDEBAR_WIDTH_KEY) || '', 10);
    if (savedWidth && savedWidth >= 140 && savedWidth <= 400) {
        const panel = document.getElementById('channel-sidebar');
        if (panel) {
            panel.style.setProperty('--channel-sidebar-w', savedWidth + 'px');
            panel.style.width = savedWidth + 'px';
        }
    }
    const savedMode = localStorage.getItem(SIDEBAR_MODE_KEY) || 'sidebar';
    setChannelSidebarMode(savedMode, false);
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

function _channelsInit() {
    setupChannelSidebarGrip();
    _setupNav();
    _restoreSidebarState();

    const setting = document.getElementById('setting-channel-sidebar');
    if (setting) {
        setting.addEventListener('change', () => setChannelSidebarMode(setting.value));
    }
}

// ---------------------------------------------------------------------------
// Window exports (for inline onclick in index.html and chat.js callers)
// ---------------------------------------------------------------------------

window.showChannelCreateDialog = showChannelCreateDialog;
window.switchChannel = switchChannel;
window.filterMessagesByChannel = filterMessagesByChannel;
window.renderChannelTabs = renderChannelTabs;
window.deleteChannel = deleteChannel;
window.showChannelRenameDialog = showChannelRenameDialog;
window.renderChannelSidebar = renderChannelSidebar;
window.setChannelSidebarMode = setChannelSidebarMode;
window.Channels = { init: _channelsInit };
