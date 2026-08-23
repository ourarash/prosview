        // ── Selection pill ───────────────────────────────────────────────────
        let pillDragging = false;
        let pillDragStartX = 0, pillDragStartY = 0;
        let pillCurrentX = 0, pillCurrentY = 0;
        let currentSelectionText = '';
        let currentSelectionRange = null;
        let selectionMemoryPreserved = false;
        let selectionPillFallbackAnchor = null;
        let selectionPillPositionFrame = null;
        let pendingSelectionActionContext = null;
        let lastValidSelectionActionContext = null;

        function nodeInsideElement(el, node) {
            if (!el || !node) return false;
            const target = node.nodeType === 3 ? node.parentNode : node;
            return !!(target && el.contains(target));
        }

        function rememberSceneSelection(sel) {
            const modalBody = document.getElementById('modalBody');
            if (!modalBody || !sel || sel.isCollapsed || !sel.rangeCount) return false;
            const text = sel.toString().trim();
            if (!text) return false;
            const range = sel.getRangeAt(0);
            if (
                !nodeInsideElement(modalBody, range.startContainer) ||
                !nodeInsideElement(modalBody, range.endContainer)
            ) {
                return false;
            }
            currentSelectionText = text;
            currentSelectionRange = range.cloneRange();
            selectionMemoryPreserved = false;
            lastValidSelectionActionContext = currentSelectionActionContext();
            return true;
        }

        function restoreSceneSelection() {
            if (!currentSelectionRange || !window.getSelection) return false;
            if (
                !document.body.contains(currentSelectionRange.startContainer) ||
                !document.body.contains(currentSelectionRange.endContainer)
            ) {
                currentSelectionRange = null;
                return false;
            }
            const sel = window.getSelection();
            if (!sel) return false;
            sel.removeAllRanges();
            sel.addRange(currentSelectionRange.cloneRange());
            return true;
        }

        function restoreSceneSelectionSoon() {
            if (!currentSelectionRange) return;
            setTimeout(function() { restoreSceneSelection(); }, 0);
        }

        function clearSceneSelectionMemory() {
            currentSelectionText = '';
            currentSelectionRange = null;
            selectionPillFallbackAnchor = null;
            selectionMemoryPreserved = false;
            clearPinnedSelectionHighlight();
        }

        function currentSelectionFlatSnapshot() {
            if (!currentSelectionRange || !_pmView || typeof aiDocTextMap !== 'function') return null;
            try {
                var from = _pmView.posAtDOM(currentSelectionRange.startContainer, currentSelectionRange.startOffset);
                var to = _pmView.posAtDOM(currentSelectionRange.endContainer, currentSelectionRange.endOffset);
                var map = aiDocTextMap(_pmView.state.doc);
                var start = -1, end = -1;
                for (var i = 0; i < map.posMap.length; i++) {
                    var pos = map.posMap[i];
                    if (pos === null) continue;
                    if (pos >= from && pos < to) {
                        if (start < 0) start = i;
                        end = i + 1;
                    }
                }
                if (start < 0 || end <= start) return null;
                var selected = map.text.slice(start, end).replace(/\s+/g, ' ').trim();
                if (selected !== currentSelectionText.replace(/\s+/g, ' ').trim()) return null;
                var path = paths[curIdx];
                var revision = meta[path] && meta[path].revision;
                if (typeof revision !== 'string' || !/^[0-9a-f]{64}$/.test(revision)) return null;
                return {
                    range: {start: start, end: end},
                    snapshot: {editor_text: map.text, source_revision: revision}
                };
            } catch (e) { return null; }
        }

        function currentSelectionFlatRange() {
            var flat = currentSelectionFlatSnapshot();
            return flat ? flat.range : null;
        }

        function currentSelectionActionContext() {
            var flat = currentSelectionFlatSnapshot();
            return {
                selection: currentSelectionText.trim(),
                range: flat ? flat.range : null,
                selectionSnapshot: flat ? flat.snapshot : null,
                liveDocument: typeof currentSceneLiveDocumentSnapshot === 'function'
                    ? currentSceneLiveDocumentSnapshot() : null,
            };
        }

        function currentSceneSelectionSnapshot(targetDocument) {
            var currentPath = paths[curIdx];
            if (!targetDocument || targetDocument.kind !== 'scene'
                || targetDocument.path !== currentPath || !_pmView
                || typeof aiDocTextMap !== 'function') return null;
            var revision = meta[currentPath] && meta[currentPath].revision;
            if (typeof revision !== 'string' || !/^[0-9a-f]{64}$/.test(revision)) return null;
            return {editor_text: aiDocTextMap(_pmView.state.doc).text, source_revision: revision};
        }

        // ── Pinned scene selection highlight ─────────────────────────────
        // The browser only ever shows one Selection at a time, and clicking
        // into the Discuss composer moves that selection off the prose. To let
        // the user select text, click into the composer, and type without
        // losing the visual marker, we paint the saved range with the CSS
        // Custom Highlight API. It survives focus changes because it isn't
        // tied to Selection.
        var PINNED_HL_NAME = 'proseview-pinned-selection';
        var _pinnedHighlight = null;

        function pinSelectionHighlight(range) {
            if (!range) return;
            if (typeof CSS === 'undefined' || !CSS.highlights || typeof Highlight === 'undefined') return;
            try {
                _pinnedHighlight = new Highlight(range);
                CSS.highlights.set(PINNED_HL_NAME, _pinnedHighlight);
            } catch (e) {
                _pinnedHighlight = null;
            }
        }

        function clearPinnedSelectionHighlight() {
            if (typeof CSS !== 'undefined' && CSS.highlights) {
                try { CSS.highlights.delete(PINNED_HL_NAME); } catch (e) {}
            }
            _pinnedHighlight = null;
        }

        function selectionViewportMetrics() {
            const zoom = parseFloat(getComputedStyle(document.body).zoom) || 1;
            const viewport = window.visualViewport;
            const left = viewport ? viewport.offsetLeft : 0;
            const top = viewport ? viewport.offsetTop : 0;
            const width = viewport ? viewport.width : window.innerWidth;
            const height = viewport ? viewport.height : window.innerHeight;
            return {zoom: zoom, left: left, top: top, right: left + width, bottom: top + height};
        }

        function clampSelectionCoordinate(value, minimum, maximum) {
            if (maximum < minimum) return minimum;
            return Math.max(minimum, Math.min(maximum, value));
        }

        function currentSelectionAnchorRect() {
            if (
                currentSelectionRange &&
                document.body.contains(currentSelectionRange.startContainer)
            ) {
                const rect = currentSelectionRange.getBoundingClientRect();
                if (rect && (rect.width || rect.height)) return rect;
            }
            return selectionPillFallbackAnchor;
        }

        function visibleSelectionPillSurface() {
            for (const id of ['selectionTodoForm', 'selectionNoteForm']) {
                const form = document.getElementById(id);
                if (form && !form.hidden) return form;
            }
            const menu = document.getElementById('selectionPillMenu');
            return menu && !menu.hidden ? menu : null;
        }

        function positionSelectionPillSurface(metrics) {
            const pill = document.getElementById('selectionPill');
            const trigger = document.getElementById('selectionPillBtn');
            const surface = visibleSelectionPillSurface();
            if (!pill || !trigger || !surface || pill.style.display === 'none') return;

            const gap = 8;
            const triggerRect = trigger.getBoundingClientRect();
            surface.style.top = 'auto';
            surface.style.bottom = 'auto';
            surface.style.left = 'auto';
            surface.style.right = 'auto';
            surface.style.maxHeight = 'none';
            surface.style.minWidth = Math.min(190, Math.max(120, (metrics.right - metrics.left - gap * 2) / metrics.zoom)) + 'px';
            surface.style.maxWidth = Math.max(120, (metrics.right - metrics.left - gap * 2) / metrics.zoom) + 'px';

            const naturalHeight = surface.scrollHeight * metrics.zoom;
            const spaceBelow = Math.max(0, metrics.bottom - triggerRect.bottom - gap);
            const spaceAbove = Math.max(0, triggerRect.top - metrics.top - gap);
            const openBelow = spaceBelow >= naturalHeight || spaceBelow >= spaceAbove;
            const availableHeight = openBelow ? spaceBelow : spaceAbove;
            surface.style.maxHeight = Math.max(48, availableHeight / metrics.zoom) + 'px';
            if (openBelow) surface.style.top = 'calc(100% + 4px)';
            else surface.style.bottom = 'calc(100% + 4px)';

            const surfaceWidth = surface.offsetWidth * metrics.zoom;
            if (triggerRect.right - surfaceWidth >= metrics.left + gap) surface.style.right = '0';
            else surface.style.left = '0';
        }

        function positionSelectionPill() {
            selectionPillPositionFrame = null;
            const pill = document.getElementById('selectionPill');
            const trigger = document.getElementById('selectionPillBtn');
            if (!pill || !trigger || pill.style.display === 'none') return;
            const anchor = currentSelectionAnchorRect();
            if (!anchor) return;

            const metrics = selectionViewportMetrics();
            const gap = 8;
            const triggerRect = trigger.getBoundingClientRect();
            const triggerWidth = triggerRect.width;
            const triggerHeight = triggerRect.height;

            let renderedLeft = anchor.right + gap;
            if (renderedLeft + triggerWidth > metrics.right - gap) {
                renderedLeft = anchor.left - triggerWidth - gap;
            }
            renderedLeft = clampSelectionCoordinate(
                renderedLeft,
                metrics.left + gap,
                metrics.right - triggerWidth - gap
            );

            let renderedTop = anchor.bottom + gap;
            if (renderedTop + triggerHeight > metrics.bottom - gap) {
                renderedTop = anchor.top - triggerHeight - gap;
            }
            renderedTop = clampSelectionCoordinate(
                renderedTop,
                metrics.top + gap,
                metrics.bottom - triggerHeight - gap
            );

            pillCurrentX = renderedLeft;
            pillCurrentY = renderedTop;
            pill.style.left = renderedLeft / metrics.zoom + 'px';
            pill.style.top = renderedTop / metrics.zoom + 'px';
            positionSelectionPillSurface(metrics);
        }

        function scheduleSelectionPillPosition() {
            if (selectionPillPositionFrame !== null) return;
            selectionPillPositionFrame = requestAnimationFrame(positionSelectionPill);
        }

        function visibleSelectionMenuItems() {
            const menu = document.getElementById('selectionPillMenu');
            if (!menu) return [];
            return Array.from(menu.querySelectorAll('.selection-pill-action')).filter(function(item) {
                return !item.hidden && item.getClientRects().length > 0 && !item.disabled;
            });
        }

        function selectionKeyboardItems() {
            return visibleSelectionMenuItems();
        }

        function setSelectionMenuExpanded(expanded, options) {
            options = options || {};
            const trigger = document.getElementById('selectionPillBtn');
            const menu = document.getElementById('selectionPillMenu');
            if (!trigger || !menu) return;
            if (!expanded) collapseSelectionPillMenu();
            else {
                ['selectionTodoForm', 'selectionNoteForm'].forEach(function(id) {
                    const form = document.getElementById(id);
                    if (form) form.hidden = true;
                });
                ['selectionTodoBtn', 'selectionNoteBtn'].forEach(function(id) {
                    const opener = document.getElementById(id);
                    if (opener) opener.setAttribute('aria-expanded', 'false');
                });
                menu.hidden = false;
            }
            trigger.setAttribute('aria-expanded', String(expanded));
            if (expanded) {
                if (currentSelectionRange) pinSelectionHighlight(currentSelectionRange);
                positionSelectionPill();
                if (options.focusFirst) {
                    const items = visibleSelectionMenuItems();
                    if (items.length) items[0].focus({preventScroll: true});
                }
            } else if (options.restoreFocus && pillIsVisible()) {
                trigger.focus({preventScroll: true});
            }
        }

        function pillIsVisible() {
            const pill = document.getElementById('selectionPill');
            return !!(
                document.documentElement.dataset.view === 'scene' &&
                pill &&
                pill.style.display !== 'none' &&
                pill.getClientRects().length > 0
            );
        }

        function openSelectionForm(formId, inputId, openerId) {
            const menu = document.getElementById('selectionPillMenu');
            const trigger = document.getElementById('selectionPillBtn');
            const form = document.getElementById(formId);
            const input = document.getElementById(inputId);
            const opener = document.getElementById(openerId);
            if (!menu || !trigger || !form || !input || !opener) return;
            [
                ['selectionTodoForm', 'selectionTodoBtn'],
                ['selectionNoteForm', 'selectionNoteBtn'],
            ].forEach(function(pair) {
                if (pair[0] === formId) return;
                const otherForm = document.getElementById(pair[0]);
                const otherButton = document.getElementById(pair[1]);
                if (otherForm) otherForm.hidden = true;
                if (otherButton) otherButton.setAttribute('aria-expanded', 'false');
            });
            menu.hidden = true;
            trigger.setAttribute('aria-expanded', 'false');
            form.hidden = false;
            opener.setAttribute('aria-expanded', 'true');
            positionSelectionPill();
            input.focus({preventScroll: true});
            input.scrollIntoView({block: 'nearest'});
            scheduleSelectionPillPosition();
        }

        function closeSelectionForm(formId, openerId, options) {
            options = options || {};
            const menu = document.getElementById('selectionPillMenu');
            const trigger = document.getElementById('selectionPillBtn');
            const form = document.getElementById(formId);
            const opener = document.getElementById(openerId);
            if (!menu || !trigger || !form || !opener) return;
            form.hidden = true;
            opener.setAttribute('aria-expanded', 'false');
            menu.hidden = false;
            trigger.setAttribute('aria-expanded', 'true');
            if (options.clearInput) {
                const input = form.querySelector('textarea');
                if (input) input.value = '';
            }
            positionSelectionPill();
            if (options.restoreFocus) opener.focus({preventScroll: true});
        }

        function getOrCreatePill() {
            let pill = document.getElementById('selectionPill');
            if (pill) return pill;
            pill = document.createElement('div');
            pill.id = 'selectionPill';
            pill.className = 'selection-pill';
            pill.innerHTML =
                '<button class="selection-pill-btn" id="selectionPillBtn" type="button" aria-label="Work with selected text" aria-haspopup="menu" aria-expanded="false" aria-controls="selectionPillMenu">···</button>' +
                '<div class="selection-pill-menu" id="selectionPillMenu" role="menu" aria-label="Work with selected text" hidden>' +
                    '<div id="selectionMenuRoot" role="none">' +
                        '<button class="selection-pill-action" id="selectionRewriteBtn" type="button" role="menuitem" tabindex="-1" aria-haspopup="menu" aria-controls="selectionRewriteMenu" aria-expanded="false">Rewrite <span aria-hidden="true">›</span></button>' +
                        '<button class="selection-pill-action" id="selectionCritiqueBtn" type="button" role="menuitem" tabindex="-1" aria-haspopup="menu" aria-controls="selectionCritiqueMenu" aria-expanded="false">Critique <span aria-hidden="true">›</span></button>' +
                        '<button class="selection-pill-action" id="selectionCodexBtn" type="button" role="menuitem" tabindex="-1">Ask about selection</button>' +
                        '<div class="selection-pill-separator" role="separator"></div>' +
                        '<button class="selection-pill-action" id="selectionSkillsBtn" type="button" role="menuitem" tabindex="-1">Skills…</button>' +
                        '<button class="selection-pill-action" id="selectionTodoBtn" type="button" role="menuitem" tabindex="-1" aria-controls="selectionTodoForm" aria-expanded="false">Add TODO</button>' +
                        '<button class="selection-pill-action" id="selectionNoteBtn" type="button" role="menuitem" tabindex="-1" aria-controls="selectionNoteForm" aria-expanded="false">Add Note</button>' +
                        '<button class="selection-pill-action" id="selectionEditorBtn" type="button" role="menuitem" tabindex="-1">Open in editor at line</button>' +
                    '</div>' +
                    '<div id="selectionRewriteMenu" role="none" hidden>' +
                        '<button class="selection-pill-action selection-menu-back" type="button" role="menuitem" tabindex="-1" data-selection-back>← Rewrite</button>' +
                        '<button class="selection-pill-action" type="button" role="menuitem" tabindex="-1" data-selection-action="rephrase">Rephrase <small>3 options</small></button>' +
                        '<button class="selection-pill-action" type="button" role="menuitem" tabindex="-1" data-selection-action="tighten">Tighten <small>2 options</small></button>' +
                        '<button class="selection-pill-action" type="button" role="menuitem" tabindex="-1" data-selection-action="clarify">Clarify <small>2 options</small></button>' +
                        '<button class="selection-pill-action" type="button" role="menuitem" tabindex="-1" data-selection-action="sensory_detail">Add sensory detail</button>' +
                        '<button class="selection-pill-action" type="button" role="menuitem" tabindex="-1" data-selection-action="show_moment">Show the moment</button>' +
                        '<button class="selection-pill-action" type="button" role="menuitem" tabindex="-1" data-selection-action="custom_rewrite" data-selection-configure="true">Custom rewrite…</button>' +
                    '</div>' +
                    '<div id="selectionCritiqueMenu" role="none" hidden>' +
                        '<button class="selection-pill-action selection-menu-back" type="button" role="menuitem" tabindex="-1" data-selection-back>← Critique</button>' +
                        '<button class="selection-pill-action" type="button" role="menuitem" tabindex="-1" data-selection-action="quick_critique">Quick critique</button>' +
                        '<button class="selection-pill-action" type="button" role="menuitem" tabindex="-1" data-selection-action="voice_character">Voice and character</button>' +
                        '<button class="selection-pill-action" type="button" role="menuitem" tabindex="-1" data-selection-action="pacing_tension">Pacing and tension</button>' +
                        '<button class="selection-pill-action" type="button" role="menuitem" tabindex="-1" data-selection-action="clarity_flow">Clarity and flow</button>' +
                        '<button class="selection-pill-action" type="button" role="menuitem" tabindex="-1" data-selection-action="continuity">Continuity check</button>' +
                    '</div>' +
                '</div>' +
                '<div class="selection-pill-form selection-todo-form" id="selectionTodoForm" role="dialog" aria-label="Add TODO to selected text" hidden>' +
                    '<textarea class="selection-todo-textarea" id="selectionTodoText" aria-label="TODO text" placeholder="Describe what needs to change..."></textarea>' +
                    '<div class="selection-todo-actions">' +
                        '<button class="selection-todo-copy-btn" id="selectionTodoCopy" type="button">Add to file</button>' +
                        '<button class="selection-todo-cancel-btn" id="selectionTodoCancel" type="button">Cancel</button>' +
                    '</div>' +
                '</div>' +
                '<div class="selection-pill-form selection-todo-form" id="selectionNoteForm" role="dialog" aria-label="Add note to selected text" hidden>' +
                    '<select class="selection-note-tag" id="selectionNoteTag" aria-label="Note tag">' +
                        '<option value="note">note</option>' +
                        '<option value="continuity">continuity</option>' +
                        '<option value="character">character</option>' +
                        '<option value="theme">theme</option>' +
                        '<option value="question">question</option>' +
                    '</select>' +
                    '<textarea class="selection-todo-textarea" id="selectionNoteText" aria-label="Note text" placeholder="Editorial observation..."></textarea>' +
                    '<div class="selection-todo-actions">' +
                        '<button class="selection-todo-copy-btn" id="selectionNoteCopy" type="button">Add to file</button>' +
                        '<button class="selection-todo-cancel-btn" id="selectionNoteCancel" type="button">Cancel</button>' +
                    '</div>' +
                '</div>';
            document.body.appendChild(pill);

            document.getElementById('selectionPillBtn').addEventListener('click', function(e) {
                e.stopPropagation();
                const menu = document.getElementById('selectionPillMenu');
                const isOpen = !menu.hidden;
                setSelectionMenuExpanded(!isOpen, {focusFirst: !isOpen});
            });

            document.getElementById('selectionEditorBtn').addEventListener('click', function(e) {
                e.stopPropagation();
                if (curIdx < 0) return;
                const p = paths[curIdx];
                const m = meta[p];
                if (!m) return;
                const selText = (window.getSelection() || {}).toString ? window.getSelection().toString().trim() : '';
                let line = (m.txt_line_offset || 0) + 1;
                if (selText && contents[p]) {
                    const needle = selText.substring(0, 50);
                    const idx = contents[p].indexOf(needle);
                    if (idx >= 0) line = (m.txt_line_offset || 0) + contents[p].substring(0, idx).split('\n').length;
                }
                window.open(buildEditorUrl(m.abs_path, line), '_blank');
                hideSelectionPill();
            });

            document.getElementById('selectionTodoBtn').addEventListener('click', function(e) {
                e.stopPropagation();
                openSelectionForm(
                    'selectionTodoForm',
                    'selectionTodoText',
                    'selectionTodoBtn'
                );
            });

            document.getElementById('selectionTodoCopy').addEventListener('click', function(e) {
                e.stopPropagation();
                const todoText = document.getElementById('selectionTodoText').value.trim();
                if (!todoText) return;
                const btn = document.getElementById('selectionTodoCopy');
                function flash(msg) {
                    const orig = btn.textContent;
                    btn.textContent = msg || orig;
                    btn.disabled = true;
                    setTimeout(function() { btn.textContent = orig; btn.disabled = false; }, 1800);
                }
                if (curIdx < 0) return;
                const p = paths[curIdx];
                const m = meta[p];
                if (!m) return;
                btn.disabled = true;
                let finalTodoText = todoText;
                if (currentSelectionText && currentSelectionText.trim().length > 0) {
                    let quote = currentSelectionText.trim().replace(/\s+/g, ' ');
                    if (quote.length > 80) quote = quote.substring(0, 80) + '...';
                    finalTodoText = todoText + ' | "' + quote + '"';
                }
                fetch('/insert-todo', {
                    method: 'POST',
                    headers: pvHeaders(),
                    body: JSON.stringify({
                        abs_path: m.abs_path,
                        selection_text: currentSelectionText,
                        txt_line_offset: m.txt_line_offset || 0,
                        todo_text: finalTodoText,
                        open_mtime: m.mtime,
                    })
                }).then(function(r) { return r.json(); }).then(function(data) {
                    if (data.ok) {
                        flash('Added!');
                        setTimeout(function() { location.reload(); }, 700);
                    } else {
                        btn.disabled = false;
                        alert('Could not insert TODO: ' + (data.error || 'unknown error'));
                    }
                }).catch(function(err) {
                    btn.disabled = false;
                    alert('Request failed: ' + err);
                });
            });

            document.getElementById('selectionTodoCancel').addEventListener('click', function(e) {
                e.stopPropagation();
                closeSelectionForm('selectionTodoForm', 'selectionTodoBtn', {
                    clearInput: true,
                    restoreFocus: true,
                });
            });

            document.getElementById('selectionNoteBtn').addEventListener('click', function(e) {
                e.stopPropagation();
                openSelectionForm(
                    'selectionNoteForm',
                    'selectionNoteText',
                    'selectionNoteBtn'
                );
            });

            document.getElementById('selectionNoteCopy').addEventListener('click', function(e) {
                e.stopPropagation();
                const noteText = document.getElementById('selectionNoteText').value.trim();
                if (!noteText) return;
                const tag = document.getElementById('selectionNoteTag').value;
                const btn = document.getElementById('selectionNoteCopy');
                function flash(msg) {
                    const orig = btn.textContent;
                    btn.textContent = msg;
                    btn.disabled = true;
                    setTimeout(function() { btn.textContent = orig; btn.disabled = false; }, 1800);
                }
                if (curIdx < 0) return;
                const p = paths[curIdx];
                const m = meta[p];
                if (!m) return;
                btn.disabled = true;
                fetch('/add-note', {
                    method: 'POST',
                    headers: pvHeaders(),
                    body: JSON.stringify({
                        abs_path: m.abs_path,
                        selection_text: currentSelectionText,
                        txt_line_offset: m.txt_line_offset || 0,
                        note_text: noteText,
                        tag: tag,
                        open_mtime: m.mtime,
                    })
                }).then(function(r) { return r.json(); }).then(function(data) {
                    if (data.ok) {
                        flash('Added!');
                        setTimeout(function() { location.reload(); }, 700);
                    } else {
                        btn.disabled = false;
                        alert('Could not insert note: ' + (data.error || 'unknown error'));
                    }
                }).catch(function(err) {
                    btn.disabled = false;
                    alert('Request failed: ' + err);
                });
            });

            document.getElementById('selectionNoteCancel').addEventListener('click', function(e) {
                e.stopPropagation();
                closeSelectionForm('selectionNoteForm', 'selectionNoteBtn', {
                    clearInput: true,
                    restoreFocus: true,
                });
            });

            function showSelectionSubmenu(id) {
                pendingSelectionActionContext = currentSelectionText
                    ? currentSelectionActionContext() : lastValidSelectionActionContext;
                var opener = document.getElementById(id === 'selectionRewriteMenu' ? 'selectionRewriteBtn' : 'selectionCritiqueBtn');
                document.getElementById('selectionMenuRoot').hidden = true;
                document.getElementById('selectionRewriteMenu').hidden = id !== 'selectionRewriteMenu';
                document.getElementById('selectionCritiqueMenu').hidden = id !== 'selectionCritiqueMenu';
                document.getElementById('selectionRewriteBtn').setAttribute('aria-expanded', String(id === 'selectionRewriteMenu'));
                document.getElementById('selectionCritiqueBtn').setAttribute('aria-expanded', String(id === 'selectionCritiqueMenu'));
                document.getElementById('selectionPillMenu').setAttribute('aria-label', opener.textContent.trim() + ' actions');
                positionSelectionPill();
                var first = document.querySelector('#' + id + ' .selection-pill-action');
                if (first) first.focus({preventScroll: true});
            }

            function managedSelectionIsCurrent() {
                return !!(currentSelectionText && currentSelectionRange);
            }

            function handoffSelectionToDiscuss(options) {
                var frozen = pendingSelectionActionContext;
                var selection = frozen ? frozen.selection : currentSelectionText.trim();
                var selectionRange = frozen ? frozen.range : currentSelectionFlatRange();
                var selectionSnapshot = frozen ? frozen.selectionSnapshot : (
                    currentSelectionFlatSnapshot() || {}
                ).snapshot || null;
                var liveDocument = frozen ? frozen.liveDocument : (
                    typeof currentSceneLiveDocumentSnapshot === 'function' ? currentSceneLiveDocumentSnapshot() : null
                );
                if (currentSelectionRange) pinSelectionHighlight(currentSelectionRange);
                selectionMemoryPreserved = true;
                setSelectionMenuExpanded(false, {restoreFocus: true});
                openDiscussForSelection(document.getElementById('selectionPillBtn'), selection, Object.assign({
                    selectionRange: selectionRange,
                    selectionSnapshot: selectionSnapshot,
                    liveDocument: liveDocument
                }, options || {}));
                pendingSelectionActionContext = null;
            }

            document.getElementById('selectionRewriteBtn').addEventListener('click', function(e) {
                e.stopPropagation(); showSelectionSubmenu('selectionRewriteMenu');
            });
            document.getElementById('selectionCritiqueBtn').addEventListener('click', function(e) {
                e.stopPropagation(); showSelectionSubmenu('selectionCritiqueMenu');
            });
            pill.querySelectorAll('[data-selection-back]').forEach(function(button) {
                button.addEventListener('click', function(e) {
                    e.stopPropagation();
                    if (!managedSelectionIsCurrent()) return;
                    document.getElementById('selectionMenuRoot').hidden = false;
                    document.getElementById('selectionRewriteMenu').hidden = true;
                    document.getElementById('selectionCritiqueMenu').hidden = true;
                    document.getElementById('selectionRewriteBtn').setAttribute('aria-expanded', 'false');
                    document.getElementById('selectionCritiqueBtn').setAttribute('aria-expanded', 'false');
                    document.getElementById('selectionPillMenu').setAttribute('aria-label', 'Work with selected text');
                    pendingSelectionActionContext = null;
                    positionSelectionPill();
                    var target = button.closest('#selectionRewriteMenu') ? 'selectionRewriteBtn' : 'selectionCritiqueBtn';
                    document.getElementById(target).focus({preventScroll: true});
                });
            });
            pill.querySelectorAll('[data-selection-action]').forEach(function(button) {
                button.addEventListener('click', function(e) {
                    e.stopPropagation();
                    var actionId = button.dataset.selectionAction;
                    handoffSelectionToDiscuss({actionId: actionId, runImmediately: button.dataset.selectionConfigure !== 'true'});
                });
            });

            document.getElementById('selectionSkillsBtn').addEventListener('click', function(e) {
                e.stopPropagation();
                if (!managedSelectionIsCurrent()) return;
                handoffSelectionToDiscuss({showSkills: true});
            });

            document.getElementById('selectionCodexBtn').addEventListener('click', function(e) {
                e.stopPropagation();
                if (!managedSelectionIsCurrent()) return;
                handoffSelectionToDiscuss({});
            });

            // Drag: only when clicking the pill background (not a button/textarea)
            pill.addEventListener('mousedown', function(e) {
                if (e.target.closest('button') || e.target.tagName === 'TEXTAREA') return;
                const rect = pill.getBoundingClientRect();
                pillDragging = true;
                pillDragStartX = e.clientX - rect.left;
                pillDragStartY = e.clientY - rect.top;
                e.preventDefault();
            });

            return pill;
        }

        function collapseSelectionPillMenu() {
            const menu = document.getElementById('selectionPillMenu');
            if (menu) {
                menu.hidden = true;
                menu.setAttribute('role', 'menu');
                menu.setAttribute('aria-label', 'Work with selected text');
            }
            const root = document.getElementById('selectionMenuRoot');
            const rewrite = document.getElementById('selectionRewriteMenu');
            const critique = document.getElementById('selectionCritiqueMenu');
            if (root) root.hidden = false;
            if (rewrite) rewrite.hidden = true;
            if (critique) critique.hidden = true;
            ['selectionRewriteBtn', 'selectionCritiqueBtn'].forEach(function(id) {
                const el = document.getElementById(id);
                if (el) el.setAttribute('aria-expanded', 'false');
            });
            ['selectionTodoForm', 'selectionNoteForm'].forEach(function(id) {
                const el = document.getElementById(id);
                if (el) el.hidden = true;
            });
            ['selectionTodoText', 'selectionNoteText'].forEach(function(id) {
                const el = document.getElementById(id);
                if (el) el.value = '';
            });
            ['selectionTodoBtn', 'selectionNoteBtn'].forEach(function(id) {
                const el = document.getElementById(id);
                if (el) el.setAttribute('aria-expanded', 'false');
            });
            const trigger = document.getElementById('selectionPillBtn');
            if (trigger) trigger.setAttribute('aria-expanded', 'false');
        }

        function showSelectionPill(x, y, selText) {
            currentSelectionText = selText || '';
            const pill = getOrCreatePill();
            collapseSelectionPillMenu();
            selectionPillFallbackAnchor = {left: x, right: x, top: y, bottom: y};
            pill.style.display = 'flex';
            positionSelectionPill();
        }

        function hideSelectionPill() {
            const pill = document.getElementById('selectionPill');
            if (pill) pill.style.display = 'none';
            collapseSelectionPillMenu();
            clearPinnedSelectionHighlight();
        }

        function openSelectionCommandMenuFromKeyboard() {
            if (document.documentElement.dataset.view !== 'scene') return false;
            const liveSelection = window.getSelection ? window.getSelection() : null;
            if (liveSelection && !liveSelection.isCollapsed && liveSelection.rangeCount) {
                if (!rememberSceneSelection(liveSelection)) return false;
            } else if (!selectionMemoryPreserved) {
                clearSceneSelectionMemory();
                return false;
            }
            if (!currentSelectionText || !currentSelectionRange) return false;
            if (
                !document.body.contains(currentSelectionRange.startContainer) ||
                !document.body.contains(currentSelectionRange.endContainer)
            ) {
                clearSceneSelectionMemory();
                return false;
            }
            const rect = currentSelectionRange.getBoundingClientRect();
            if (!pillIsVisible()) showSelectionPill(rect.right, rect.bottom, currentSelectionText);
            setSelectionMenuExpanded(true, {focusFirst: true});
            return true;
        }

        function closeVisibleSelectionSubsurface() {
            const root = document.getElementById('selectionMenuRoot');
            const rewrite = document.getElementById('selectionRewriteMenu');
            const critique = document.getElementById('selectionCritiqueMenu');
            if (root && root.hidden && ((rewrite && !rewrite.hidden) || (critique && !critique.hidden))) {
                const wasRewrite = !!(rewrite && !rewrite.hidden);
                root.hidden = false;
                if (rewrite) rewrite.hidden = true;
                if (critique) critique.hidden = true;
                document.getElementById('selectionRewriteBtn').setAttribute('aria-expanded', 'false');
                document.getElementById('selectionCritiqueBtn').setAttribute('aria-expanded', 'false');
                document.getElementById('selectionPillMenu').setAttribute('aria-label', 'Work with selected text');
                positionSelectionPill();
                document.getElementById(wasRewrite ? 'selectionRewriteBtn' : 'selectionCritiqueBtn').focus({preventScroll: true});
                return true;
            }
            const forms = [
                ['selectionTodoForm', 'selectionTodoBtn'],
                ['selectionNoteForm', 'selectionNoteBtn'],
            ];
            for (const pair of forms) {
                const form = document.getElementById(pair[0]);
                if (form && !form.hidden) {
                    closeSelectionForm(pair[0], pair[1], {restoreFocus: true});
                    return true;
                }
            }
            return false;
        }

        function focusableSelectionDialogControls(dialog) {
            if (!dialog) return [];
            return Array.from(dialog.querySelectorAll(
                'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
            )).filter(function(control) {
                return control.getClientRects().length > 0 && !control.hidden;
            });
        }

        function moveFocusBeyondSelectionPill(reverse) {
            const pill = document.getElementById('selectionPill');
            const candidates = Array.from(document.querySelectorAll(
                'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [contenteditable="true"], [tabindex]:not([tabindex="-1"])'
            )).filter(function(control) {
                return (!pill || !pill.contains(control)) &&
                    control.tabIndex >= 0 &&
                    control.getClientRects().length > 0 &&
                    getComputedStyle(control).visibility !== 'hidden';
            });
            const destination = reverse ? candidates[candidates.length - 1] : candidates[0];
            if (destination) destination.focus({preventScroll: true});
            else if (pill) document.getElementById('selectionPillBtn').focus({preventScroll: true});
        }

        function handleSelectionPillKeydown(e) {
            if (!pillIsVisible()) return;
            const menu = document.getElementById('selectionPillMenu');
            if (e.key === 'Tab') {
                const dialog = visibleSelectionPillSurface();
                if (dialog && dialog.getAttribute('role') === 'dialog') {
                    const controls = focusableSelectionDialogControls(dialog);
                    if (!controls.length) return;
                    const activeIndex = controls.indexOf(document.activeElement);
                    if (activeIndex < 0 || (e.shiftKey && activeIndex === 0)) {
                        controls[controls.length - 1].focus({preventScroll: true});
                        e.preventDefault();
                        e.stopImmediatePropagation();
                    } else if (!e.shiftKey && activeIndex === controls.length - 1) {
                        controls[0].focus({preventScroll: true});
                        e.preventDefault();
                        e.stopImmediatePropagation();
                    }
                    return;
                }
                if (menu && !menu.hidden) {
                    setSelectionMenuExpanded(false);
                    moveFocusBeyondSelectionPill(e.shiftKey);
                    e.preventDefault();
                    e.stopImmediatePropagation();
                    return;
                }
            }
            if (e.key === 'Escape') {
                if (closeVisibleSelectionSubsurface()) {
                    e.preventDefault();
                    e.stopImmediatePropagation();
                    return;
                }
                if (menu && !menu.hidden) {
                    setSelectionMenuExpanded(false, {restoreFocus: true});
                    e.preventDefault();
                    e.stopImmediatePropagation();
                    return;
                }
                hideSelectionPill();
                e.preventDefault();
                e.stopImmediatePropagation();
                return;
            }
            if (!menu || menu.hidden || menu.getAttribute('role') !== 'menu') return;
            if (e.key === 'ArrowRight' && (document.activeElement === document.getElementById('selectionRewriteBtn') || document.activeElement === document.getElementById('selectionCritiqueBtn'))) {
                document.activeElement.click();
                e.preventDefault();
                e.stopPropagation();
                return;
            }
            if (e.key === 'ArrowLeft') {
                const root = document.getElementById('selectionMenuRoot');
                if (root && root.hidden && closeVisibleSelectionSubsurface()) {
                    e.preventDefault();
                    e.stopPropagation();
                    return;
                }
            }
            if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return;
            const items = selectionKeyboardItems();
            if (!items.length) return;
            let index = items.indexOf(document.activeElement);
            if (e.key === 'Home') index = 0;
            else if (e.key === 'End') index = items.length - 1;
            else if (e.key === 'ArrowDown') index = index < 0 ? 0 : (index + 1) % items.length;
            else index = index < 0 ? items.length - 1 : (index - 1 + items.length) % items.length;
            items[index].focus({preventScroll: true});
            e.preventDefault();
            e.stopPropagation();
        }

        function initSelectionPill() {
            const modalBody = document.getElementById('modalBody');
            if (!modalBody) return;

            function exposeCurrentSceneSelection() {
                const sel = window.getSelection();
                if (!rememberSceneSelection(sel)) return false;
                clearPinnedSelectionHighlight();
                const rect = currentSelectionRange.getBoundingClientRect();
                showSelectionPill(rect.right, rect.bottom, currentSelectionText);
                return true;
            }

            // Both interaction paths defer reading the selection so the browser
            // can settle. In edit mode ProseMirror rebuilds the DOM selection
            // asynchronously, so that later read sometimes finds it collapsed
            // and the pill never appears -- which a writer experiences as the
            // action menu simply not opening, intermittently. Capturing
            // synchronously first keeps the selection the reader actually made,
            // and the deferred pass falls back to it.
            function exposeSelectionAfterSettling(delayMs) {
                const captured = rememberSceneSelection(window.getSelection());
                setTimeout(function() {
                    if (exposeCurrentSceneSelection()) return;
                    if (captured && currentSelectionText && currentSelectionRange) {
                        clearPinnedSelectionHighlight();
                        const rect = currentSelectionRange.getBoundingClientRect();
                        showSelectionPill(rect.right, rect.bottom, currentSelectionText);
                        return;
                    }
                    hideSelectionPill();
                    clearSceneSelectionMemory();
                }, delayMs);
            }

            modalBody.addEventListener('mouseup', function(e) {
                const pill = document.getElementById('selectionPill');
                if (pill && pill.contains(e.target)) return;
                exposeSelectionAfterSettling(10);
            });

            // Keyboard selection does not emit mouseup. Keyup fires after the
            // browser has extended the native selection, so expose the exact
            // same action trigger without inventing a second interaction path.
            modalBody.addEventListener('keyup', function(event) {
                const selectionNavigation = ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'];
                const mayChangeSelection = selectionNavigation.includes(event.key) || (
                    (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a'
                );
                if (!mayChangeSelection) return;
                exposeSelectionAfterSettling(0);
            });

            document.addEventListener('mousedown', function(e) {
                const pill = document.getElementById('selectionPill');
                if (!pill || pill.style.display === 'none') return;
                if (e.target.closest('#discussPanel')) return;
                if (!pill.contains(e.target) && !e.target.closest('#modalBody')) {
                    hideSelectionPill();
                    clearSceneSelectionMemory();
                }
            });

            document.addEventListener('mousemove', function(e) {
                if (!pillDragging) return;
                const pill = document.getElementById('selectionPill');
                const trigger = document.getElementById('selectionPillBtn');
                if (!pill || !trigger) return;
                const metrics = selectionViewportMetrics();
                const rect = trigger.getBoundingClientRect();
                pillCurrentX = clampSelectionCoordinate(
                    e.clientX - pillDragStartX,
                    metrics.left + 8,
                    metrics.right - rect.width - 8
                );
                pillCurrentY = clampSelectionCoordinate(
                    e.clientY - pillDragStartY,
                    metrics.top + 8,
                    metrics.bottom - rect.height - 8
                );
                pill.style.left = pillCurrentX / metrics.zoom + 'px';
                pill.style.top = pillCurrentY / metrics.zoom + 'px';
                positionSelectionPillSurface(metrics);
            });

            document.addEventListener('mouseup', function() { pillDragging = false; });

            document.addEventListener('keydown', handleSelectionPillKeydown, true);
            modalBody.addEventListener('scroll', scheduleSelectionPillPosition, {passive: true});
            const modalScroller = modalBody.closest('.modal-content');
            if (modalScroller && modalScroller !== modalBody) {
                modalScroller.addEventListener('scroll', scheduleSelectionPillPosition, {passive: true});
            }
            window.addEventListener('scroll', scheduleSelectionPillPosition, {passive: true});
            window.addEventListener('resize', scheduleSelectionPillPosition);
            if (window.visualViewport) {
                window.visualViewport.addEventListener('resize', scheduleSelectionPillPosition);
                window.visualViewport.addEventListener('scroll', scheduleSelectionPillPosition);
            }
            new MutationObserver(scheduleSelectionPillPosition).observe(document.body, {
                attributes: true,
                attributeFilter: ['style'],
            });
            getOrCreatePill().addEventListener('pointerup', scheduleSelectionPillPosition);
        }

        // Register the layered selection Escape handler before the edit-mode
        // handler below so the closest visible surface always closes first.
        initSelectionPill();

        // Global Esc -> exit edit mode (with unsaved-changes confirmation).
        // Capture phase so we run before any popover/selection handlers and
        // before ProseMirror's keymap.
        document.addEventListener('keydown', function(e) {
            if (e.key !== 'Escape') return;
            // Native top-layer dialogs own Escape while open. Intercepting it
            // here would put the edit confirmation behind that dialog and make
            // both surfaces unreachable.
            if (document.querySelector('dialog[open]')) return;
            // Don't interfere if our own confirmation dialog is up; it has
            // its own Esc handler.
            if (_unsavedDialog) return;
            // Don't interfere if the diff modal is open
            const diffModal = document.getElementById('diffModalOverlay');
            if (diffModal && !diffModal.hidden) return;
            if (tryEscapeEditMode()) {
                e.preventDefault();
                e.stopPropagation();
            }
        }, true);

        var _refreshTimer = null;
        var _refreshInFlight = false;
        var _refreshQueued = false;

        function pathListContains(pathsList, path) {
            if (!pathsList || !pathsList.length || !path) return true;
            return pathsList.some(function(changed) {
                return changed === path || changed.endsWith('/' + path);
            });
        }

        function reloadOrDefer(changedPaths) {
            // Always do a partial refresh instead of location.reload() —
            // a full reload jolts the viewport (jump to top, then
            // scroll-restore back) and tears down the live editor. This
            // covers both self-saves and external file edits.
            // refreshContent() is a no-op for the open modal while
            // _pmEditMode is true so the editor stays mounted.
            if (_pendingSelfReloads > 0) _pendingSelfReloads--;
            refreshContent(changedPaths || null);
        }

        function scheduleContentRefresh(delay) {
            if (_refreshInFlight) {
                _refreshQueued = true;
                return;
            }
            if (_refreshTimer) clearTimeout(_refreshTimer);
            _refreshTimer = setTimeout(function() {
                _refreshTimer = null;
                refreshContent();
            }, delay);
        }

        function refreshContent(changedPaths) {
            var view = document.documentElement.dataset.view || '';
            if (view === 'file') {
                var titleEl = document.getElementById('filePreviewTitle');
                var filePath = titleEl ? titleEl.textContent.trim() : '';
                if (pathListContains(changedPaths, filePath) && typeof refreshFilePreview === 'function') {
                    refreshFilePreview({ silent: true, changedPaths: changedPaths || null });
                }
                return;
            }
            if (view !== 'scene' || curIdx < 0 || _pmEditMode) return;
            var scenePath = paths[curIdx];
            if (!pathListContains(changedPaths, scenePath)) return;
            if (_refreshInFlight) {
                _refreshQueued = true;
                return;
            }
            if (_refreshTimer) {
                clearTimeout(_refreshTimer);
                _refreshTimer = null;
            }
            _refreshInFlight = true;
            var refreshSucceeded = false;
            fetch('/scene-data?path=' + encodeURIComponent(scenePath), { cache: 'no-store' }).then(function(r) {
                if (!r.ok) throw new Error('Refresh failed: ' + r.status);
                return r.json();
            }).then(function(data) {
                if (data.contents && data.contents[scenePath] !== undefined) {
                    var oldContent = contents[scenePath] || '';
                    var newContent = data.contents[scenePath];
                    if (oldContent !== newContent && oldContent !== '') {
                        var oldParas = oldContent.split('\n\n');
                        var newParas = newContent.split('\n\n');
                        var changed = [];
                        for (var i = 0; i < newParas.length; i++) {
                            if (i >= oldParas.length || newParas[i] !== oldParas[i]) {
                                changed.push(i);
                            }
                        }
                        window._lastExternalChangeIndices = changed;
                    }
                    contents[scenePath] = newContent;
                }
                if (data.meta && data.meta[scenePath]) meta[scenePath] = data.meta[scenePath];
                if (data.highlightsByPath && data.highlightsByPath[scenePath]) highlightsByPath[scenePath] = data.highlightsByPath[scenePath];
                refreshSucceeded = true;
                updateModal(true);
            }).catch(function() {}).finally(function() {
                _refreshInFlight = false;
                if (!refreshSucceeded) return;
                if (_refreshQueued) {
                    _refreshQueued = false;
                    scheduleContentRefresh(0);
                }
            });
        }

        function hotSwapCss() {
            fetch('/app.css?t=' + Date.now(), { cache: 'no-store' })
                .then(function(r) { return r.text(); })
                .then(function(css) {
                    var styleEl = document.getElementById('proseview-app-css');
                    if (styleEl) styleEl.textContent = css;
                })
                .catch(function() {});
        }

        function showAssetReloadBanner() {
            // The only thing a reload destroys is an in-progress unsaved
            // edit. So we only surface the banner when the user is in edit
            // mode -- otherwise just refresh silently.
            var editing = (typeof _pmEditMode !== 'undefined') && _pmEditMode;
            if (!editing) {
                location.reload();
                return;
            }
            if (document.getElementById('assetReloadBanner')) return;
            var banner = document.createElement('div');
            banner.id = 'assetReloadBanner';
            banner.className = 'asset-reload-banner';
            banner.innerHTML =
                '<span class="asset-reload-banner-msg">Page assets updated</span>' +
                '<button type="button" class="asset-reload-banner-btn" onclick="location.reload()">Reload</button>' +
                '<button type="button" class="asset-reload-banner-dismiss" title="Dismiss" onclick="this.parentElement.remove()">&times;</button>';
            document.body.appendChild(banner);
        }

        (function connectSSE() {
            const es = new EventSource(pvEventSourceUrl('/events'));
            es.onmessage = function(e) {
                if (!e.data) return;
                var payload = null;
                if (e.data.charAt(0) === '{') {
                    try { payload = JSON.parse(e.data); } catch(err) { payload = null; }
                }
                if (payload && payload.type === 'ai:proposal' && typeof handleAiProposalEvent === 'function') {
                    handleAiProposalEvent(payload);
                } else if ((payload && (payload.type === 'reload' || payload.type === 'reload:content')) ||
                    e.data === 'reload' || e.data === 'reload:content') {
                    reloadOrDefer(payload && Array.isArray(payload.paths) ? payload.paths : null);
                } else if (e.data === 'reload:css') {
                    hotSwapCss();
                } else if (e.data === 'reload:html' || e.data === 'reload:js') {
                    showAssetReloadBanner();
                }
            };
            es.onerror = function() {
                if (es.readyState === EventSource.CLOSED) return;
                es.close();
                setTimeout(connectSSE, 3000);
            };
        })();
