        function mountProseView(p) {
            if (_pmView) { _pmView.destroy(); _pmView = null; }
            var PM = window._PM;
            if (!PM) return;
            var host = document.getElementById('sceneProseHost');
            if (!host) return;

            var markdown = (contents[p] || '').trim();

            // Annotation is an atom node, and markdown-it emits a single
            // html_block token (not an open/close pair), so we use the
            // ``node:`` spec form rather than ``block:`` -- otherwise older
            // prosemirror-markdown registers html_block_open / _close
            // handlers that never match and the parser throws
            // "Token type `html_block` not supported".
            //
            // ``html_inline`` (raw HTML inside a paragraph) is dropped
            // silently because the annotation node is block-level; nothing
            // in this app produces inline HTML on purpose.
            var parser = new PM.MarkdownParser(
                PM.mySchema,
                PM.defaultMarkdownParser.tokenizer,
                Object.assign({}, PM.defaultMarkdownParser.tokens, {
                    html_block: {
                        node: 'annotation',
                        getAttrs: function(tok) { return { raw: tok.content.trim() }; }
                    },
                    html_inline: { ignore: true }
                })
            );

            var doc = parser.parse(markdown);

            var lnPlugin = _buildLnPlugin();

            function buildListInputRules(PM) {
                if (!PM.inputRules || !PM.wrappingInputRule) return null;
                return PM.inputRules({
                    rules: [
                        PM.wrappingInputRule(/^\s*([-+*])\s$/, PM.mdSchema.nodes.bullet_list, { tight: true }),
                        PM.wrappingInputRule(/^(\d+)\.\s$/, PM.mdSchema.nodes.ordered_list, match => ({order: +match[1], tight: true}), (match, node) => node.childCount + node.attrs.order == +match[1])
                    ]
                });
            }

            var plugins = [
                PM.buildHlPlugin(),
                lnPlugin,
                (typeof buildAiProposalPlugin === 'function' ? buildAiProposalPlugin(PM) : null),
                buildListInputRules(PM),
                PM.history(),
                PM.keymap(Object.assign({}, PM.baseKeymap, {
                    'Mod-z': PM.undo,
                    'Mod-y': PM.redo,
                    'Mod-Shift-z': PM.redo,
                    'Mod-b': PM.toggleMark(PM.mySchema.marks.strong),
                    'Mod-i': PM.toggleMark(PM.mySchema.marks.em),
                    'Mod-`': PM.toggleMark(PM.mySchema.marks.code),
                    'Mod-e': PM.toggleMark(PM.mySchema.marks.code),
                    'Enter': function(state, dispatch, view) {
                        return PM.chainCommands(PM.splitListItem(state.schema.nodes.list_item), PM.baseKeymap.Enter)(state, dispatch, view);
                    },
                    'Mod-Shift-8': function() { window.toggleList('bullet_list'); return true; },
                    'Mod-Shift-7': function() { window.toggleList('ordered_list'); return true; },
                    // Saving with the keyboard keeps you writing; the Save
                    // button is the one that finishes and leaves edit mode.
                    // saveSceneEdit guards on `exitEditMode !== false`, so
                    // this has to be passed explicitly.
                    'Mod-s': function() { saveSceneEdit(null, false); return true; }
                }))
            ].filter(Boolean);

            var state = PM.EditorState.create({ doc: doc, plugins: plugins });
            _pmView = new PM.EditorView(host, {
                state: state,
                editable: function() { return _pmEditMode; },
                // Keep the cursor away from the very top/bottom of the
                // modal scroll container so arrow-key navigation produces
                // small, frequent scrolls instead of one large jump when
                // the cursor finally hits the edge.
                scrollThreshold: 80,
                scrollMargin: 80,
                // Track unsaved changes so the edit pill / title can show a
                // modified indicator. Only flips on transactions that
                // actually change the document, not selection-only ones.
                dispatchTransaction: function(tr) {
                    var newState = _pmView.state.apply(tr);
                    _pmView.updateState(newState);
                    if (_pmEditMode && tr.docChanged && !_pmDirty) {
                        setPmDirty(true);
                    }
                },
                nodeViews: {
                    annotation: PM.createAnnotationNodeView
                }
            });

            initAffordance(_pmView);
            updatePMHighlightDecorations();

            // Tag each top-level block with its source line and apply the
            // toggle's current state (so a freshly-mounted scene reflects
            // the saved preference).
            try {
                var lnSet = _buildLineNumberDecorations(_pmView.state.doc, markdown, (meta[p] && meta[p].txt_line_offset) || 0, meta[p] && meta[p].abs_path);
                if (lnSet) {
                    var tr = _pmView.state.tr.setMeta(lnPluginKey, lnSet);
                    _pmView.dispatch(tr);
                }
            } catch (e) {}
            _applyLineNumbersClass();
            _applyEditingProseClass();
            if (typeof aiMaybeRefocusActiveProposal === 'function') {
                setTimeout(function() { aiMaybeRefocusActiveProposal(p); }, 0);
            }
        }

        function updatePMHighlightDecorations() {
            if (!_pmView || !window._PM) return;
            var p = paths[curIdx];
            var PM = window._PM;
            var sceneHls = highlightsByPath[p] || { paragraphs: [], highlights: {} };
            var hlData = sceneHls.highlights || {};
            var doc = _pmView.state.doc;

            var paraNodes = [];
            doc.descendants(function(node, pos) {
                if (node.isTextblock) paraNodes.push(pos);
            });

            var decorations = [];
            PASS_ORDER.forEach(function(name) {
                if (!hls[name]) return;
                var insts = hlData[name] || [];
                insts.forEach(function(inst) {
                    var paraIdx = inst.paragraph_index;
                    var offsets = inst.char_offsets;
                    if (!offsets || paraIdx >= paraNodes.length) return;
                    var nodePos = paraNodes[paraIdx];
                    var from = nodePos + 1 + offsets[0];
                    var to = nodePos + 1 + offsets[1];
                    if (from >= to || to > doc.content.size) return;
                    var cls = PASS_CLASSES[name] || '';
                    var title = PASS_LABELS[name] || name;
                    if (name === 'sensory' && inst.note) title += ' (' + inst.note + ')';
                    
                    var desc = PASS_INLINE_TIPS ? (PASS_INLINE_TIPS[name] || '') : '';
                    if (desc.indexOf('{word}') !== -1) desc = desc.split('{word}').join(inst.text);
                    
                    var attrs = { class: cls };
                    if (name === 'repeats') {
                        var parts = (inst.note || '').split('/');
                        var paraCount = parts[0] || '?';
                        var sceneCount = parts[1] || '?';
                        if (desc.indexOf('{para}') !== -1) desc = desc.split('{para}').join(paraCount);
                        if (desc.indexOf('{scene}') !== -1) desc = desc.split('{scene}').join(sceneCount);
                        attrs['data-count'] = paraCount + ' / ' + sceneCount;
                    }
                    
                    attrs['data-hl-title'] = title;
                    attrs['data-hl-desc'] = desc;
                    
                    decorations.push(PM.Decoration.inline(from, to, attrs));
                });
            });

            var decoSet = PM.DecorationSet.create(doc, decorations);
            var tr = _pmView.state.tr.setMeta(PM.hlPluginKey, decoSet);
            _pmView.dispatch(tr);
        }

        function toggleSceneEdit() {
            if (!window._PM) return;
            if (_pmEditMode) {
                cancelSceneEdit();
                return;
            }
            if (!_pmView) {
                render();
                if (!_pmView) return;
            }
            _pmEditMode = true;
            _pmView.setProps({ editable: function() { return true; } });
            var editBar = document.getElementById('sceneEditBar');
            if (editBar) editBar.hidden = false;
            var btn = document.getElementById('sceneEditBtn');
            if (btn) btn.textContent = '✗ Cancel';
            var p = paths[curIdx];
            _pmOpenMtime = meta[p] && meta[p].mtime;
            setPmDirty(false);
            _applyEditingProseClass();
            _pmView.focus();
        }

        function serializeSceneEditorMarkdown() {
            if (!_pmView || !window._PM) return '';
            var PM = window._PM;
            var nodes = Object.assign({}, PM.defaultMarkdownSerializer.nodes, {
                annotation: function(state, node) {
                    state.write(node.attrs.raw);
                    state.closeBlock(node);
                }
            });
            return new PM.MarkdownSerializer(nodes, PM.defaultMarkdownSerializer.marks).serialize(_pmView.state.doc);
        }

        function currentSceneLiveDocumentSnapshot() {
            if (!_pmView || !_pmDirty || !_pmEditMode || _pmOpenMtime === null || _pmOpenMtime === undefined) return null;
            return {content: serializeSceneEditorMarkdown(), base_mtime: _pmOpenMtime};
        }

        function saveSceneEdit(onSaved, exitEditMode, overwrite) {
            if (!_pmView || !_pmEditMode) return;
            if (!_pmDirty) return;
            if (_pmSaveInFlight) return;
            var p = paths[curIdx];
            var markdown = serializeSceneEditorMarkdown();
            _pmSaveInFlight = true;

            // Stay in edit mode while the request is in flight; reflect
            // progress in the pill instead of yanking the bar away.
            setPmSaving();

            // The save will trigger an SSE "reload" event via the server's
            // file-watcher invalidation. Mark it expected so reloadOrDefer
            // can swallow it (else we'd get a jolting full page reload).
            _pendingSelfReloads++;
            if (_pendingSelfReloadTimer) clearTimeout(_pendingSelfReloadTimer);
            _pendingSelfReloadTimer = setTimeout(function() {
                _pendingSelfReloads = 0;
                _pendingSelfReloadTimer = null;
            }, 4000);

            var absPath = meta[p] && meta[p].abs_path;
            fetch('/save-scene', {
                method: 'POST',
                headers: pvHeaders(),
                body: JSON.stringify({
                    abs_path: absPath,
                    content: markdown,
                    open_mtime: _pmOpenMtime,
                    // Set only by the conflict dialog's explicit "mine wins".
                    // The server still backs up the version it replaces.
                    overwrite: !!overwrite
                })
            }).then(function(r) {
                if (r.status === 409) {
                    _pmSaveInFlight = false;
                    setPmDirty(true);
                    _pmConflictDraft = markdown;
                    var conflictButton = document.getElementById('sceneConflictReopen');
                    if (conflictButton) conflictButton.hidden = false;
                    openSceneConflictDialog();
                    return null;
                }
                return r.json();
            }).then(function(data) {
                if (!data) return;
                _pmSaveInFlight = false;
                if (!data.ok) { setPmDirty(true); return; }
                if (data.mtime) _pmOpenMtime = data.mtime;
                // meta is the baseline every later write reads: the next edit
                // session and the annotation endpoints. refreshContent() would
                // normally carry the new mtime in, but it is a no-op while the
                // editor is open and never retries, so our own save has to
                // advance it. Leaving it stale makes the *next* save look like
                // someone else changed the file underneath us.
                if (meta[p] && data.mtime) meta[p].mtime = data.mtime;
                if (meta[p] && data.revision) meta[p].revision = data.revision;
                contents[p] = markdown;
                var liveMarkdown = serializeSceneEditorMarkdown();
                if (liveMarkdown !== markdown) {
                    setPmDirty(true);
                    return;
                }
                setPmSaved();
                clearSceneConflictState();
                if (typeof aiMarkAppliedProposalsSaved === 'function') aiMarkAppliedProposalsSaved();
                var historyPane = document.getElementById('sceneHistoryPane');
                if (historyPane && !historyPane.hidden && paths[curIdx] && typeof loadSceneHistory === 'function') {
                    loadSceneHistory(paths[curIdx]);
                }
                if (exitEditMode !== false) cancelSceneEdit();
                if (typeof onSaved === 'function') onSaved();
            }).catch(function(err) {
                _pmSaveInFlight = false;
                setPmDirty(true);
                alert('Save failed: ' + (err && err.message || 'unknown error'));
            });
        }

        function openSceneConflictDialog() {
            var dialog = document.getElementById('sceneConflictDialog');
            if (!dialog || !_pmConflictDraft) return;
            var status = document.getElementById('sceneConflictStatus');
            if (status) status.textContent = '';
            if (!dialog.open) dialog.showModal();
            var keep = dialog.querySelector('button');
            if (keep) keep.focus();
        }

        function keepEditingAfterConflict() {
            var dialog = document.getElementById('sceneConflictDialog');
            if (dialog && dialog.open) dialog.close('keep-editing');
            if (_pmView) _pmView.focus();
        }

        function currentConflictDraft() {
            // The writer may continue editing after the first 409. Use what is
            // in the editor now, not the snapshot captured at conflict time, so
            // no recovery action silently omits later work.
            return (_pmEditMode && _pmView) ? serializeSceneEditorMarkdown() : (_pmConflictDraft || '');
        }

        function clearSceneConflictState() {
            _pmConflictDraft = null;
            var conflictButton = document.getElementById('sceneConflictReopen');
            if (conflictButton) conflictButton.hidden = true;
            var dialog = document.getElementById('sceneConflictDialog');
            if (dialog && dialog.open) dialog.close('resolved');
        }

        function showConflictDiff() {
            var p = paths[curIdx];
            var absPath = meta[p] && meta[p].abs_path;
            if (!absPath) return;
            // A modal <dialog> renders in the top layer, so it would sit over
            // the diff overlay. Close it; closeDiffModal() brings it back.
            var dialog = document.getElementById('sceneConflictDialog');
            if (dialog && dialog.open) dialog.close('show-diff');
            openConflictDiffModal(absPath, currentConflictDraft());
        }

        function overwriteDiskWithConflictDraft() {
            var dialog = document.getElementById('sceneConflictDialog');
            if (dialog && dialog.open) dialog.close('overwrite');
            var overlay = document.getElementById('diffModalOverlay');
            if (overlay && !overlay.hidden) {
                // Drop the conflict chrome first so closeDiffModal() does not
                // read this as an unresolved conflict and reopen the dialog.
                document.getElementById('diffModalOverwriteBtn').hidden = true;
                closeDiffModal();
            }
            saveSceneEdit(null, false, true);
        }

        function copyConflictDraft() {
            var status = document.getElementById('sceneConflictStatus');
            var draft = currentConflictDraft();
            var copied = function() { if (status) status.textContent = 'Draft copied to the clipboard.'; };
            var failed = function() { if (status) status.textContent = 'Clipboard access was unavailable. Keep editing to preserve the draft.'; };
            if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(draft).then(copied, failed);
            else failed();
        }

        function reloadConflictDiskVersion() {
            var p = paths[curIdx];
            var dialog = document.getElementById('sceneConflictDialog');
            if (dialog && dialog.open) dialog.close('reload-disk');
            clearSceneConflictState();
            cancelSceneEdit();
            refreshContent([p]);
        }

        function cancelSceneEdit() {
            // The response still owns the acknowledged snapshot while a save
            // is in flight. Exiting now would let it mutate hidden editor
            // state and make the durable file disagree with the page.
            if (_pmSaveInFlight) return false;
            if (typeof aiDiscardAppliedProposals === 'function') aiDiscardAppliedProposals();
            _pmEditMode = false;
            _pmSaveInFlight = false;
            clearSceneConflictState();
            setPmDirty(false);
            _applyEditingProseClass();
            hideInsertAffordance();
            closeAnnotationPopover();
            if (_pmView) _pmView.setProps({ editable: function() { return false; } });
            var editBar = document.getElementById('sceneEditBar');
            if (editBar) {
                editBar.hidden = true;
                editBar.classList.remove('is-saving', 'is-saved', 'is-dirty');
            }
            var btn = document.getElementById('sceneEditBtn');
            if (btn) btn.textContent = '✏ Edit';
            var p = paths[curIdx];
            var scrollEl = document.querySelector('#sceneModal .modal-content');
            var bodyEl = document.getElementById('modalBody');
            var oldScroll = 0;
            if (scrollEl) {
                oldScroll = scrollEl.scrollTop;
                if (bodyEl) bodyEl.style.minHeight = scrollEl.scrollHeight + 'px';
            }
            mountProseView(p);
            if (scrollEl) {
                scrollEl.scrollTop = oldScroll;
                setTimeout(function() {
                    scrollEl.scrollTop = oldScroll;
                    if (bodyEl) bodyEl.style.minHeight = '';
                }, 50);
            }
            return true;
        }

        window.addEventListener('beforeunload', function(event) {
            if (!_pmEditMode || !_pmDirty) return;
            event.preventDefault();
            event.returnValue = '';
        });

        var _hlTooltip = null;
        var _hlTooltipTimeout = null;

        function getHlTooltip() {
            if (!_hlTooltip) {
                _hlTooltip = document.createElement('div');
                _hlTooltip.className = 'hl-custom-tooltip';
                _hlTooltip.style.opacity = '0';
                document.body.appendChild(_hlTooltip);
            }
            return _hlTooltip;
        }

        document.addEventListener('mouseover', function(e) {
            var target = e.target;
            // Ignore prose view widgets and other things that aren't highlight spans
            if (target && target.classList && target.classList.contains('ProseMirror-widget')) return;
            
            var isHl = false;
            if (target && target.classList) {
                for (var i = 0; i < target.classList.length; i++) {
                    if (target.classList[i].startsWith('hl-')) {
                        isHl = true;
                        break;
                    }
                }
            }

            if (!isHl) {
                if (_hlTooltip && _hlTooltip.style.opacity !== '0') {
                    clearTimeout(_hlTooltipTimeout);
                    _hlTooltipTimeout = setTimeout(function() { _hlTooltip.style.opacity = '0'; }, 100);
                }
                return;
            }

            clearTimeout(_hlTooltipTimeout);
            var title = target.getAttribute('data-hl-title');
            var desc = target.getAttribute('data-hl-desc');
            if (!title) return;

            var tt = getHlTooltip();
            tt.innerHTML = '<div class="hl-title">' + title + '</div>' + 
                           (desc ? '<div class="hl-desc">' + desc + '</div>' : '') +
                           '<div class="hl-disable">To disable this highlight, go to the Analysis tab.</div>';
            
            tt.style.display = 'block';
            var rect = target.getBoundingClientRect();
            
            requestAnimationFrame(function() {
                var ttRect = tt.getBoundingClientRect();
                var top = rect.top - ttRect.height - 8;
                if (top < 0) top = rect.bottom + 8;
                var left = rect.left + (rect.width / 2) - (ttRect.width / 2);
                if (left < 10) left = 10;
                if (left + ttRect.width > window.innerWidth - 10) left = window.innerWidth - ttRect.width - 10;
                
                tt.style.top = top + window.scrollY + 'px';
                tt.style.left = left + window.scrollX + 'px';
                tt.style.opacity = '1';
            });
        });

        window.toggleFormat = function(markName) {
            if (!_pmView || !window._PM) return;
            var PM = window._PM;
            var markType = _pmView.state.schema.marks[markName];
            if (markType) {
                PM.toggleMark(markType)(_pmView.state, _pmView.dispatch);
                _pmView.focus();
            }
        };

        window.toggleList = function(listType) {
            if (!_pmView || !window._PM) return;
            var PM = window._PM;
            var state = _pmView.state;
            var dispatch = _pmView.dispatch.bind(_pmView);
            var nodeType = state.schema.nodes[listType];
            var itemType = state.schema.nodes.list_item;
            if (!nodeType || !itemType) return;
            
            var isActive = false;
            var $from = state.selection.$from;
            for (var i = $from.depth; i > 0; i--) {
                if ($from.node(i).type === nodeType) {
                    isActive = true;
                    break;
                }
            }

            if (isActive) {
                if (PM.liftListItem && PM.liftListItem(itemType)(state)) {
                    PM.liftListItem(itemType)(state, dispatch);
                }
            } else {
                if (PM.wrapInList && PM.wrapInList(nodeType, { tight: true })(state)) {
                    PM.wrapInList(nodeType, { tight: true })(state, dispatch);
                } else {
                    if (PM.liftListItem && PM.liftListItem(itemType)(state)) {
                        PM.liftListItem(itemType)(state, dispatch);
                        if (PM.wrapInList && PM.wrapInList(nodeType, { tight: true })(_pmView.state)) {
                            PM.wrapInList(nodeType, { tight: true })(_pmView.state, dispatch);
                        }
                    }
                }
            }
            _pmView.focus();
        };

        window.toggleBlockquote = function() {
            if (!_pmView || !window._PM) return;
            var PM = window._PM;
            var state = _pmView.state;
            var nodeType = state.schema.nodes.blockquote;
            if (!nodeType) return;
            
            var dispatch = _pmView.dispatch.bind(_pmView);
            
            var isActive = false;
            var $from = state.selection.$from;
            for (var i = $from.depth; i > 0; i--) {
                if ($from.node(i).type === nodeType) {
                    isActive = true;
                    break;
                }
            }

            if (isActive) {
                if (PM.lift && PM.lift(state)) {
                    PM.lift(state, dispatch);
                }
            } else {
                if (PM.wrapIn && PM.wrapIn(nodeType)(state)) {
                    PM.wrapIn(nodeType)(state, dispatch);
                }
            }
            _pmView.focus();
        };

        (function() {
            var editBar = document.getElementById('sceneEditBar');
            var dragHandle = document.querySelector('.scene-edit-status');
            if (!editBar || !dragHandle) return;
            
            var originalParent = editBar.parentNode;
            var originalNextSibling = editBar.nextSibling;
            
            var isDragging = false;
            var startX = 0, startY = 0;
            
            dragHandle.style.cursor = 'grab';
            
            dragHandle.addEventListener('mousedown', function(e) {
                isDragging = true;
                dragHandle.style.cursor = 'grabbing';
                
                if (editBar.parentNode !== document.body) {
                    var rect = editBar.getBoundingClientRect();
                    document.body.appendChild(editBar);
                    editBar.style.position = 'fixed';
                    editBar.style.margin = '0';
                    editBar.style.bottom = 'auto';
                    editBar.style.right = 'auto';
                    editBar.style.left = rect.left + 'px';
                    editBar.style.top = rect.top + 'px';
                    editBar.style.transform = 'none';
                    editBar.style.zIndex = '3000';
                    startX = e.clientX - rect.left;
                    startY = e.clientY - rect.top;
                } else {
                    startX = e.clientX - parseFloat(editBar.style.left || 0);
                    startY = e.clientY - parseFloat(editBar.style.top || 0);
                }
                e.preventDefault();
            });
            
            document.addEventListener('mousemove', function(e) {
                if (!isDragging) return;
                var left = e.clientX - startX;
                var top = e.clientY - startY;
                editBar.style.left = left + 'px';
                editBar.style.top = top + 'px';
            });
            
            document.addEventListener('mouseup', function() {
                if (isDragging) {
                    isDragging = false;
                    dragHandle.style.cursor = 'grab';
                }
            });
            
            window._resetEditBarPosition = function() {
                if (editBar.parentNode !== originalParent) {
                    originalParent.insertBefore(editBar, originalNextSibling);
                    editBar.style.position = '';
                    editBar.style.margin = '';
                    editBar.style.bottom = '';
                    editBar.style.right = '';
                    editBar.style.left = '';
                    editBar.style.top = '';
                    editBar.style.transform = '';
                    editBar.style.zIndex = '';
                }
            };
        })();
