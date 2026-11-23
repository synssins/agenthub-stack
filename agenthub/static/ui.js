// UIKit: the shared building blocks of the settings pages and menus (styles in ui.css).
// Everything is built with createElement / textContent; nothing here takes HTML strings.
(function () {
    function node(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined) e.textContent = text;
        return e;
    }
    function put(parent, children) {
        for (const c of children.flat()) if (c) parent.append(c);
        return parent;
    }

    // A section that expands and collapses. opts: {id, open, note, count}. With an id the
    // open/closed state is remembered in this browser. -> <details>; add content to .body.
    function group(title, opts = {}, ...children) {
        const d = node('details', 'ui-group');
        const key = opts.id ? 'agenthub-group-' + opts.id : null;
        let open = opts.open !== false;
        if (key) { try { const v = localStorage.getItem(key); if (v !== null) open = v === '1'; } catch (e) { /* private mode */ } }
        d.open = open;
        const s = node('summary', 'ui-group-head');
        s.append(node('span', 'ui-group-title', title));
        if (opts.count !== undefined && opts.count !== null) s.append(node('span', 'ui-badge', String(opts.count)));
        if (opts.note) s.append(node('span', 'ui-group-note', opts.note));
        d.body = node('div', 'ui-group-body');
        d.append(s, put(d.body, children));
        if (key) d.addEventListener('toggle', () => { try { localStorage.setItem(key, d.open ? '1' : '0'); } catch (e) { /* private mode */ } });
        return d;
    }

    // One setting: label and optional hint on the left, the control on the right.
    // opts: {hint, stack} (stack = control under the label, for wide controls).
    function field(label, control, opts = {}) {
        const row = node(control && control.matches && control.matches('input[type=checkbox]') ? 'label' : 'div',
                         'ui-field' + (opts.stack ? ' stack' : ''));
        const text = node('div', 'ui-field-text');
        const name = node(row.tagName === 'LABEL' ? 'span' : 'label', 'ui-field-label', label);
        if (control && control.id && name.tagName === 'LABEL') name.htmlFor = control.id;
        else if (control && name.tagName === 'LABEL' && control.matches && control.matches('input, select, textarea')) {
            control.id = control.id || 'ui-f' + (++field.n);
            name.htmlFor = control.id;
        }
        text.append(name);
        if (opts.hint) text.append(node('small', 'ui-field-hint', opts.hint));
        const slot = node('div', 'ui-field-control');
        if (control) slot.append(control);
        row.append(text, slot);
        return row;
    }
    field.n = 0;

    const badge = (text, tone) => node('span', 'ui-badge' + (tone ? ' ' + tone : ''), text);   // tone: ok | warn | danger | accent
    const empty = (title, hint, action) => put(node('div', 'ui-empty'),
        [node('strong', '', title), hint ? node('span', '', hint) : null, action || null]);
    const actions = (...children) => put(node('div', 'ui-actions'), children);

    window.UIKit = { group, field, badge, empty, actions };
})();
