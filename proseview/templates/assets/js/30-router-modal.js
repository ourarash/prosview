        function activeRouteKey() {
            const route = parseHashRoute();
            if (!route) return '/tab/' + (currentTab || 'overview');
            if (route.kind === 'tab') return '/tab/' + (VALID_TABS.includes(route.arg) ? route.arg : 'overview');
            if (route.kind === 'scene' && route.arg) return '/scene/' + route.arg;
            if (route.kind === 'file' && route.arg) return '/file/' + route.arg;
            return '/tab/' + (currentTab || 'overview');
        }

        function activeScrollContainer() {
            const route = parseHashRoute();
            if (route && route.kind === 'scene') return document.querySelector('#sceneModal .modal-content');
            if (route && route.kind === 'file') return document.getElementById('filePreviewBody');
            return window;
        }

        function readScrollTop(container) {
            if (!container) return null;
            return container === window ? (window.scrollY || window.pageYOffset || 0) : container.scrollTop;
        }

        function writeScrollTop(container, top) {
            if (!container) return;
            // Use 'instant' behavior so route restoration bypasses
            // scroll-behavior:smooth on .modal-content. Without this,
            // a refresh visibly animates from 0 to the saved position.
            if (typeof container.scrollTo === 'function') {
                try {
                    container.scrollTo({ top: top, left: 0, behavior: 'instant' });
                    return;
                } catch (err) {
                    // 'instant' rejected by older browsers; fall through.
                }
            }
            if (container === window) window.scrollTo(0, top);
            else container.scrollTop = top;
        }

        function loadSavedScrollTop(key) {
            try {
                const raw = sessionStorage.getItem(VIEW_SCROLL_STORAGE_PREFIX + key);
                if (raw === null) return null;
                const parsed = parseInt(raw, 10);
                return Number.isNaN(parsed) ? null : Math.max(0, parsed);
            } catch (err) {
                return null;
            }
        }

        function saveActiveScrollPosition() {
            if (routeHydrating) return;
            const key = activeRouteKey();
            const top = readScrollTop(activeScrollContainer());
            if (!key || top === null) return;
            try {
                sessionStorage.setItem(VIEW_SCROLL_STORAGE_PREFIX + key, String(Math.round(top)));
            } catch (err) {
                // Ignore storage errors and keep default refresh behavior.
            }
        }

        function scheduleScrollSave() {
            if (scrollSaveQueued) return;
            scrollSaveQueued = true;
            requestAnimationFrame(function() {
                scrollSaveQueued = false;
                saveActiveScrollPosition();
            });
        }

        function restoreActiveScrollPosition() {
            const top = loadSavedScrollTop(activeRouteKey());
            if (top === null) return;
            const delays = [0, 40, 120, 260];
            delays.forEach(function(delay) {
                window.setTimeout(function() {
                    const container = activeScrollContainer();
                    if (!container) return;
                    writeScrollTop(container, top);
                }, delay);
            });
        }

        function buildEditorUrl(absPath, line) {
            const lineVal = (line && line > 1) ? line : 1;
            if (editorScheme === 'custom' && editorUrlTemplate) {
                return editorUrlTemplate
                    .replace('{abs_path}', encodeURI(absPath))
                    .replace('{line}', String(lineVal));
            }
            const base = editorScheme + '://file/' + encodeURI(absPath);
            return lineVal > 1 ? base + ':' + lineVal : base;
        }

        // Highlight toggles persist across scenes and across reloads.
        // The user's pick of "show passive voice" is a global preference,
        // not a per-scene state, so resetting on every open felt fiddly.
        function _loadHighlightPrefs() {
            var defaults = {};
            PASS_ORDER.forEach(function(p) { defaults[p] = false; });
            try {
                var raw = localStorage.getItem(HIGHLIGHTS_STORAGE_KEY);
                if (!raw) return defaults;
                var saved = JSON.parse(raw);
                if (!saved || typeof saved !== 'object') return defaults;
                PASS_ORDER.forEach(function(p) {
                    if (typeof saved[p] === 'boolean') defaults[p] = saved[p];
                });
            } catch (err) {
                // Ignore storage / JSON errors and fall back to defaults.
            }
            return defaults;
        }

        function _saveHighlightPrefs() {
            try {
                localStorage.setItem(HIGHLIGHTS_STORAGE_KEY, JSON.stringify(hls));
            } catch (err) {
                // localStorage is full / disabled; the current-session
                // toggles still work, just not across reloads.
            }
        }

        function openSceneModal(p) {
            // Guard against a path that is not in the scene index: rendering
            // meta[undefined] throws and leaves the user on a dead click.
            if (paths.indexOf(p) === -1) return;
            if (guardDirtySceneNavigation()) {
                showUnsavedDialog({onContinue: function() { openSceneModal(p); }});
                return false;
            }
            saveActiveScrollPosition();
            curIdx = paths.indexOf(p);
            hls = _loadHighlightPrefs();
            updateModal();
            resetSceneToolbarForRoute();
            document.documentElement.dataset.view = 'scene';
            routeToHash('/scene/' + encodeURIComponent(p), true);
            restoreActiveScrollPosition();
            // Reveal the scene in the sidebar: highlight it and expand the
            // chapter folders above it.
            if (typeof revealSidebarItem === 'function') revealSidebarItem({ scenePath: p });
            if (typeof discussFollowActiveDocument === 'function') discussFollowActiveDocument();
            // Deliberately does not open the dock. Forcing it open here made
            // `body.discuss-open` -- and its `margin-right` -- apply to every
            // scene, which silently narrowed the reading column, the file
            // preview and the search reflow that eight tests measure. The dock
            // is opened by the Panel button, a keyboard shortcut, or a
            // selection action: all things the reader asked for.
            return true;
        }

        function openRelatedDoc(path) {
            if (guardDirtySceneNavigation()) {
                showUnsavedDialog({onContinue: function() { openRelatedDoc(path); }});
                return false;
            }
            if (!closeSceneModal({route: false})) return false;
            previewRepoFile(path);
            return true;
        }

        function updateModal(preserveScroll) {
            const p = paths[curIdx], m = meta[p], b = document.getElementById('modalBody'), a = document.getElementById('modalAlerts'), s = document.getElementById('modalStats');
            const modalTitle = document.getElementById('modalTitle');
            modalTitle.replaceChildren();
            const literaryTitle = document.createElement('span');
            literaryTitle.className = 'scene-literary-title';
            literaryTitle.textContent = m.title || p.split('/').pop().replace(/\.md$/i, '').replace(/[-_]+/g, ' ');
            const technicalPath = document.createElement('span');
            technicalPath.className = 'scene-technical-path';
            technicalPath.textContent = p;
            modalTitle.append(literaryTitle, technicalPath);
            const _modalEditorBtn = document.getElementById('modalEditorBtn');
            _modalEditorBtn.style.display = 'flex';
            _modalEditorBtn.href = buildEditorUrl(m.abs_path);
            _modalEditorBtn.title = 'Open in ' + editorLabel;

            // Built as nodes rather than a string: several of these tiles are
            // buttons that toggle the highlight pass they were computed from,
            // so the reader can see which words produced the number instead of
            // being asked to trust it.
            s.replaceChildren();
            let mattrBox = null;
            let mtldBox = null;
            const mBand = window.lexicalBands ? window.lexicalBands.mattr : [0.65, 0.85];
            const lBand = window.lexicalBands ? window.lexicalBands.mtld : [50, 180];
            const mTarget = 'Typical: ' + mBand[0] + ' - ' + mBand[1];
            const lTarget = 'Typical: ' + lBand[0] + ' - ' + lBand[1];

            const md = window.medians || {};
            const hintAvg = function(key, formatFn) {
                if (md[key] === undefined) return '';
                return `\n\nBook median: ${formatFn(md[key])}`;
            };
            const isOutlier = function(val, med, threshold = 0.5) {
                if (!val || !med) return false;
                return val > med * (1 + threshold) || val < med * (1 - threshold);
            };

            [
                {value: m.words.toLocaleString(), label: 'Words', unit: 'in this scene',
                 warn: isOutlier(m.words, md.words),
                 hint: 'Total word count of the prose in this scene.' + hintAvg('words', v => Math.round(v).toLocaleString())},
                {value: m.dlg_pct.toFixed(1) + '%', label: 'Dialogue',
                 warn: isOutlier(m.dlg_pct, md.dlg_pct),
                 unit: 'of all words', hint: 'Ratio of words in quotes to all words.' + hintAvg('dlg_pct', v => v.toFixed(1) + '%')},
                {value: m.sent_stdev ? m.sent_stdev.toFixed(1) : '0.0', label: 'Sent. Variation', unit: 'standard deviation', 
                 warn: isOutlier(m.sent_stdev, md.sent_stdev),
                 hint: 'Mathematical standard deviation of your sentence lengths.\n\n• High: good variation between short punchy sentences and long flowing ones.\n• Low: monotonous pacing where all sentences are a similar length.' + hintAvg('sent_stdev', v => v.toFixed(1))},
                {value: m.sensory.toFixed(1), label: 'Sensory', unit: 'per 1,000 words', pass: 'sensory',
                 warn: isOutlier(m.sensory, md.sensory),
                 hint: 'Sight, sound, smell, touch and taste words per thousand.\n\n• The Sensory pass marks them in the prose.' + hintAvg('sensory', v => v.toFixed(1))},
                {value: m.first_person.toFixed(1), label: '1st Person', unit: 'per 1,000 words',
                 pass: 'first_person',
                 warn: isOutlier(m.first_person, md.first_person),
                 hint: 'First-person pronouns per thousand words.\n\n• The First Person pass marks them.' + hintAvg('first_person', v => v.toFixed(1))},
                {value: m.passive.toFixed(1), label: 'Passive', unit: 'per 1,000 words',
                 pass: 'passive_voice',
                 warn: isOutlier(m.passive, md.passive),
                 hint: 'Passive constructions per thousand words.\n\n• The Passive Voice pass marks them.' + hintAvg('passive', v => v.toFixed(1))},
                {value: m.avg_sent.toFixed(1), label: 'Avg. Sentence', unit: 'words',
                 warn: isOutlier(m.avg_sent, md.avg_sent),
                 hint: 'Average number of words per sentence.' + hintAvg('avg_sent', v => v.toFixed(1))},
                {value: m.crutch.toFixed(1), label: 'Crutch', unit: 'per 1,000 words',
                 pass: 'crutch_words',
                 warn: isOutlier(m.crutch, md.crutch),
                 hint: 'Hedging words per thousand words.\n\n• Examples: just, really, quite, actually, and similar.\n• The Crutch Words pass marks them.' + hintAvg('crutch', v => v.toFixed(1))},
                {id: 'sceneMattrBox', value: m.mattr ? m.mattr.toFixed(3) : '-', label: 'Variety (MATTR)', unit: 'Local • ' + mTarget,
                 warn: m.mattr ? (m.mattr < mBand[0] || m.mattr > mBand[1]) : false,
                 hint: 'Moving-Average Type-Token Ratio.\n\n• Local lexical variety.\n• A measure of vocabulary richness over a moving window of 100 words.\n• Typical range: ' + mBand[0] + ' to ' + mBand[1] + '.'},
                {id: 'sceneMtldBox', value: m.mtld ? m.mtld.toFixed(1) : '-', label: 'Variety (MTLD)', unit: 'Scene • ' + lTarget,
                 warn: m.mtld ? (m.mtld < lBand[0] || m.mtld > lBand[1]) : false,
                 hint: 'Measure of Textual Lexical Diversity.\n\n• Whole-scene lexical variety.\n• A measure of vocabulary richness taking the entire scene length into account.\n• Typical range: ' + lBand[0] + ' to ' + lBand[1] + '.'},
            ].forEach(function(stat) {
                const box = document.createElement('div');
                box.className = 'scene-stat-box';
                if (stat.warn) box.classList.add('scene-stat-warn');
                if (stat.id) box.id = stat.id;
                if (stat.pass) box.dataset.pass = stat.pass;
                if (stat.hint) box.title = stat.hint;

                const val = document.createElement('span');
                val.className = 'val';
                val.textContent = stat.value;
                const lbl = document.createElement('span');
                lbl.className = 'lbl';
                lbl.textContent = stat.label;
                box.append(val, lbl);
                if (stat.unit) {
                    const unit = document.createElement('span');
                    unit.className = 'unit';
                    unit.textContent = stat.unit;
                    box.appendChild(unit);
                }
                s.appendChild(box);
                
                if (stat.id === 'sceneMattrBox') mattrBox = box;
                if (stat.id === 'sceneMtldBox') mtldBox = box;
            });

            if (!m.mattr && mattrBox && mtldBox) {
                mattrBox.querySelector('.val').textContent = '...';
                mtldBox.querySelector('.val').textContent = '...';
                fetch('/api/scene/lexical?path=' + encodeURIComponent(p))
                    .then(function(res) { return res.json(); })
                    .then(function(lex) {
                        if (lex.ok && paths[curIdx] === p) {
                            m.mattr = lex.mattr;
                            m.mtld = lex.mtld;
                            mattrBox.querySelector('.val').textContent = m.mattr.toFixed(3);
                            mtldBox.querySelector('.val').textContent = m.mtld.toFixed(1);
                            if (m.mattr < mBand[0] || m.mattr > mBand[1]) mattrBox.classList.add('scene-stat-warn');
                            if (m.mtld < lBand[0] || m.mtld > lBand[1]) mtldBox.classList.add('scene-stat-warn');
                        }
                    })
                    .catch(function(err) { console.error('Failed to fetch scene lexical stats', err); });
            }

            // The pass toggles live in the panel's Analysis tab now. This row
            // is kept only for the Character Bible's "Back to Scene" button,
            // which has nowhere else to go.
            a.replaceChildren();
            render(preserveScroll);
            if (typeof renderSceneAnalysisPane === 'function'
                && !(document.getElementById('sceneAnalysisPane') || {hidden: true}).hidden) {
                renderSceneAnalysisPane();
            }
            syncSceneDisclosureState(true);
        }

        function updateFontSize(v) {
            const size = normalizeModalFontSize(v);
            const modalBody = document.getElementById('modalBody');
            const slider = document.getElementById('modalFontSize');
            if (modalBody) modalBody.style.fontSize = size + 'px';
            if (slider) slider.value = String(size);
            try {
                localStorage.setItem(MODAL_FONT_SIZE_STORAGE_KEY, String(size));
            } catch (err) {
                // Ignore storage errors and keep the current session size.
            }
        }

        // ── Compact scene toolbar ───────────────────────────────────────
        // This is presentation state only. It never changes a manuscript,
        // and the persisted preference deliberately has a small allow-list so
        // stale or edited localStorage values fail safely to auto-hide.
        const SCENE_TOOLBAR_MODE_STORAGE_KEY = 'proseview-scene-toolbar-mode';
        const SCENE_TOOLBAR_MODES = ['auto', 'pinned', 'hidden'];
        var _sceneToolbarMode = loadSceneToolbarMode();
        var _sceneToolbarLastScrollTop = 0;
        var _sceneToolbarHideTimer = null;

        function loadSceneToolbarMode() {
            try {
                var saved = localStorage.getItem(SCENE_TOOLBAR_MODE_STORAGE_KEY);
                return SCENE_TOOLBAR_MODES.indexOf(saved) >= 0 ? saved : 'auto';
            } catch (err) {
                return 'auto';
            }
        }

        function sceneToolbarHeader() {
            return document.querySelector('#sceneModal .modal-header');
        }

        function sceneToolbarReducedMotion() {
            return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
        }

        function sceneToolbarMenuIsOpen() {
            return !!document.querySelector('.scene-toolbar-popover:not([hidden])');
        }

        function setSceneToolbarHidden(hidden) {
            var header = sceneToolbarHeader();
            if (!header) return;
            header.dataset.toolbarHidden = hidden ? 'true' : 'false';
        }

        function clearSceneToolbarHideTimer() {
            window.clearTimeout(_sceneToolbarHideTimer);
            _sceneToolbarHideTimer = null;
        }

        function sceneToolbarModeRequiresHidden() {
            return _sceneToolbarMode === 'hidden' ||
                !!document.querySelector('#sceneModal .modal-content.modal-focus');
        }

        function revealSceneToolbar(temporary) {
            clearSceneToolbarHideTimer();
            setSceneToolbarHidden(false);
            if (temporary && sceneToolbarModeRequiresHidden()) {
                _sceneToolbarHideTimer = window.setTimeout(function() {
                    _sceneToolbarHideTimer = null;
                    var header = sceneToolbarHeader();
                    if (!header || !sceneToolbarModeRequiresHidden() ||
                            header.contains(document.activeElement) || sceneToolbarMenuIsOpen()) return;
                    setSceneToolbarHidden(true);
                }, 1800);
            }
        }

        function syncSceneToolbarModeControls() {
            var header = sceneToolbarHeader();
            if (header) header.dataset.toolbarMode = _sceneToolbarMode;
            document.querySelectorAll('input[name="sceneToolbarMode"]').forEach(function(input) {
                input.checked = input.value === _sceneToolbarMode;
            });
        }

        function setSceneToolbarMode(mode, persist) {
            if (SCENE_TOOLBAR_MODES.indexOf(mode) < 0) mode = 'auto';
            clearSceneToolbarHideTimer();
            _sceneToolbarMode = mode;
            syncSceneToolbarModeControls();
            if (persist !== false) {
                try { localStorage.setItem(SCENE_TOOLBAR_MODE_STORAGE_KEY, mode); } catch (err) {}
            }
            var focusLayout = !!document.querySelector('#sceneModal .modal-content.modal-focus');
            if (focusLayout || mode === 'hidden') {
                closeSceneToolbarMenus();
                setSceneToolbarHidden(true);
            } else {
                setSceneToolbarHidden(false);
            }
        }

        function closeSceneToolbarMenus(options) {
            var restoreFocus = !!(options && options.restoreFocus);
            var focusedOpener = null;
            document.querySelectorAll('.scene-toolbar-popover').forEach(function(menu) {
                if (menu.hidden) return;
                var opener = document.querySelector('[aria-controls="' + menu.id + '"]');
                menu.hidden = true;
                if (opener) {
                    opener.setAttribute('aria-expanded', 'false');
                    focusedOpener = focusedOpener || opener;
                }
            });
            if (restoreFocus && focusedOpener) focusedOpener.focus();
        }

        function toggleSceneToolbarMenu(menuId, opener) {
            var menu = document.getElementById(menuId);
            if (!menu || !opener) return;
            var opening = menu.hidden;
            closeSceneToolbarMenus();
            if (!opening) return;
            revealSceneToolbar(false);
            menu.hidden = false;
            opener.setAttribute('aria-expanded', 'true');
        }

        function resetSceneToolbarForRoute() {
            var scroller = document.querySelector('#sceneModal .modal-content');
            clearSceneToolbarHideTimer();
            _sceneToolbarLastScrollTop = scroller ? scroller.scrollTop : 0;
            closeSceneToolbarMenus();
            syncSceneToolbarModeControls();
            var focusLayout = !!(scroller && scroller.classList.contains('modal-focus'));
            setSceneToolbarHidden(focusLayout || _sceneToolbarMode === 'hidden');
        }

        function handleSceneToolbarScroll(event) {
            if (event.target !== document.querySelector('#sceneModal .modal-content')) return;
            var current = event.target.scrollTop;
            var delta = current - _sceneToolbarLastScrollTop;
            _sceneToolbarLastScrollTop = current;
            if (_sceneToolbarMode !== 'auto' || sceneToolbarReducedMotion()) return;
            if (document.querySelector('#sceneModal .modal-content.modal-focus')) return;
            if (current < 24 || delta < -8) {
                revealSceneToolbar(false);
            } else if (current > 80 && delta > 8 && !sceneToolbarMenuIsOpen()) {
                setSceneToolbarHidden(true);
            }
        }

        function initSceneToolbar() {
            var header = sceneToolbarHeader();
            var scroller = document.querySelector('#sceneModal .modal-content');
            if (!header || !scroller) return;
            syncSceneToolbarModeControls();
            setSceneToolbarHidden(_sceneToolbarMode === 'hidden');
            scroller.addEventListener('scroll', handleSceneToolbarScroll, { passive: true });
            header.addEventListener('focusin', function() { revealSceneToolbar(false); });
            header.addEventListener('focusout', function() {
                window.setTimeout(function() {
                    if (header.contains(document.activeElement) || sceneToolbarMenuIsOpen()) return;
                    if (_sceneToolbarMode === 'hidden' || scroller.classList.contains('modal-focus')) {
                        setSceneToolbarHidden(true);
                    }
                }, 0);
            });
            header.addEventListener('mouseenter', function() { revealSceneToolbar(false); });
            header.addEventListener('mouseleave', function() {
                if (_sceneToolbarMode !== 'hidden' && !scroller.classList.contains('modal-focus')) return;
                clearSceneToolbarHideTimer();
                _sceneToolbarHideTimer = window.setTimeout(function() {
                    _sceneToolbarHideTimer = null;
                    if (sceneToolbarModeRequiresHidden() && !header.contains(document.activeElement) &&
                            !sceneToolbarMenuIsOpen()) setSceneToolbarHidden(true);
                }, 500);
            });
            document.addEventListener('pointerdown', function(event) {
                if (!event.target.closest('.scene-toolbar-menu-wrap')) closeSceneToolbarMenus();
            });
        }

        function openBio(name) {
            const slug = name.toLowerCase().replace(/\s+/g, '-');
            const bio = bios[slug] || "# Bio not found for " + name;
            const b = document.getElementById('modalBody');
            const s = document.getElementById('modalStats');
            const a = document.getElementById('modalAlerts');
            const t = document.getElementById('modalTitle');

            t.innerText = "Character Bible: " + name;
            document.getElementById('modalEditorBtn').style.display = 'none';
            s.innerHTML = "";
            a.innerHTML = '<button type="button" class="alert-tag alert-tag-active" onclick="updateModal()" aria-label="Back to scene">\u2190 Back to Scene</button>';
            b.replaceChildren();
            const bioCard = document.createElement('div');
            bioCard.className = 'bio-card';
            renderSafeMarkdown(bioCard, bio, {basePath: '', allowRawImages: false});
            b.appendChild(bioCard);
            document.querySelector('.modal-content').scrollTop = 0;
        }

        function _syncStatTiles() {
            // A stat tile and its pass row are two readouts of one fact, so
            // both have to reflect the same state.
            // Tiles are readouts, not controls: they only echo which pass is on.
            document.querySelectorAll('#modalStats [data-pass]').forEach(function(box) {
                box.classList.toggle('scene-stat-on', !!hls[box.dataset.pass]);
            });
            if (typeof syncScenePassRows === 'function') syncScenePassRows();
        }

        function _applyHighlightChange() {
            _syncStatTiles();
            _saveHighlightPrefs();
            syncAllBtn();
            if (window._PM && _pmView) { updatePMHighlightDecorations(); } else { render(true); }
        }

        function toggleHighlight(id) {
            hls[id] = !hls[id];
            _applyHighlightChange();
        }

        function syncAllBtn() {
            const btn = document.getElementById('scenePassAllBtn');
            if (!btn) return;
            const anyOn = PASS_ORDER.some(k => hls[k]);
            btn.textContent = anyOn ? 'Clear' : 'All';
            btn.setAttribute('aria-pressed', anyOn ? 'true' : 'false');
        }

        function toggleAllHighlights() {
            const anyOn = PASS_ORDER.some(k => hls[k]);
            PASS_ORDER.forEach(k => { hls[k] = !anyOn; });
            _applyHighlightChange();
        }

        function attrEscape(s) {
            return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        }

        function renderCharacterTags(chars, charMentions) {
            charMentions = charMentions || {};
            return chars.map(function(c) {
                const metrics = charMentions[c];
                let title = 'Read bio';
                if (metrics) {
                    title = 'Mentions: ' + metrics.dialogue + ' dialogue, ' + metrics.prose + ' prose\nClick to read bio';
                }
                return '<button type="button" class="sc-char-tag" data-char-name="' +
                    attrEscape(c) +
                    '" onclick="openBio(this.dataset.charName)" title="' + attrEscape(title) + '">' +
                    escHtml(c) +
                    '</button>';
            }).join('');
        }

        function render(preserveScroll) {
            const p = paths[curIdx], b = document.getElementById('modalBody'), m = meta[p];
            const scrollEl = document.querySelector('#sceneModal .modal-content');
            let oldScroll = 0;
            if (scrollEl) {
                oldScroll = scrollEl.scrollTop;
                if (b) b.style.minHeight = scrollEl.scrollHeight + 'px';
            }
            const fm = m.fm || {};
            const chars = Array.isArray(fm.characters) ? fm.characters : (typeof fm.characters === 'string' ? [fm.characters] : []);
            const charMentions = m.char_mentions || {};
            const relatedDocs = Array.isArray(m.related_docs) ? m.related_docs : [];
            const sceneTodos = Array.isArray(m.todos) ? m.todos : [];
            const todosByPara = {};
            const fmTodos = [];
            sceneTodos.forEach(function(t) {
                if (t.paragraph_index >= 0) {
                    if (!todosByPara[t.paragraph_index]) todosByPara[t.paragraph_index] = [];
                    todosByPara[t.paragraph_index].push(t);
                } else {
                    fmTodos.push(t);
                }
            });
            const sceneNotes = Array.isArray(m.notes) ? m.notes : [];
            const notesByPara = {};
            sceneNotes.forEach(function(n) {
                if (!notesByPara[n.paragraph_index]) notesByPara[n.paragraph_index] = [];
                notesByPara[n.paragraph_index].push(n);
            });

            const editorHref = buildEditorUrl(m.abs_path);
            let relatedHtml = '<div class="scene-card-related">' +
                              '<div class="sc-row">' +
                              '<span class="sc-label">Related Docs</span>';
            if (relatedDocs.length) {
                relatedHtml += '<ul class="related-doc-list">';
                relatedDocs.forEach(doc => {
                    const docHref = buildEditorUrl(doc.abs_path || '');
                    const preview = doc.preview_text ? doc.preview_text.replace(/"/g, '&quot;') : '';
                    relatedHtml += '<li class="related-doc-item">' +
                                   '<button type="button" class="related-doc-link" data-path="' + attrEscape(doc.path || '') + '" onclick="openRelatedDoc(this.dataset.path)" title="' + attrEscape(preview) + '">' + escHtml(doc.path || '') + '</button>' +
                                   '<a class="related-doc-editor-icon" href="' + attrEscape(docHref) + '" target="_blank" title="Open in ' + attrEscape(editorLabel) + '">\u2197</a>' +
                                   '</li>';
                });
                relatedHtml += '</ul>';
            } else {
                relatedHtml += '<div class="related-doc-empty">No related planning or continuity docs matched this scene.</div>';
            }
            relatedHtml += '</div></div>';
            // Story-layer fields, labelled with the keys this repo actually
            // uses, so the row is traceable back to the frontmatter. Optional:
            // a manuscript that does not use them shows no row rather than a
            // line of "Unknown".
            const storyFields = (typeof storyModel === 'object' && storyModel) || {};
            const threadKey = storyFields.thread_field || 'thread';
            const dayKey = storyFields.day_field || 'day';
            const cap = function(t) { return t.charAt(0).toUpperCase() + t.slice(1); };
            const storyRow = function(label, value) {
                return value === undefined || value === null || value === ''
                    ? ''
                    : '<div class="sc-row"><span class="sc-label">' + escHtml(cap(label))
                      + '</span><span class="sc-value">' + escHtml(String(value)) + '</span></div>';
            };

            // Every row below is optional. A manuscript written without
            // frontmatter -- an Obsidian vault, an imported draft -- used to
            // render seven rows of "Unknown" and "Not defined", which reads as
            // a broken panel rather than an unused feature. Empty rows are
            // dropped, and if nothing at all is set the block is replaced by
            // one line saying what to add.
            const contextRows =
                storyRow('POV', fm.pov) +
                storyRow(threadKey, fm[threadKey]) +
                storyRow('When', fm.when) +
                storyRow(dayKey, fm[dayKey]) +
                storyRow('Where', fm.where || fm.location);
            const hasCharacters = Array.isArray(chars) ? chars.length > 0 : !!chars;
            const characterRow = hasCharacters
                ? '<div class="sc-row"><span class="sc-label">Characters</span><div class="sc-characters">' +
                  renderCharacterTags(chars, charMentions) + '</div></div>'
                : '';

            const arcRows =
                storyRow('Goal', fm.goal) +
                storyRow('Conflict', fm.conflict) +
                storyRow('Outcome', fm.outcome);
            const arcHtml = arcRows
                ? '<div class="scene-card-arc">' + arcRows + '</div>'
                : '';

            const hasAnyFrontmatter = !!(contextRows || characterRow || arcRows);
            const emptyHint = hasAnyFrontmatter ? '' :
                '<div class="scene-card-fm-empty">' +
                '<p>No scene details yet. These come from <em>frontmatter</em>: a small block ' +
                'of notes about the scene, written in YAML between two <code>---</code> lines at ' +
                'the very top of the file. It sits above your prose and never appears in the ' +
                'finished text:</p>' +
                '<pre><code>---\ncharacters:\nwhere:\nwhen:\ngoal:\nconflict:\noutcome:\n---</code></pre>' +
                '<button type="button" class="scene-card-fm-add" ' +
                'data-abs-path="' + attrEscape(m.abs_path || '') + '">Add this block</button>' +
                '<p class="scene-card-fm-hint">The keys are written empty for you to fill in. ' +
                'Every field is optional, and the rest of Proseview works without them.</p>' +
                '</div>';

            let cardHtml = '<div class="scene-card">' +
                           '<div class="scene-card-meta">' +
                           '<div class="scene-card-context-header" title="These fields are extracted from the YAML frontmatter block at the top of your markdown file.">Data from YAML Frontmatter &#x24D8;</div>' +
                           '<div class="sc-row scene-card-top">' +
                           '<span class="sc-label">Scene File <a class="editor-icon-btn" href="' + attrEscape(editorHref) + '" target="_blank" title="Open in ' + escHtml(editorLabel) + '">\u2197</a></span>' +
                           '<span class="sc-value">' + escHtml(p) + '</span>' +
                           '</div>' +
                           contextRows +
                           characterRow +
                           '</div>' +
                           arcHtml +
                           emptyHint +
                           relatedHtml +
                           '</div>';

            // Build tasks panel (all TODOs + notes sorted by paragraph order)
            const allTasks = [];
            sceneTodos.forEach(function(t) { allTasks.push({type: 'todo', para: t.paragraph_index, item: t}); });
            sceneNotes.forEach(function(n) { allTasks.push({type: 'note', para: n.paragraph_index, item: n}); });
            allTasks.sort(function(a, b) { return a.para - b.para; });
            let tasksHtml = '';
            if (allTasks.length) {
                const rows = allTasks.map(function(task) {
                    const jumpBtn = task.para >= 0
                        ? '<button class="task-jump-btn" type="button" data-para-idx="' + task.para + '" title="Scroll to paragraph">&#x2193;</button>'
                        : '';
                    const entry = task.type === 'todo'
                        ? todoEntryHtml(task.item, m.abs_path)
                        : noteEntryHtml(task.item, m.abs_path);
                    return '<div class="task-row">' + jumpBtn + entry + '</div>';
                }).join('');
                tasksHtml = '<div class="scene-tasks-section">' +
                    '<div class="scene-tasks-header"><span class="scene-tasks-label">Tasks</span>' +
                    '<span class="scene-tasks-count">' + allTasks.length + '</span></div>' +
                    rows + '</div>';
            }

            if (fmTodos.length) {
                const lineItems = fmTodos.map(function(t) {
                    return '<li class="scene-todo-item">' +
                           '<label class="fm-todo-label" style="display:flex; align-items:flex-start; gap:8px; cursor:pointer;">' +
                           '<input type="checkbox" class="fm-todo-cb" style="margin-top:4px;" onchange="completeFmTodo(this, \'' + attrEscape(m.abs_path) + '\', \'' + attrEscape(t.text) + '\', \'' + m.mtime + '\')">' +
                           '<span>' + escHtml(t.text) + '</span>' +
                           '</label></li>';
                }).join('');
                cardHtml += '<div class="scene-todos-section"><div class="scene-todos-label">Scene TODOs (frontmatter)</div><ul class="scene-todos-list" style="list-style:none; padding:0; margin:0;">' + lineItems + '</ul></div>';
            }

            // ProseMirror is the only renderer. If the module is still
            // loading, the inline ESM bootstrap at the bottom of the
            // template re-invokes render() once window._PM is ready.
            // Context and tasks live in the scene panel, not above the prose:
            // a disclosure here pushed the reading column down and reflowed it
            // on every toggle. The prose starts at the top and stays there.
            b.innerHTML = '<div id="sceneProseHost"></div>';
            if (!window._sceneContextBody) {
                window._sceneContextBody = document.createElement('div');
                window._sceneContextBody.id = 'sceneContextBody';
                window._sceneContextBody.className = 'scene-secondary-body';
            }
            window._sceneContextBody.innerHTML = cardHtml + tasksHtml;
            if (typeof renderSceneDetailsPane === 'function') renderSceneDetailsPane();
            if (window._PM) {
                mountProseView(p);
                if (window._lastExternalChangeIndices && window._lastExternalChangeIndices.length > 0) {
                    const pmParas = document.querySelectorAll('.ProseMirror > p');
                    window._lastExternalChangeIndices.forEach(function(idx) {
                        if (pmParas[idx]) {
                            pmParas[idx].classList.add('external-change-highlight');
                        }
                    });
                    window._lastExternalChangeIndices = null;
                }
            }
            if (scrollEl) {
                if (preserveScroll) {
                    scrollEl.scrollTop = oldScroll;
                    setTimeout(function() {
                        scrollEl.scrollTop = oldScroll;
                        if (b) b.style.minHeight = '';
                    }, 50);
                } else {
                    scrollEl.scrollTop = 0;
                    if (b) b.style.minHeight = '';
                }
            }
        }

        function guardDirtySceneNavigation() {
            return document.documentElement.dataset.view === 'scene' && _pmEditMode && _pmDirty;
        }

        function syncSceneDisclosureState() {
            // Kept as a no-op seam: the disclosures it used to collapse are gone,
            // and the panel handles narrow widths itself.
        }

        window.addEventListener('resize', function() { syncSceneDisclosureState(false); });
        if (window.visualViewport) {
            window.visualViewport.addEventListener('resize', function() { syncSceneDisclosureState(false); });
        }
        new MutationObserver(function() { syncSceneDisclosureState(false); }).observe(
            document.documentElement,
            {attributes: true, attributeFilter: ['data-css-zoom']}
        );
        if (window.ResizeObserver) {
            const sceneContent = document.querySelector('#sceneModal .modal-content');
            if (sceneContent) new ResizeObserver(function() { syncSceneDisclosureState(false); }).observe(sceneContent);
        }

        function navigateScene(d) {
            if (guardDirtySceneNavigation()) {
                showUnsavedDialog({onContinue: function() { navigateScene(d); }});
                return false;
            }
            _pmEditMode = false;
            saveActiveScrollPosition();
            curIdx = Math.max(0, Math.min(paths.length - 1, curIdx + d));
            updateModal();
            const p = paths[curIdx];
            if (p) {
                routeToHash('/scene/' + encodeURIComponent(p), true);
                restoreActiveScrollPosition();
                if (typeof discussFollowActiveDocument === 'function') discussFollowActiveDocument();
            }
            return true;
        }
        function closeSceneModal(options) {
            options = options || {};
            if (guardDirtySceneNavigation()) {
                showUnsavedDialog({onContinue: closeSceneModal});
                return false;
            }
            saveActiveScrollPosition();
            hideSelectionPill();
            clearSceneSelectionMemory();
            hideInsertAffordance();
            closeAnnotationPopover();
            if (_pmView) { _pmView.destroy(); _pmView = null; }
            _pmEditMode = false;
            var editBar = document.getElementById('sceneEditBar');
            if (editBar) editBar.hidden = true;
            if (window._resetEditBarPosition) window._resetEditBarPosition();
            exitFocusMode();
            delete document.documentElement.dataset.view;
            if (options.route !== false) routeToHash('/tab/' + currentTab, true);
            if (typeof discussFollowActiveDocument === 'function') discussFollowActiveDocument();
            restoreActiveScrollPosition();
            return true;
        }

        // Focus mode used to hide the stat grid and the pass row where they sat
        // above the prose. Both live in the dock now, so what it hides is the
        // dock -- and it puts it back on the way out, because a reading mode
        // that quietly discards your panel is a mode you stop using.
        var _focusClosedTheDock = false;

        function toggleFocusMode() {
            var mc = document.querySelector('#sceneModal .modal-content');
            if (!mc) return;
            var entering = !mc.classList.contains('modal-focus');
            clearSceneToolbarHideTimer();
            mc.classList.toggle('modal-focus', entering);
            var btn = document.getElementById('modalFocusBtn');
            if (btn) {
                btn.classList.toggle('is-active', entering);
                btn.setAttribute('aria-pressed', entering ? 'true' : 'false');
            }
            if (entering) _closeDockForFocus();
            else _restoreDockAfterFocus();
            closeSceneToolbarMenus();
            setSceneToolbarHidden(entering || _sceneToolbarMode === 'hidden');
        }

        function _closeDockForFocus() {
            var panel = document.getElementById('discussPanel');
            var open = panel && !panel.hidden;
            _focusClosedTheDock = !!open;
            if (open && typeof closeScenePanel === 'function') closeScenePanel();
        }

        function _restoreDockAfterFocus() {
            if (!_focusClosedTheDock) return;
            _focusClosedTheDock = false;
            if (typeof toggleScenePanel === 'function') toggleScenePanel();
        }

        function exitFocusMode() {
            clearSceneToolbarHideTimer();
            var mc = document.querySelector('#sceneModal .modal-content');
            var wasFocused = mc && mc.classList.contains('modal-focus');
            if (mc) mc.classList.remove('modal-focus');
            var btn = document.getElementById('modalFocusBtn');
            if (btn) {
                btn.classList.remove('is-active');
                btn.setAttribute('aria-pressed', 'false');
            }
            // Closing the scene leaves focus mode too; the dock belongs to the
            // scene, so there is nothing to restore in that direction.
            if (wasFocused) _focusClosedTheDock = false;
            setSceneToolbarHidden(_sceneToolbarMode === 'hidden');
        }

        function toggleFullscreen() {
            var btn = document.getElementById('modalFullscreenBtn');
            if (!document.fullscreenElement) {
                document.documentElement.requestFullscreen().then(function() {
                    if (navigator.keyboard && navigator.keyboard.lock) {
                        navigator.keyboard.lock(['Escape']).catch(function() {});
                    }
                    if (btn) {
                        btn.classList.add('is-active');
                        btn.setAttribute('aria-pressed', 'true');
                    }
                }).catch(function(err) {
                    console.warn('Error attempting to enable fullscreen: ' + err.message);
                });
            } else {
                if (document.exitFullscreen) {
                    document.exitFullscreen().then(function() {
                        if (navigator.keyboard && navigator.keyboard.unlock) {
                            navigator.keyboard.unlock();
                        }
                        if (btn) {
                            btn.classList.remove('is-active');
                            btn.setAttribute('aria-pressed', 'false');
                        }
                    });
                }
            }
        }

        document.addEventListener('fullscreenchange', function() {
            var btn = document.getElementById('modalFullscreenBtn');
            if (!btn) return;
            var isFs = !!document.fullscreenElement;
            btn.classList.toggle('is-active', isFs);
            btn.setAttribute('aria-pressed', isFs ? 'true' : 'false');
        });

        document.addEventListener('keydown', function(e) {
            if (e.ctrlKey || e.altKey || e.metaKey) return;
            var tag = (e.target.tagName || '').toUpperCase();
            if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
            if (document.documentElement.dataset.view !== 'scene') return;
            if ((e.metaKey || e.ctrlKey) && e.shiftKey && (e.key === 'f' || e.key === 'F')) {
                e.preventDefault();
                toggleFullscreen();
                return;
            }
            if ((e.key === 'f' || e.key === 'F') && !_pmEditMode) {
                e.preventDefault();
                toggleFocusMode();
            } else if ((e.key === 'e' || e.key === 'E') && !_pmEditMode) {
                e.preventDefault();
                toggleSceneEdit();
            } else if ((e.key === 'b' || e.key === 'B') && !_pmEditMode) {
                e.preventDefault();
                setSidebarOpen(document.documentElement.dataset.sidebar === 'closed');
            }
        });

        document.addEventListener('keydown', function(e) {
            if (e.key !== 'Escape' || !sceneToolbarMenuIsOpen()) return;
            e.preventDefault();
            e.stopImmediatePropagation();
            closeSceneToolbarMenus({ restoreFocus: true });
        });

        initSceneToolbar();

window.completeFmTodo = function(checkbox, absPath, text, openMtime) {
    if (!checkbox.checked) return;
    checkbox.disabled = true;
    const originalText = checkbox.nextElementSibling.textContent;
    checkbox.nextElementSibling.textContent = 'Removing...';
    
    fetch('/delete-fm-todo', {
        method: 'POST',
        headers: pvHeaders(),
        body: JSON.stringify({
            abs_path: absPath,
            todo_text: text,
            open_mtime: parseFloat(openMtime) || 0
        })
    }).then(function(r) {
        if (!r.ok) throw new Error('Failed to delete frontmatter TODO');
        return r.json();
    }).then(function(data) {
        if (data.ok) {
            checkbox.closest('.scene-todo-item').remove();
            // Invalidate analysis to fetch updated data
            if (typeof markAnalysisStale === 'function') markAnalysisStale();
        } else {
            throw new Error(data.error || 'Unknown error');
        }
    }).catch(function(err) {
        checkbox.disabled = false;
        checkbox.checked = false;
        checkbox.nextElementSibling.textContent = originalText;
        console.error(err);
        alert('Could not complete TODO: ' + err.message);
    });
};
