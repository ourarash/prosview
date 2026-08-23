        // ── Sidebar ──────────────────────────────────────────────────────────

        (function initSidebarResize() {
            var handle = document.getElementById('sidebarResizeHandle');
            if (!handle) return;
            var startX, startW;
            var html = document.documentElement;

            function setSidebarWidth(w) {
                var bounds = workspaceSidebarWidthBounds();
                w = Math.max(bounds.min, Math.min(bounds.max, w));
                html.style.setProperty('--sidebar-w', w + 'px');
                updateSeparatorValue(handle, w, bounds.min, bounds.max);
                try { localStorage.setItem('proseview-sidebar-w', w); } catch(e) {}
            }

            handle.addEventListener('mousedown', function(e) {
                e.preventDefault();
                startX = e.clientX;
                startW = document.getElementById('repoSidebar').offsetWidth;
                document.addEventListener('mousemove', onMove);
                document.addEventListener('mouseup', onUp);
                document.body.style.userSelect = 'none';
                document.body.style.cursor = 'col-resize';
            });

            handle.addEventListener('keydown', function(e) {
                if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
                var current = document.getElementById('repoSidebar').getBoundingClientRect().width;
                var next = current;
                var bounds = workspaceSidebarWidthBounds();
                if (e.key === 'Home') next = bounds.min;
                else if (e.key === 'End') next = bounds.max;
                else next += (e.key === 'ArrowRight' ? 1 : -1) * (e.shiftKey ? 50 : 20);
                setSidebarWidth(next);
                e.preventDefault();
            });

            function onMove(e) { setSidebarWidth(startW + (e.clientX - startX)); }
            function onUp() {
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
                document.body.style.userSelect = '';
                document.body.style.cursor = '';
            }

            try {
                var saved = localStorage.getItem('proseview-sidebar-w');
                if (saved) setSidebarWidth(+saved);
                else setSidebarWidth(document.getElementById('repoSidebar').getBoundingClientRect().width);
            } catch(e) {}
            window.addEventListener('resize', function() {
                setSidebarWidth(document.getElementById('repoSidebar').getBoundingClientRect().width / workspaceZoomFactor());
            });
            window.addEventListener('proseview:workspace-metrics', function() {
                setSidebarWidth(parseFloat(getComputedStyle(html).getPropertyValue('--sidebar-w')) || 280);
            });
        })();

        function setSidebarOpen(open) {
            const html = document.documentElement;
            if (open) {
                html.dataset.sidebar = 'open';
                try { localStorage.setItem('proseview-sidebar', 'open'); } catch(e) {}
                if (!window.__sidebarRendered) {
                    try {
                        renderSidebarTree();
                        window.__sidebarRendered = true;
                    } catch(e) { console.error('proseview: sidebar render failed', e); }
                }
            } else {
                html.dataset.sidebar = 'closed';
                try { localStorage.setItem('proseview-sidebar', 'closed'); } catch(e) {}
            }
            if (typeof syncSidebarInteractiveState === 'function') syncSidebarInteractiveState();
        }

        function renderSidebarTree() {
            const container = document.getElementById('sidebarTree');
            if (!container || !sidebarTree.length) return;
            container.setAttribute('role', 'tree');
            container.setAttribute('aria-label', 'Repository files');
            container.innerHTML = '';
            container.appendChild(buildSidebarList(sidebarTree, 0));
            const first = container.querySelector('[role="treeitem"]');
            if (first) first.tabIndex = 0;
            // The tree is rendered lazily (first open) and rebuilt wholesale,
            // so re-apply whatever the active document is.
            applySidebarReveal();
        }

        function buildSidebarList(nodes, depth) {
            const ul = document.createElement('ul');
            if (depth > 0) ul.setAttribute('role', 'group');
            for (const node of nodes) ul.appendChild(buildSidebarItem(node, depth));
            return ul;
        }

        function sidebarIcon(kind, extraClass) {
            const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            svg.setAttribute('viewBox', '0 0 24 24');
            svg.setAttribute('aria-hidden', 'true');
            svg.setAttribute('focusable', 'false');
            svg.classList.add(kind === 'chevron' ? 'sidebar-disclosure-icon' : 'sidebar-node-icon');
            if (extraClass) svg.classList.add(extraClass);

            const paths = {
                chevron: ['M9 18l6-6-6-6'],
                folder: ['M3.5 8h17v9.5a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z', 'M3.5 8V6.5a2 2 0 0 1 2-2h4l2.2 3.5'],
                file: ['M6 3.5h7l5 5v12H6z', 'M13 3.5v5h5'],
                scene: ['M6 3.5h7l5 5v12H6z', 'M13 3.5v5h5', 'M9 13h6M9 17h5']
            };
            for (const data of paths[kind]) {
                const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
                path.setAttribute('d', data);
                svg.appendChild(path);
            }
            return svg;
        }

        function sidebarLabel(name) {
            const label = document.createElement('span');
            label.className = 'sidebar-row-label';
            label.textContent = name;
            return label;
        }

        function buildSidebarItem(node, depth) {
            const li = document.createElement('li');
            li.setAttribute('role', 'none');
            if (node.is_file) {
                const a = document.createElement('button');
                a.type = 'button';
                a.className = 'file-link';
                a.setAttribute('role', 'treeitem');
                a.setAttribute('aria-level', String(depth + 1));
                a.tabIndex = -1;
                a.dataset.path = node.path;
                const iconSpacer = document.createElement('span');
                iconSpacer.className = 'sidebar-icon-spacer';
                iconSpacer.setAttribute('aria-hidden', 'true');
                a.appendChild(iconSpacer);
                if (node.is_scene) {
                    a.dataset.scenePath = node.scene_path || '';
                    a.appendChild(sidebarIcon('scene', 'sidebar-scene-icon'));
                    a.appendChild(sidebarLabel(node.name));
                    // openSceneModal reveals the scene in the sidebar itself.
                    a.onclick = () => openSceneModal(node.scene_path);
                } else {
                    a.appendChild(sidebarIcon('file'));
                    a.appendChild(sidebarLabel(node.name));
                    // No cache guard: previewRepoFile serves from
                    // repoFileByPath when present and fetches otherwise, which
                    // is the only way to open files outside the preview
                    // folders (manuscript notes, say).
                    a.onclick = () => previewRepoFile(node.path);
                }
                li.appendChild(a);
                if (typeof sidebarAttachRowActions === 'function') sidebarAttachRowActions(node, li, a, depth);
            } else {
                const tog = document.createElement('button');
                tog.type = 'button';
                tog.className = 'dir-toggle';
                tog.setAttribute('role', 'treeitem');
                tog.setAttribute('aria-level', String(depth + 1));
                tog.setAttribute('aria-expanded', depth === 0 ? 'true' : 'false');
                tog.tabIndex = -1;
                tog.appendChild(sidebarIcon('chevron'));
                tog.appendChild(sidebarIcon('folder'));
                tog.appendChild(sidebarLabel(node.name));
                tog.onclick = () => {
                    const expanded = li.classList.toggle('expanded');
                    tog.setAttribute('aria-expanded', expanded ? 'true' : 'false');
                };
                li.appendChild(tog);
                if (typeof sidebarAttachRowActions === 'function') sidebarAttachRowActions(node, li, tog, depth);
                if (node.children && node.children.length)
                    li.appendChild(buildSidebarList(node.children, depth + 1));
                if (depth === 0) li.classList.add('expanded');
            }
            return li;
        }

        function visibleSidebarTreeItems(tree) {
            return Array.from(tree.querySelectorAll('[role="treeitem"]')).filter(function(item) {
                return item.getClientRects().length > 0;
            });
        }

        function focusSidebarTreeItem(tree, item) {
            tree.querySelectorAll('[role="treeitem"]').forEach(function(candidate) {
                candidate.tabIndex = candidate === item ? 0 : -1;
            });
            item.focus();
        }

        document.getElementById('sidebarTree').addEventListener('keydown', function(event) {
            const tree = event.currentTarget;
            const current = event.target.closest('[role="treeitem"]');
            if (!current) return;
            const items = visibleSidebarTreeItems(tree);
            const index = items.indexOf(current);
            if (event.key === 'ArrowDown' && index < items.length - 1) {
                focusSidebarTreeItem(tree, items[index + 1]);
            } else if (event.key === 'ArrowUp' && index > 0) {
                focusSidebarTreeItem(tree, items[index - 1]);
            } else if (event.key === 'Home' && items.length) {
                focusSidebarTreeItem(tree, items[0]);
            } else if (event.key === 'End' && items.length) {
                focusSidebarTreeItem(tree, items[items.length - 1]);
            } else if (event.key === 'ArrowRight' && current.classList.contains('dir-toggle')) {
                if (current.getAttribute('aria-expanded') === 'false') current.click();
                else if (index < items.length - 1) focusSidebarTreeItem(tree, items[index + 1]);
            } else if (event.key === 'ArrowLeft') {
                if (current.classList.contains('dir-toggle') && current.getAttribute('aria-expanded') === 'true') {
                    current.click();
                } else {
                    const parent = current.closest('ul[role="group"]');
                    const parentItem = parent && parent.parentElement && parent.parentElement.querySelector(':scope > .dir-toggle');
                    if (parentItem) focusSidebarTreeItem(tree, parentItem);
                }
            } else if ((event.key === 'Enter' || event.key === ' ') && current) {
                current.click();
            } else {
                return;
            }
            event.preventDefault();
        });

        // The document the sidebar should point at, as {path} or {scenePath}.
        // Kept outside the DOM so a tree that has not been rendered yet (the
        // sidebar renders lazily on first open) still reveals the right file
        // once it is built.
        var _sidebarRevealTarget = null;

        function revealSidebarItem(target) {
            _sidebarRevealTarget = target || null;
            applySidebarReveal();
        }

        function highlightSidebarItem(fullPath) {
            revealSidebarItem({ path: fullPath });
        }

        function applySidebarReveal() {
            const tree = document.getElementById('sidebarTree');
            if (!tree) return;
            const target = _sidebarRevealTarget;
            let match = null;
            tree.querySelectorAll('.file-link').forEach(el => {
                const hit = !!target && (
                    (!!target.path && el.dataset.path === target.path) ||
                    (!!target.scenePath && el.dataset.scenePath === target.scenePath)
                );
                el.classList.toggle('active', hit);
                if (hit) el.setAttribute('aria-current', 'page');
                else el.removeAttribute('aria-current');
                if (hit) match = el;
            });
            // Files outside the sidebar's folders (search reaches the whole
            // repository) simply have nothing to reveal.
            if (!match) return;
            // Expand every ancestor folder so the file is actually on screen.
            for (var li = match.closest('li'); li && tree.contains(li);
                 li = li.parentElement && li.parentElement.closest('li')) {
                li.classList.add('expanded');
                var toggle = li.querySelector(':scope > .dir-toggle');
                if (toggle) toggle.setAttribute('aria-expanded', 'true');
            }
            match.scrollIntoView({ block: 'nearest' });
        }

        // Defer sidebar render so it does not block or interfere with initial
        // page layout and chart initialisation.
        requestAnimationFrame(function() {
            if (document.documentElement.dataset.sidebar !== 'closed') {
                try {
                    renderSidebarTree();
                    window.__sidebarRendered = true;
                } catch(e) {
                    console.error('proseview: sidebar render failed', e);
                }
            }
        });

        function sortTable(n) { sortTableEl(document.getElementById("sceneTable"), n); }

        function sortTableEl(t, n) {
            if (!t) return;
            var r = t.rows, s = true, d = "asc", c = 0;
            while (s) {
                s = false;
                for (var i = 1; i < (r.length - 1); i++) {
                    var x = r[i].getElementsByTagName("TD")[n], y = r[i+1].getElementsByTagName("TD")[n], should = false;
                    if (!x || !y) continue;
                    var xv = x.innerText.replace(/,/g, '').replace(/%/g, ''), yv = y.innerText.replace(/,/g, '').replace(/%/g, '');
                    if (!isNaN(parseFloat(xv)) && !isNaN(parseFloat(yv))) { xv = parseFloat(xv); yv = parseFloat(yv); } else { xv = xv.toLowerCase(); yv = yv.toLowerCase(); }
                    if (d == "asc" ? xv > yv : xv < yv) { should = true; break; }
                }
                if (should) { r[i].parentNode.insertBefore(r[i+1], r[i]); s = true; c++; } else if (c == 0 && d == "asc") { d = "desc"; s = true; }
            }
            Array.from(t.tHead ? t.tHead.rows[0].cells : []).forEach(function(header, index) {
                header.setAttribute('aria-sort', index === n ? (d === 'asc' ? 'ascending' : 'descending') : 'none');
            });
        }
