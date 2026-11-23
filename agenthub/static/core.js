/**
 * core.js -- EventHub for agentchattr
 *
 * Tiny pub/sub hub. WebSocket events are emitted here so modules can
 * subscribe without touching the legacy switch statement in chat.js.
 *
 * Usage:
 *   Hub.on('session', (data) => { ... });
 *   Hub.emit('session', { action: 'create', data: session });
 *   Hub.off('session', handler);
 */

const Hub = (() => {
    const _listeners = {};

    function on(type, fn) {
        if (!_listeners[type]) _listeners[type] = [];
        _listeners[type].push(fn);
    }

    function off(type, fn) {
        const list = _listeners[type];
        if (!list) return;
        const idx = list.indexOf(fn);
        if (idx !== -1) list.splice(idx, 1);
    }

    function emit(type, data) {
        const list = _listeners[type];
        if (!list) return;
        for (const fn of list) {
            try {
                fn(data);
            } catch (e) {
                console.error(`[Hub] Error in listener for "${type}":`, e);
            }
        }
    }

    return { on, off, emit };
})();

// Make Hub available globally during the transition period.
// Once all modules use ES imports, this can be removed.
window.Hub = Hub;

// Links in chat (markdown, auto-linked URLs, attachments) always open in a new
// tab, never replacing the room. In-page links (href="#...") and javascript
// handlers are left alone.
document.addEventListener('click', (e) => {
    const a = e.target.closest && e.target.closest('a[href]');
    if (!a || e.defaultPrevented) return;
    const href = a.getAttribute('href') || '';
    if (!href || href.startsWith('#') || href.toLowerCase().startsWith('javascript:')) return;
    if (a.hasAttribute('download')) return;  // saving a file is not navigation
    if (a.hasAttribute('onclick')) return;   // app-handled links (attachment download, open path)
    e.preventDefault();
    window.open(a.href, '_blank', 'noopener,noreferrer');
}, true);


// Phones and tablets: the navigation and the members slide in as drawers (see style.css, shell.css).
function toggleDrawer(which) {
    const body = document.body;
    for (const name of ['channels', 'members']) {
        const open = name === which && !body.classList.contains(`drawer-${name}`);
        body.classList.toggle(`drawer-${name}`, open);
        document.getElementById(`nav-${name}`)?.setAttribute('aria-expanded', String(open));
    }
}
window.toggleDrawer = toggleDrawer;

// iOS Safari doesn't shrink the layout for the on-screen keyboard: size the app to the
// visible viewport so the composer stays above the keyboard and the header stays on top.
if (window.visualViewport) {
    const vv = window.visualViewport;
    const fit = () => {
        document.documentElement.style.setProperty('--vvh', `${vv.height}px`);
        document.documentElement.style.setProperty('--vvt', `${vv.offsetTop}px`);
    };
    vv.addEventListener('resize', fit);
    vv.addEventListener('scroll', fit);
    fit();
}
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') toggleDrawer(null); });
// Picking a channel, a member or a tool closes the drawer
// (tapping the channel you're already in opens its rename/delete controls, so keep the drawer then)
document.addEventListener('click', (e) => {
    if (!document.body.matches('.drawer-channels, .drawer-members')) return;
    const row = e.target.closest('#channel-sidebar-list [data-channel]');
    if ((row && !row.classList.contains('active')) || e.target.closest('#members-list .member, .members-picker-item, #channel-sidebar .nav-item')) {
        setTimeout(() => toggleDrawer(null), 0);
    }
}, true);
