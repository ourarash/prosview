        // Hash hydration runs before the preview function's source position, so
        // lifecycle state used by it must be initialized at bundle entry.
        var _repoPreviewRequestVersion = 0;


        syncThemeToggle();
        syncFontToggle();
        syncModalFontSize();
        document.addEventListener('scroll', scheduleScrollSave, true);
        window.addEventListener('beforeunload', saveActiveScrollPosition);

        // Defer chart init so the page layout settles before Chart.js attaches
        // its ResizeObserver. Initialising charts synchronously in a WebView
        // (VS Code Live Preview uses Electron/Chromium) can cause an immediate
        // ResizeObserver → resize → ResizeObserver loop that exhausts the call
        // stack (RangeError from the cross-origin chart.js bundle).
        requestAnimationFrame(function() {
          // responsive:false avoids the ResizeObserver setter cycle (Object.set
          // ↔ Object.set infinite recursion) in Electron/Chromium WebViews.
          function renderChartData(id, cfg) {
            var canvas = document.getElementById(id);
            var figure = canvas && canvas.closest('.chart-figure');
            var mount = figure && figure.querySelector('.chart-data');
            if (!mount) return;
            mount.replaceChildren();
            var details = document.createElement('details');
            var summary = document.createElement('summary');
            summary.textContent = 'View chart data';
            details.appendChild(summary);
            var table = document.createElement('table');
            table.className = 'chart-data-table';
            var labels = (cfg.data && cfg.data.labels) || [];
            var datasets = (cfg.data && cfg.data.datasets) || [];
            var scatter = datasets.some(function(ds) {
              return (ds.data || []).some(function(point) {
                return point && typeof point === 'object' && point.x !== undefined;
              });
            });
            var head = document.createElement('thead');
            var headRow = document.createElement('tr');
            var xAxisName = cfg.options && cfg.options.scales && cfg.options.scales.x &&
              cfg.options.scales.x.title && cfg.options.scales.x.title.text || 'X';
            var yAxisName = cfg.options && cfg.options.scales && cfg.options.scales.y &&
              cfg.options.scales.y.title && cfg.options.scales.y.title.text || 'Y';
            (scatter ? ['Point', 'Series', xAxisName, yAxisName] : ['Category', 'Series', 'Value']).forEach(function(label) {
              var th = document.createElement('th');
              th.scope = 'col';
              th.textContent = label;
              headRow.appendChild(th);
            });
            head.appendChild(headRow);
            table.appendChild(head);
            var body = document.createElement('tbody');
            datasets.forEach(function(ds, datasetIndex) {
              (ds.data || []).forEach(function(value, index) {
                var row = document.createElement('tr');
                var cells;
                if (scatter && value && typeof value === 'object') {
                  cells = [value.label || labels[index] || 'Point ' + (index + 1), ds.label || 'Value', value.x, value.y];
                } else {
                  cells = [labels[index] || 'Item ' + (index + 1), ds.label || 'Value', value];
                }
                cells.forEach(function(value, cellIndex) {
                  var cell = document.createElement(cellIndex === 0 ? 'th' : 'td');
                  if (cellIndex === 0) cell.scope = 'row';
                  var displayed = value;
                  if (typeof value === 'number' && !Number.isInteger(value)) {
                    displayed = Number(value.toFixed(3));
                  }
                  cell.textContent = displayed === null || displayed === undefined ? '—' : String(displayed);
                  row.appendChild(cell);
                });
                body.appendChild(row);
              });
            });
            table.appendChild(body);
            var annotations = cfg.options && cfg.options.plugins && cfg.options.plugins.annotation &&
              cfg.options.plugins.annotation.annotations;
            var target = annotations && annotations.target;
            if (target && target.xMin !== undefined && target.xMax !== undefined &&
                target.yMin !== undefined && target.yMax !== undefined) {
              var context = document.createElement('p');
              context.className = 'chart-data-context';
              context.textContent = 'Target range: ' + xAxisName + ' ' + target.xMin + ' to ' + target.xMax +
                '; ' + yAxisName + ' ' + target.yMin + ' to ' + target.yMax + '.';
              details.appendChild(context);
            }
            details.appendChild(table);
            mount.appendChild(details);
          }

          function makeChart(id, h, cfg) {
            var canvas = document.getElementById(id);
            if (!canvas) return null;
            var w = (canvas.parentElement ? canvas.parentElement.offsetWidth : 0) || 400;
            canvas.width = w;
            canvas.height = h;
            // animation:false + responsive:false prevent both the async rAF
            // render path and the ResizeObserver setter cycle in Electron WebViews.
            cfg.options = Object.assign({responsive: false, maintainAspectRatio: false, animation: false}, cfg.options || {});
            applyThemeToConfig(id, cfg, getThemePalette());
            renderChartData(id, cfg);
            try { return new Chart(canvas, cfg); }
            catch(e) { console.error('proseview: chart init failed (' + id + ')', e); return null; }
          }

          // A chart with no rows renders as an empty axis box, which looks
          // identical whether the manuscript has no characters configured or
          // the name matching is broken. Say which.
          function noteIfEmpty(id, data, message) {
              const rows = (data.datasets || []).reduce(function(n, d) {
                  return n + ((d.data || []).length);
              }, 0);
              if (rows) return;
              const canvas = document.getElementById(id);
              if (!canvas || !canvas.parentElement) return;
              const note = document.createElement('p');
              note.className = 'story-empty chart-empty';
              note.innerHTML = message;
              canvas.parentElement.replaceChildren(note);
              if (chartRefs[id]) { chartRefs[id].destroy(); delete chartRefs[id]; }
          }



          // Built on demand by the Analysis tab -- see 19-analysis.js. Kept in
          // this closure so it still has makeChart and the theme helpers.
          window.buildAnalysisCharts = function(data) {
            if (chartRefs.lexicalScatterChart) { chartRefs.lexicalScatterChart.destroy(); }
            if (chartRefs.presenceChart) { chartRefs.presenceChart.destroy(); }
            if (chartRefs.locationChart) { chartRefs.locationChart.destroy(); }
            if (chartRefs.coOccurChart) { chartRefs.coOccurChart.destroy(); }

            chartRefs.presenceChart = makeChart('presenceChart', 250, {
                type: 'line', data: presenceChartData,
                options: { scales: { x: { title: { display: true, text: 'Chapter' } }, y: { beginAtZero: true, title: { display: true, text: 'Mentions' } } }, plugins: { legend: { position: 'right', labels: { boxWidth: 10, font: { size: 9 } } } } }
            });

            chartRefs.locationChart = makeChart('locationChart', 250, {
                type: 'bar', data: locationChartData,
                options: { indexAxis: 'y', plugins: { legend: { display: false } } }
            });

            chartRefs.coOccurChart = makeChart('coOccurChart', 250, {
                type: 'bar', data: coOccurChartData,
                options: { indexAxis: 'y', plugins: { legend: { display: false } } }
            });

            const NO_CAST = 'No characters yet. Add a <code>characters:</code> list to '
                + '<code>.proseview.yaml</code>, or put one Markdown file per character in '
                + '<code>story-bible/characters/</code>. A character file\u2019s '
                + '<code>name:</code> is matched against the prose, so multi-word names work.';
            noteIfEmpty('presenceChart', presenceChartData, NO_CAST);
            noteIfEmpty('coOccurChart', coOccurChartData, NO_CAST);
            noteIfEmpty('locationChart', locationChartData,
                'No settings yet. Add <code>where:</code> to a scene\u2019s frontmatter, or '
                + 'group scenes into folders by location.');

            chartRefs.lexicalScatterChart = makeChart('lexicalScatterChart', 350, {
              type: 'scatter', data: { datasets: data.scatterChart.datasets },
              options: { scales: { x: { min:0.65, max:0.85, title: { display: true, text: 'Local Variety (MATTR)' } }, y: { min:50, max:180, title: { display: true, text: 'Whole-Scene Variety (MTLD)' } } },
              plugins: { legend: { display: false }, annotation: { annotations: { target: { type: 'box', xMin: data.scatterChart.bands.mattr[0], xMax: data.scatterChart.bands.mattr[1], yMin: data.scatterChart.bands.mtld[0], yMax: data.scatterChart.bands.mtld[1], borderWidth: 2 } } } } }
            });
          };

        });

        function _mtimeForAbsPath(absPath) {
            // The annotation endpoints refuse a write when the file changed
            // since this page rendered it. meta is keyed by display path, so
            // find the entry whose abs_path matches.
            for (const p in meta) {
                if (meta[p] && meta[p].abs_path === absPath) return meta[p].mtime;
            }
            return undefined;
        }

        const NOTE_TAGS = ['note', 'continuity', 'character', 'theme', 'question'];

        function todoEntryHtml(t, absPath) {
            const tagChip = '<span class="note-tag-chip note-tag-todo">todo</span>';
            if (t.source === 'frontmatter') {
                return '<div class="todo-entry">' +
                    '<div class="todo-entry-display">' +
                    tagChip +
                    '<span class="todo-entry-text">' + escHtml(t.text) + '</span>' +
                    '</div></div>';
            }
            const lineLabel = t.line ? '<span class="todo-line">L' + t.line + '</span>' : '';
            const actionBtns = '<div class="note-entry-actions"><button class="todo-edit-btn" type="button">Edit</button><button class="todo-delete-btn" type="button">Delete</button></div>';
            return '<div class="todo-entry" data-abs-path="' + attrEscape(absPath) + '" data-todo-text="' + attrEscape(t.text) + '">' +
                '<div class="todo-entry-display">' +
                tagChip +
                lineLabel +
                '<span class="todo-entry-text">' + escHtml(t.text) + '</span>' +
                actionBtns +
                '</div>' +
                '<div class="todo-entry-edit" hidden>' +
                '<textarea class="note-edit-textarea">' + escHtml(t.text) + '</textarea>' +
                '<div class="note-edit-actions">' +
                '<button class="todo-save-btn" type="button">Save</button>' +
                '<button class="todo-cancel-edit-btn" type="button">Cancel</button>' +
                '</div></div></div>';
        }

        function noteEntryHtml(n, absPath) {
            const tagOptions = NOTE_TAGS.map(function(t) {
                return '<option value="' + t + '"' + (t === n.tag ? ' selected' : '') + '>' + t + '</option>';
            }).join('');
            const lineLabel = n.line ? '<span class="todo-line">L' + n.line + '</span>' : '';
            const actionBtns = '<div class="note-entry-actions"><button class="note-edit-btn" type="button">Edit</button><button class="note-delete-btn" type="button">Delete</button></div>';
            return '<div class="note-entry" data-abs-path="' + attrEscape(absPath) + '" data-note-text="' + attrEscape(n.text) + '" data-note-tag="' + attrEscape(n.tag) + '">' +
                '<div class="note-entry-display">' +
                '<span class="note-tag-chip note-tag-' + escHtml(n.tag) + '">' + escHtml(n.tag) + '</span>' +
                lineLabel +
                '<span class="note-entry-text">' + escHtml(n.text) + '</span>' +
                actionBtns +
                '</div>' +
                '<div class="note-entry-edit" hidden>' +
                '<select class="note-edit-tag">' + tagOptions + '</select>' +
                '<textarea class="note-edit-textarea">' + escHtml(n.text) + '</textarea>' +
                '<div class="note-edit-actions">' +
                '<button class="note-save-btn" type="button">Save</button>' +
                '<button class="note-cancel-edit-btn" type="button">Cancel</button>' +
                '</div></div></div>';
        }

        function buildNotesTab() {
            const content = document.getElementById('notesTabContent');
            if (!content) return;
            const tagFilter = (document.getElementById('notesTagFilter') || {}).value || 'all';
            const grouped = {};
            Object.keys(meta).forEach(function(path) {
                const notes = (meta[path].notes || []).filter(function(n) {
                    return tagFilter === 'all' || n.tag === tagFilter;
                });
                if (notes.length) grouped[path] = {notes: notes, abs_path: meta[path].abs_path};
            });
            const keys = Object.keys(grouped).sort();
            if (!keys.length) {
                content.innerHTML = '<div class="notes-empty">No notes' + (tagFilter !== 'all' ? ' tagged "' + escHtml(tagFilter) + '"' : '') + '.</div>';
                return;
            }
            let html = '';
            keys.forEach(function(path) {
                const {notes, abs_path} = grouped[path];
                const name = path.split('/').pop() || path;
                html += '<div class="notes-scene-group">' +
                    '<div class="notes-scene-header">' +
                    '<button class="notes-scene-link" type="button" data-scene-path="' + attrEscape(path) + '">' + escHtml(name) + '</button>' +
                    '</div>';
                notes.forEach(function(n) {
                    html += '<div class="notes-row">' + noteEntryHtml(n, abs_path) + '</div>';
                });
                html += '</div>';
            });
            content.innerHTML = html;
        }

        function filterNotes() { buildNotesTab(); }

        function buildTodosTab() {
            const content = document.getElementById('todosTabContent');
            if (!content) return;
            const grouped = {};
            Object.keys(meta).forEach(function(path) {
                const todos = meta[path].todos || [];
                if (todos.length) grouped[path] = {todos: todos, abs_path: meta[path].abs_path};
            });
            const keys = Object.keys(grouped).sort();
            if (!keys.length) {
                content.innerHTML = '<div class="notes-empty">No TODOs found.</div>';
                return;
            }
            let html = '';
            keys.forEach(function(path) {
                const {todos, abs_path} = grouped[path];
                const name = path.split('/').pop() || path;
                html += '<div class="notes-scene-group">' +
                    '<div class="notes-scene-header">' +
                    '<button class="notes-scene-link" type="button" data-scene-path="' + attrEscape(path) + '">' + escHtml(name) + '</button>' +
                    '</div>';
                todos.forEach(function(t) {
                    html += '<div class="notes-row">' + todoEntryHtml(t, abs_path) + '</div>';
                });
                html += '</div>';
            });
            content.innerHTML = html;
        }

        document.addEventListener('click', function(e) {
            const sceneLink = e.target.closest('.notes-scene-link[data-scene-path]');
            if (sceneLink) {
                e.preventDefault();
                openSceneModal(sceneLink.dataset.scenePath);
                return;
            }

            const jb = e.target.closest('.task-jump-btn');
            if (jb) {
                e.preventDefault();
                const paraIdx = parseInt(jb.dataset.paraIdx, 10);
                _scrollToPara(paraIdx);
                return;
            }
        });

        // Try several strategies to locate the paragraph in the live DOM
        // and scroll/flash it. Falls back to retries because ProseMirror
        // may finish mounting after the click fires (the user can click
        // immediately after a refresh).
        function _scrollToPara(paraIdx, attempt) {
            attempt = attempt || 0;
            var target = _findParaTarget(paraIdx);
            if (!target && attempt < 4) {
                setTimeout(function() { _scrollToPara(paraIdx, attempt + 1); }, 80);
                return;
            }
            if (!target) {
                console.warn('[proseview] task-jump: no DOM target for paragraph', paraIdx);
                return;
            }
            // ``block: 'start'`` aligns the target to the top of the
            // scroll container (modulo CSS scroll-padding-top). This makes
            // the jump visually unambiguous even when two TODO/Note
            // markers sit a few pixels apart in adjacent paragraphs --
            // ``block: 'center'`` produced nearly identical scroll
            // positions for those, so the second click looked like a no-op.
            target.scrollIntoView({ behavior: 'smooth', block: 'start' });
            target.classList.add('para-flash');
            setTimeout(function() { target.classList.remove('para-flash'); }, 1600);
        }

        // Resolve a paragraph index (counted by paragraph_blocks() in
        // scenes.py) to a DOM element under the live ProseMirror view.
        // paragraph_blocks() splits on blank lines and filters out
        // headings; we apply the same filter to the rendered DOM so the
        // index lines up.
        //
        // Subtle point: the line-number gutter plugin emits a
        // Decoration.widget(offset+1) for every top-level node. For
        // paragraphs (non-atom) the widget renders INSIDE the <p>, so
        // it doesn't appear as a sibling. For annotation atoms the
        // widget can't render inside the atom, so ProseMirror places it
        // as a top-level sibling immediately after the annotation. That
        // shifted every index past the first annotation by one and made
        // task-jump arrows for any TODO/Note after a previous annotation
        // land on a .pm-line-jump <a> instead of the annotation itself.
        function _findParaTarget(paraIdx) {
            if (!Number.isFinite(paraIdx) || paraIdx < 0) return null;
            var host = document.getElementById('sceneProseHost');
            if (!host) return null;
            var root = host.querySelector('.ProseMirror');
            if (!root) return null;
            var blocks = [];
            for (var i = 0; i < root.children.length; i++) {
                var el = root.children[i];
                if (/^H[1-6]$/i.test(el.tagName)) continue;
                if (el.classList && el.classList.contains('ProseMirror-widget')) continue;
                blocks.push(el);
            }
            return blocks[paraIdx] || null;
        }

        function closeAllPopovers() {
            document.querySelectorAll('.note-popover.is-open, .todo-popover.is-open').forEach(function(p) { p.classList.remove('is-open'); });
        }

        function postAndReload(url, body, errorMsg, disableBtn) {
            if (disableBtn) disableBtn.disabled = true;
            fetch(url, {method: 'POST', headers: pvHeaders(), body: JSON.stringify(body)})
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.ok) setTimeout(function() { location.reload(); }, 300);
                    else { if (disableBtn) disableBtn.disabled = false; alert(errorMsg + ': ' + (data.error || 'unknown error')); }
                })
                .catch(function(err) { if (disableBtn) disableBtn.disabled = false; alert('Request failed: ' + err); });
        }

        document.addEventListener('click', function(e) {
            // "Add this block": write an empty frontmatter scaffold into a
            // scene that has none. Every key lands blank on purpose -- a
            // guessed value would read like something the writer typed.
            const fmAddBtn = e.target.closest('.scene-card-fm-add');
            if (fmAddBtn) {
                const absPath = fmAddBtn.dataset.absPath;
                if (!absPath) return;
                postAndReload(
                    '/add-frontmatter',
                    {abs_path: absPath, open_mtime: _mtimeForAbsPath(absPath)},
                    'Could not add the frontmatter block',
                    fmAddBtn
                );
                return;
            }
            // Note icon: toggle popover open/closed
            const noteIcon = e.target.closest('.note-marker-icon');
            if (noteIcon) {
                const popover = noteIcon.closest('.note-marker') && noteIcon.closest('.note-marker').querySelector('.note-popover');
                if (popover) {
                    const wasOpen = popover.classList.contains('is-open');
                    closeAllPopovers();
                    if (!wasOpen) popover.classList.add('is-open');
                }
                return;
            }

            // TODO icon: toggle popover open/closed
            const todoIcon = e.target.closest('.todo-marker-icon');
            if (todoIcon) {
                const popover = todoIcon.closest('.todo-marker') && todoIcon.closest('.todo-marker').querySelector('.todo-popover');
                if (popover) {
                    const wasOpen = popover.classList.contains('is-open');
                    closeAllPopovers();
                    if (!wasOpen) popover.classList.add('is-open');
                }
                return;
            }

            // Actions inside a note entry
            const noteEntry = e.target.closest('.note-entry');
            if (noteEntry) {
                if (e.target.closest('.note-edit-btn')) {
                    noteEntry.querySelector('.note-entry-display').hidden = true;
                    const editSec = noteEntry.querySelector('.note-entry-edit');
                    editSec.hidden = false;
                    editSec.querySelector('.note-edit-textarea').focus();
                    return;
                }
                if (e.target.closest('.note-cancel-edit-btn')) {
                    noteEntry.querySelector('.note-entry-display').hidden = false;
                    noteEntry.querySelector('.note-entry-edit').hidden = true;
                    return;
                }
                if (e.target.closest('.note-save-btn')) {
                    const btn = e.target.closest('.note-save-btn');
                    const newText = noteEntry.querySelector('.note-edit-textarea').value.trim();
                    const newTag = noteEntry.querySelector('.note-edit-tag').value;
                    if (!newText) return;
                    postAndReload('/edit-note', {abs_path: noteEntry.dataset.absPath, old_note_text: noteEntry.dataset.noteText, old_tag: noteEntry.dataset.noteTag, new_note_text: newText, new_tag: newTag, open_mtime: _mtimeForAbsPath(noteEntry.dataset.absPath)}, 'Could not save note', btn);
                    return;
                }
                if (e.target.closest('.note-delete-btn')) {
                    postAndReload('/delete-note', {abs_path: noteEntry.dataset.absPath, note_text: noteEntry.dataset.noteText, tag: noteEntry.dataset.noteTag, open_mtime: _mtimeForAbsPath(noteEntry.dataset.absPath)}, 'Could not delete note', e.target.closest('.note-delete-btn'));
                    return;
                }
                return;
            }

            // Actions inside a todo entry
            const todoEntry = e.target.closest('.todo-entry');
            if (todoEntry && todoEntry.dataset.absPath) {
                if (e.target.closest('.todo-edit-btn')) {
                    todoEntry.querySelector('.todo-entry-display').hidden = true;
                    const editSec = todoEntry.querySelector('.todo-entry-edit');
                    editSec.hidden = false;
                    editSec.querySelector('.note-edit-textarea').focus();
                    return;
                }
                if (e.target.closest('.todo-cancel-edit-btn')) {
                    todoEntry.querySelector('.todo-entry-display').hidden = false;
                    todoEntry.querySelector('.todo-entry-edit').hidden = true;
                    return;
                }
                if (e.target.closest('.todo-save-btn')) {
                    const btn = e.target.closest('.todo-save-btn');
                    const newText = todoEntry.querySelector('.note-edit-textarea').value.trim();
                    if (!newText) return;
                    postAndReload('/edit-todo', {abs_path: todoEntry.dataset.absPath, old_todo_text: todoEntry.dataset.todoText, new_todo_text: newText, open_mtime: _mtimeForAbsPath(todoEntry.dataset.absPath)}, 'Could not save TODO', btn);
                    return;
                }
                if (e.target.closest('.todo-delete-btn')) {
                    postAndReload('/delete-todo', {abs_path: todoEntry.dataset.absPath, todo_text: todoEntry.dataset.todoText, open_mtime: _mtimeForAbsPath(todoEntry.dataset.absPath)}, 'Could not delete TODO', e.target.closest('.todo-delete-btn'));
                    return;
                }
                return;
            }

            // Click outside all markers/popovers: close everything
            if (!e.target.closest('.note-marker') && !e.target.closest('.note-popover') &&
                !e.target.closest('.todo-marker') && !e.target.closest('.todo-popover')) {
                closeAllPopovers();
            }
        });

        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') closeAllPopovers();
        });

        function showTab(name) {
            saveActiveScrollPosition();
            name = VALID_TABS.includes(name) ? name : 'overview';
            document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
            document.querySelectorAll('.tab-nav button').forEach(function(b) {
                b.classList.remove('active');
                b.removeAttribute('aria-current');
            });
            const panel = document.getElementById('tab-' + name);
            if (panel) panel.classList.add('active');
            const btn = document.querySelector('.tab-nav button[data-tab="' + name + '"]');
            if (btn) {
                btn.classList.add('active');
                btn.setAttribute('aria-current', 'page');
            }
            currentTab = name;
            if (name === 'analysis') buildAnalysisTab();
            if (name === 'timeline') buildTimelineTab();
            if (name === 'notes') buildNotesTab();
            if (name === 'todos') buildTodosTab();
            routeToHash('/tab/' + name, true);
            restoreActiveScrollPosition();
        }

        const ROUTE_HISTORY_INDEX = 'proseviewRouteIndex';
        let routeHistoryIndex = Number.isInteger(history.state && history.state[ROUTE_HISTORY_INDEX])
            ? history.state[ROUTE_HISTORY_INDEX]
            : 0;
        if (!Number.isInteger(history.state && history.state[ROUTE_HISTORY_INDEX])) {
            history.replaceState(
                Object.assign({}, history.state || {}, {[ROUTE_HISTORY_INDEX]: routeHistoryIndex}),
                '',
                window.location.href
            );
        }
        let restoringGuardedTraversal = null;

        function routeToHash(fragment, push) {
            if (suppressHashWrite) return;
            const full = '#' + fragment;
            if (window.location.hash === full) return;
            if (push) {
                routeHistoryIndex += 1;
                history.pushState({[ROUTE_HISTORY_INDEX]: routeHistoryIndex}, '', full);
            } else {
                history.replaceState({[ROUTE_HISTORY_INDEX]: routeHistoryIndex}, '', full);
            }
        }

        function parseHashRoute() {
            const raw = window.location.hash;
            if (!raw || raw === '#' || raw === '#/') return null;
            const clean = raw.replace(/^#\/?/, '');
            const slash = clean.indexOf('/');
            if (slash < 0) return { kind: clean, arg: '' };
            const kind = clean.substring(0, slash);
            let arg = clean.substring(slash + 1);
            try { arg = decodeURIComponent(arg); } catch (err) { /* keep raw */ }
            return { kind: kind, arg: arg };
        }

        function applyHashRoute() {
            const route = parseHashRoute();
            if (restoringGuardedTraversal) {
                const pending = restoringGuardedTraversal;
                restoringGuardedTraversal = null;
                routeHistoryIndex = pending.activeIndex;
                showUnsavedDialog({onContinue: function() {
                    history.go(pending.delta);
                }});
                return;
            }
            if (typeof guardDirtySceneNavigation === 'function' && guardDirtySceneNavigation()) {
                const activeScene = paths[curIdx];
                const sameScene = route && route.kind === 'scene' && route.arg === activeScene;
                if (!sameScene) {
                    const targetIndex = history.state && history.state[ROUTE_HISTORY_INDEX];
                    const delta = Number.isInteger(targetIndex) ? targetIndex - routeHistoryIndex : 0;
                    if (delta) {
                        restoringGuardedTraversal = {
                            activeIndex: routeHistoryIndex,
                            delta: delta,
                        };
                        history.go(-delta);
                    } else {
                        // A manually assigned hash has no indexed History API
                        // entry. Keep the active route visible without adding
                        // synthetic entries, then replay the pending URL only
                        // after the writer confirms.
                        const pendingUrl = window.location.href;
                        const activeHash = '#/scene/' + encodeURIComponent(activeScene).replace(/%2F/gi, '/');
                        history.replaceState(
                            {[ROUTE_HISTORY_INDEX]: routeHistoryIndex}, '', activeHash
                        );
                        showUnsavedDialog({onContinue: function() {
                            history.replaceState(
                                {[ROUTE_HISTORY_INDEX]: routeHistoryIndex}, '', pendingUrl
                            );
                            applyHashRoute();
                        }});
                    }
                    return;
                }
                return;
            }
            const targetIndex = history.state && history.state[ROUTE_HISTORY_INDEX];
            if (Number.isInteger(targetIndex)) routeHistoryIndex = targetIndex;
            suppressHashWrite = true;
            routeHydrating = true;
            try {
                if (!route) {
                    delete document.documentElement.dataset.view;
                    showTab('overview');
                    return;
                }
                if (route.kind === 'tab') {
                    delete document.documentElement.dataset.view;
                    showTab(VALID_TABS.includes(route.arg) ? route.arg : 'overview');
                } else if (route.kind === 'scene' && route.arg && paths.indexOf(route.arg) >= 0) {
                    openSceneModal(route.arg);
                } else if (route.kind === 'file' && route.arg) {
                    previewRepoFile(route.arg, { route: false });
                } else {
                    delete document.documentElement.dataset.view;
                    showTab('overview');
                }
            } finally {
                routeHydrating = false;
                suppressHashWrite = false;
                restoreActiveScrollPosition();
            }
        }

        window.addEventListener('popstate', applyHashRoute);
        if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
        if (window.location.hash && window.location.hash !== '#' && window.location.hash !== '#/') {
            applyHashRoute();
        } else {
            restoreActiveScrollPosition();
        }

        function escHtml(s) {
            return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        }

        function renderRepoFile(node, options) {
            options = options || {};
            var path = node.path;
            saveActiveScrollPosition();
            highlightSidebarItem(path);
            if (options.route !== false) routeToHash('/file/' + encodeURIComponent(path), true);
            document.getElementById('filePreviewTitle').textContent = node.path;
            const sizeKb = (node.size / 1024).toFixed(1);
            document.getElementById('filePreviewMeta').textContent =
                'Last modified ' + node.modified_at + ' \u00b7 ' + sizeKb + ' KB';
            const editorBtn = document.getElementById('filePreviewEditorBtn');
            editorBtn.href = buildEditorUrl(node.abs_path);
            editorBtn.textContent = '\u2197';
            editorBtn.title = 'Open in ' + editorLabel;
            const body = document.getElementById('filePreviewBody');
            if (node.too_large) {
                const limitKb = (repoPreviewMax / 1024).toFixed(0);
                body.innerHTML = '<div class="repo-warn">This file is ' + sizeKb + ' KB, above the ' + limitKb + ' KB preview limit.</div>';
            } else if (!node.is_text || node.body === null) {
                body.innerHTML = '<div class="repo-warn">Preview not available for this file type.</div>';
            } else {
                const lname = node.name.toLowerCase();
                if (lname.endsWith('.md') || lname.endsWith('.markdown')) {
                    if (node.body.length > 65536) {
                        body.innerHTML = '<div class="repo-warn">This file is ' + sizeKb + ' KB \u2014 too large for inline rendering. <a class="editor-btn" href="' + editorBtn.href + '" target="_blank">\u2197 Open in ' + escHtml(editorLabel) + '</a></div>';
                    } else {
                        body.replaceChildren();
                        renderSafeMarkdown(body, node.body, {basePath: node.path});
                    }
                } else {
                    body.innerHTML = '';
                    const pre = document.createElement('pre');
                    pre.innerText = node.body;
                    body.appendChild(pre);
                }
            }
            body.scrollTop = 0;
            document.documentElement.dataset.view = 'file';
            restoreActiveScrollPosition();
            if (typeof discussFollowActiveDocument === 'function') discussFollowActiveDocument();
            if (options.focus) document.getElementById('filePreviewTitle').focus({ preventScroll: true });
            return node;
        }

        function previewRepoFile(path, options) {
            options = options || {};
            if (typeof guardDirtySceneNavigation === 'function' && guardDirtySceneNavigation()) {
                showUnsavedDialog({onContinue: function() { previewRepoFile(path, options); }});
                return Promise.resolve(null);
            }
            const requestVersion = ++_repoPreviewRequestVersion;
            const cached = repoFileByPath[path];
            if (cached) return Promise.resolve(renderRepoFile(cached, options));

            saveActiveScrollPosition();
            highlightSidebarItem(path);
            if (options.route !== false) routeToHash('/file/' + encodeURIComponent(path), true);
            document.getElementById('filePreviewTitle').textContent = path;
            const absPathForEditor = (typeof repoRoot !== 'undefined' ? repoRoot + '/' : '') + path;
            const editorBtn = document.getElementById('filePreviewEditorBtn');
            editorBtn.href = typeof buildEditorUrl === 'function' ? buildEditorUrl(absPathForEditor) : '#';
            editorBtn.textContent = '\u2197';
            editorBtn.title = 'Open in ' + (typeof editorLabel !== 'undefined' ? editorLabel : 'Editor');
            document.getElementById('filePreviewMeta').textContent = 'Loading preview…';
            document.getElementById('filePreviewBody').innerHTML = '<div class="repo-warn">Loading preview…</div>';
            document.documentElement.dataset.view = 'file';
            if (options.focus) document.getElementById('filePreviewTitle').focus({ preventScroll: true });

            return fetch('/repo-file?path=' + encodeURIComponent(path), { cache: 'no-store' })
                .then(function(response) {
                    return response.json().then(function(data) {
                        if (!response.ok || !data || !data.ok || !data.node) {
                            throw new Error((data && data.error) || 'Could not load file preview');
                        }
                        return data.node;
                    });
                })
                .then(function(node) {
                    if (!node || node.path !== path) throw new Error('File preview returned the wrong document');
                    repoFileByPath[path] = node;
                    if (requestVersion !== _repoPreviewRequestVersion) return null;
                    return renderRepoFile(node, { route: false, focus: options.focus });
                })
                .catch(function(error) {
                    if (requestVersion !== _repoPreviewRequestVersion) return null;
                    document.getElementById('filePreviewMeta').textContent = 'Preview unavailable';
                    document.getElementById('filePreviewBody').innerHTML = '';
                    var warning = document.createElement('div');
                    warning.className = 'repo-warn';
                    warning.textContent = error.message || 'Could not load file preview';
                    document.getElementById('filePreviewBody').appendChild(warning);
                    return null;
                });
        }

        function closeFilePreview() {
            saveActiveScrollPosition();
            delete document.documentElement.dataset.view;
            routeToHash('/tab/' + currentTab, true);
            restoreActiveScrollPosition();
        }

        // Re-fetch the currently previewed file from disk and re-render only
        // the file preview body, leaving other UI state untouched.
        function refreshFilePreview(options) {
            options = options || {};
            var titleEl = document.getElementById('filePreviewTitle');
            var path = titleEl ? titleEl.textContent.trim() : '';
            if (!path) return;
            if (options.changedPaths && !pathListContains(options.changedPaths, path)) return;
            var btn = document.getElementById('filePreviewRefreshBtn');
            var prevHTML = btn ? btn.innerHTML : '';
            if (btn && !options.silent) { btn.disabled = true; btn.innerHTML = '\u2026'; }
            fetch('/repo-file?path=' + encodeURIComponent(path), { cache: 'no-store' })
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (!data || !data.ok || !data.node) {
                        throw new Error((data && data.error) || 'unknown error');
                    }
                    repoFileByPath[path] = data.node;
                    if (activeRouteKey() !== '/file/' + path) return;
                    previewRepoFile(path, { route: false });
                })
                .catch(function(err) {
                    if (!options.silent) alert('Could not refresh file: ' + err);
                })
                .finally(function() {
                    if (btn && !options.silent) { btn.disabled = false; btn.innerHTML = prevHTML || '\u21BB'; }
                });
        }
