        // ── Project conversations with document-aware turns ──────────────
        var _discussConversationId = null;
        var _discussSnapshot = null;
        var _discussDocumentKey = '';
        var _discussDraftDocument = null;
        var _discussEventSource = null;
        var _discussAttachments = [];
        var _discussSelection = '';
        var _discussSelectionRange = null;
        var _discussSelectionSnapshot = null;
        var _discussSelectionSourceTaskId = null;
        var _discussLiveDocument = null;
        var _discussIncludeCurrentDocument = false;
        var _discussContextChoices = [];
        var _discussContextActiveIndex = 0;
        var _discussMentionRange = null;
        var _discussContextCandidateCache = null;
        var _discussReturnFocus = null;
        var _discussRefreshTimer = null;
        var _discussLastApproval = '';
        var _discussPendingAction = null;
        var _discussRepositoryAction = null;
        var _discussRetryOfTaskId = null;
        var _discussSelectedSkill = null;
        var _discussSkills = [];
        var _discussAutoRun = false;
        var _discussPreservedDraft = '';
        var _discussAutoReviewedTasks = Object.create(null);
        var _discussAutoReviewRequests = Object.create(null);
        var _prosviewRepositoryRootCache = null;
        var _discussRequestTimeoutMs = 15000;
        var _discussOpenFailed = false;
        var _discussLocalError = '';
        var _discussLocalErrorKind = '';
        // ── Agents ──────────────────────────────────────────────────────────
        // Codex and Claude are separate conversations that run at the same
        // time on the server. They are tabs, so only one is on screen; the
        // other keeps working and its state is restored from its snapshot when
        // you come back. Only the things a snapshot cannot carry -- the draft
        // you were typing, what you had attached -- are held here per agent.
        const DISCUSS_AGENTS = ['codex', 'claude'];
        // Reading passes are ordinary questions: no schema, no card, and the
        // thing they read stays attached so a follow-up can refer to it.
        const DISCUSS_READING_ACTIONS = [
            'quick_critique', 'style_consistency', 'voice_character',
            'pacing_tension', 'clarity_flow', 'continuity'
        ];
        function discussIsReadingAction(actionId) {
            return DISCUSS_READING_ACTIONS.indexOf(String(actionId || '')) >= 0;
        }
        // What a writer reaches for on an ordinary afternoon. The test for this
        // list is repetition: these get run on the same scene in draft two and
        // again in draft five.
        //
        // What each card *says* belongs to its skill file, which the writer
        // owns and can rewrite -- `description:` in the SKILL.md frontmatter,
        // served by /api/discuss/actions. The wording here is the last resort:
        // it stands in until that answer lands, and for a skill file whose
        // frontmatter has no description line at all.
        const DISCUSS_SCENE_PASSES = [
            {
                id: 'quick_critique',
                label: 'Quick critique',
                copy: 'What is working against this scene, each note quoting the line and suggesting a fix.'
            },
            {
                id: 'style_consistency',
                label: 'Style and consistency',
                copy: 'Proseview finds the passives, filter verbs and echoes. The agent says which ones hurt.'
            }
        ];
        // The rarer, heavier work. Same cards, same skill files behind them.
        const DISCUSS_REPOSITORY_PASSES = [
            {
                id: 'canon_refactor',
                label: 'Trace a canon change',
                copy: 'Find consequences across the configured story folders.'
            },
            {
                id: 'scene_continuity',
                label: "Check this scene's continuity",
                copy: 'Compare this document with the rest of the story evidence.'
            }
        ];
        // id -> {label, description} as the skill files currently read.
        var _discussActionCopy = {};
        // How long a finished turn keeps its result on screen before the
        // strip stands down.
        const DISCUSS_TURN_DONE_MS = 6000;
        var _discussStreamText = '';
        const DISCUSS_AGENT_KEY = 'proseview-discuss-agent';
        var _discussAgent = _readDiscussAgent();
        var _discussAgentAvailability = {};
        var _discussAgentLocal = {codex: null, claude: null};
        var _discussAgentPollTimer = null;

        function _readDiscussAgent() {
            var saved = null;
            try { saved = localStorage.getItem(DISCUSS_AGENT_KEY); } catch (e) {}
            if (DISCUSS_AGENTS.indexOf(saved) >= 0) return saved;
            var fallback = (typeof discussDefaultAgent !== 'undefined') ? discussDefaultAgent : 'codex';
            return DISCUSS_AGENTS.indexOf(fallback) >= 0 ? fallback : 'codex';
        }

        function discussAgentLabel(agent) {
            return (agent || _discussAgent) === 'claude' ? 'Claude' : 'Codex';
        }

        function _saveDiscussAgentLocal() {
            var previous = _discussAgentLocal[_discussAgent] || {};
            _discussAgentLocal[_discussAgent] = {
                attachments: _discussAttachments.slice(),
                selectedSkill: _discussSelectedSkill,
                conversationId: _discussConversationId || previous.conversationId || null
            };
        }

        function _restoreDiscussAgentLocal() {
            var bag = _discussAgentLocal[_discussAgent] || {};
            _discussAttachments = (bag.attachments || []).slice();
            _discussSelectedSkill = bag.selectedSkill || null;
        }

        // Rename every piece of chrome that used to say "Codex" unconditionally.
        function _applyDiscussAgentLabels() {
            var label = discussAgentLabel();
            var title = document.getElementById('discussTitle');
            if (title) title.textContent = label;
            var close = document.getElementById('discussClose');
            if (close) close.setAttribute('aria-label', 'Close the ' + label + ' panel');
            var stop = document.getElementById('discussStop');
            if (stop) stop.textContent = 'Stop ' + label;
            var inputLabel = document.getElementById('discussInputLabel');
            if (inputLabel) inputLabel.textContent = 'Ask ' + label + ' about your project';
        }

        function showDiscussAgentTab(agent, trigger) {
            agent = DISCUSS_AGENTS.indexOf(agent) >= 0 ? agent : 'codex';
            var panel = document.getElementById('discussPanel');
            var log = document.getElementById('discussLog');
            // Only skip reopening when this agent's conversation is already the
            // thing on screen. The dock being open on Scene or Analysis is not
            // the same as Codex being live in it, and treating it as such left
            // the tab showing an empty log that never connected.
            var live = panel && !panel.hidden && log && !log.hidden && _discussConversationId;
            if (live && agent === _discussAgent) { _showDiscussBody(); return; }
            if (panel && !panel.hidden) {
                saveDiscussDraft();
                _saveDiscussAgentLocal();
            }
            if (_discussEventSource) { _discussEventSource.close(); _discussEventSource = null; }
            _discussAgent = agent;
            try { localStorage.setItem(DISCUSS_AGENT_KEY, agent); } catch (e) {}
            _applyDiscussAgentLabels();
            openDiscuss(trigger || _discussReturnFocus);
        }

        // An agent you are not looking at still finishes, still gets stuck on
        // an approval. The dock being closed is exactly when that matters most,
        // so the poll keeps running -- it just never opens a conversation that
        // was never started, which would boot an agent nobody asked for.
        function _pollInactiveDiscussAgent() {
            clearTimeout(_discussAgentPollTimer);
            _discussAgentPollTimer = setTimeout(function() {
                var panel = document.getElementById('discussPanel');
                var live = panel && !panel.hidden && _discussConversationId;
                var targets = DISCUSS_AGENTS.filter(function(agent) {
                    // The agent on screen reports itself through its own event
                    // stream; polling it as well would only race that.
                    if (live && agent === _discussAgent) return false;
                    return !!(_discussAgentLocal[agent] || {}).conversationId;
                });
                Promise.all(targets.map(function(agent) {
                    var known = (_discussAgentLocal[agent] || {}).conversationId;
                    return fetch('/api/discuss/conversations/' + encodeURIComponent(known) + '/snapshot',
                                 {cache: 'no-store'})
                        .then(function(response) { return response.ok ? response.json() : null; })
                        .then(function(data) {
                            var snapshot = (data || {}).snapshot || {};
                            var pending = (snapshot.approvals || []).some(function(row) {
                                return row.status === 'pending';
                            });
                            var busy = !!snapshot.active_turn_id
                                || !!snapshot.active_request_id
                                || (snapshot.queue || []).length > 0;
                            _discussAgentState[agent] = pending ? 'attention' : (busy ? 'busy' : '');
                        })
                        .catch(function() {});
                })).then(function() {
                    _syncDiscussAmbientSignals();
                    _pollInactiveDiscussAgent();
                });
            }, 4000);
        }

        // '' | 'busy' | 'attention'. Working and waiting-on-you are different
        // problems: one resolves itself, the other never will until you act.
        function _markDiscussAgentTab(agent, state) {
            (UTILITY_TAB_IDS[agent] || []).forEach(function(id) {
                var el = document.getElementById(id);
                if (!el) return;
                el.classList.toggle('utility-tab-busy', state === 'busy');
                el.classList.toggle('utility-tab-attention', state === 'attention');
            });
        }

        // Asked for on every open rather than once per page: the skills live
        // under .proseview, which the file watcher ignores, so a writer who has
        // just rewritten a description gets it back by reopening the panel.
        function loadDiscussScenePasses() {
            return discussApi('/api/discuss/actions', {}).then(function(data) {
                var next = {};
                (data.actions || []).forEach(function(row) {
                    if (!row || !row.id) return;
                    next[row.id] = {
                        label: String(row.label || ''),
                        description: String(row.description || '')
                    };
                });
                var changed = JSON.stringify(next) !== JSON.stringify(_discussActionCopy);
                _discussActionCopy = next;
                // The cards were painted from the fallbacks above before this
                // answer arrived. Repaint only if it actually says something
                // else, and only while they are the thing on screen.
                if (changed && document.querySelector('.discuss-story-action')) renderDiscussSnapshot();
            }).catch(function() {});
        }

        function loadDiscussAgentAvailability() {
            return discussApi('/api/discuss/agents', {}).then(function(data) {
                (data.agents || []).forEach(function(row) {
                    _discussAgentAvailability[row.id] = row;
                    (UTILITY_TAB_IDS[row.id] || []).forEach(function(id) {
                        var el = document.getElementById(id);
                        if (!el) return;
                        el.classList.toggle('utility-tab-unavailable', !row.available);
                        if (!row.available && row.reason) el.title = row.label + ' is unavailable — ' + row.reason;
                    });
                });
            }).catch(function() {});
        }
        var _discussLocalErrorReload = false;
        var _discussReconnectTimer = null;

        // Drafts belong to the provider's project conversation. Their document
        // is stored separately so navigation cannot silently change context
        // underneath text the writer already composed.
        function discussDraftKey(doc, agent) {
            return 'proseview-draft:' + (agent || _discussAgent);
        }

        function legacyDiscussDraftKey(doc, agent) {
            return 'proseview-draft:' + (agent || _discussAgent) + ':' + discussDocumentKey(doc);
        }

        function discussDraftDocumentKey(agent) {
            return discussDraftKey(null, agent) + ':document';
        }

        function discussIncludeCurrentDocumentKey(agent) {
            return discussDraftKey(null, agent) + ':include-current-document';
        }

        function discussTurnDocument() {
            return _discussDraftDocument || discussDocument();
        }

        function discussTaskDocument(target) {
            var taskDocument = target && target.document;
            if (taskDocument && (taskDocument.kind === 'scene' || taskDocument.kind === 'file')
                && typeof taskDocument.path === 'string' && taskDocument.path) {
                return {kind: taskDocument.kind, path: taskDocument.path};
            }
            return null;
        }

        function discussLiveDocumentFor(targetDocument) {
            var current = discussDocument();
            if (!current || !targetDocument || current.kind !== targetDocument.kind
                || current.path !== targetDocument.path || targetDocument.kind !== 'scene') return null;
            return typeof currentSceneLiveDocumentSnapshot === 'function'
                ? currentSceneLiveDocumentSnapshot() : null;
        }

        function discussSelectionSnapshotFor(targetDocument) {
            return typeof currentSceneSelectionSnapshot === 'function'
                ? currentSceneSelectionSnapshot(targetDocument) : null;
        }

        function saveDiscussDraft() {
            var input = document.getElementById('discussInput');
            if (!input) return;
            var key = discussDraftKey(null, _discussAgent);
            if (input.value && !_discussDraftDocument) _discussDraftDocument = discussDocument();
            if (!input.value && !_discussSelection && !_discussPendingAction
                && !_discussRepositoryAction && !_discussIncludeCurrentDocument) {
                _discussDraftDocument = null;
            }
            try {
                if (input.value) sessionStorage.setItem(key, input.value);
                else sessionStorage.removeItem(key);
                if (_discussDraftDocument && (input.value || _discussIncludeCurrentDocument)) {
                    sessionStorage.setItem(discussDraftDocumentKey(_discussAgent), JSON.stringify(_discussDraftDocument));
                } else {
                    sessionStorage.removeItem(discussDraftDocumentKey(_discussAgent));
                }
                if (_discussIncludeCurrentDocument) {
                    sessionStorage.setItem(discussIncludeCurrentDocumentKey(_discussAgent), 'true');
                } else sessionStorage.removeItem(discussIncludeCurrentDocumentKey(_discussAgent));
            } catch(e) {}
        }

        function restoreDiscussDraft(doc, agent) {
            try {
                var key = discussDraftKey(doc, agent);
                var draft = sessionStorage.getItem(key) || '';
                var rawDocument = sessionStorage.getItem(discussDraftDocumentKey(agent));
                var savedDocument = rawDocument ? JSON.parse(rawDocument) : null;
                _discussIncludeCurrentDocument = sessionStorage.getItem(
                    discussIncludeCurrentDocumentKey(agent)
                ) === 'true';
                if (!draft && doc) {
                    var legacyKey = legacyDiscussDraftKey(doc, agent);
                    var legacyDraft = sessionStorage.getItem(legacyKey) || '';
                    if (legacyDraft) {
                        draft = legacyDraft;
                        savedDocument = Object.assign({}, doc);
                        sessionStorage.setItem(key, draft);
                        sessionStorage.setItem(discussDraftDocumentKey(agent), JSON.stringify(savedDocument));
                        sessionStorage.removeItem(legacyKey);
                    }
                }
                _discussDraftDocument = savedDocument && (savedDocument.kind === 'scene' || savedDocument.kind === 'file')
                    && typeof savedDocument.path === 'string' ? savedDocument : null;
                if (draft && !_discussDraftDocument && doc) _discussDraftDocument = Object.assign({}, doc);
                return draft;
            }
            catch(e) { _discussIncludeCurrentDocument = false; return ''; }
        }

        function hasLegacyDiscussDraft(doc, agent) {
            if (!doc) return false;
            try { return !!sessionStorage.getItem(legacyDiscussDraftKey(doc, agent)); }
            catch(e) { return false; }
        }

        function activateLegacyDiscussDraft(doc, agent) {
            var input = document.getElementById('discussInput');
            if (!input || input.value || _discussDraftDocument || _discussSelection
                || _discussPendingAction || _discussRepositoryAction || !doc) return false;
            var legacyKey = legacyDiscussDraftKey(doc, agent);
            try {
                var draft = sessionStorage.getItem(legacyKey) || '';
                if (!draft) return false;
                var savedDocument = {kind: doc.kind, path: doc.path};
                sessionStorage.setItem(discussDraftKey(doc, agent), draft);
                sessionStorage.setItem(discussDraftDocumentKey(agent), JSON.stringify(savedDocument));
                sessionStorage.removeItem(legacyKey);
                input.value = draft;
                _discussDraftDocument = savedDocument;
                _discussPreservedDraft = draft;
                _saveDiscussAgentLocal();
                renderDiscussContext();
                renderDiscussTaskMode();
                renderDiscussContextOptions();
                return true;
            } catch(e) { return false; }
        }

        function discussDocument() {
            var view = document.documentElement.dataset.view;
            if (view === 'scene' && curIdx >= 0 && paths[curIdx]) return {kind: 'scene', path: paths[curIdx]};
            if (view === 'file') {
                var title = document.getElementById('filePreviewTitle');
                var path = title ? title.textContent.trim() : '';
                var node = repoFileByPath[path];
                if (path && node && node.is_text && !node.too_large) return {kind: 'file', path: path};
            }
            return null;
        }

        function discussDocumentKey(doc) { return doc ? doc.kind + ':' + doc.path : ''; }

        function captureDiscussSelection() {
            var selection = window.getSelection ? window.getSelection() : null;
            if (!selection || selection.isCollapsed || !selection.anchorNode) return '';
            var anchor = selection.anchorNode.nodeType === 1 ? selection.anchorNode : selection.anchorNode.parentElement;
            if (!anchor || (!anchor.closest('#modalBody') && !anchor.closest('#filePreviewBody'))) return '';
            return selection.toString().slice(0, 65536);
        }

        function discussApi(path, body) {
            var controller = typeof AbortController === 'function' ? new AbortController() : null;
            var timedOut = false;
            var timeoutMs = Math.max(1, Number(_discussRequestTimeoutMs) || 15000);
            var timeout = controller ? setTimeout(function() {
                timedOut = true;
                controller.abort();
            }, timeoutMs) : null;
            return fetch(path, {
                method: 'POST',
                headers: pvHeaders(),
                body: JSON.stringify(body || {}),
                signal: controller ? controller.signal : undefined
            }).then(function(response) {
                return response.json().catch(function() { return {}; }).then(function(data) {
                    if (!response.ok) throw new Error(data.error || ('Request failed (' + response.status + ')'));
                    return data;
                });
            }).catch(function(error) {
                if (timedOut || (error && error.name === 'AbortError')) {
                    throw new Error('Request timed out. Check the connection and try again.');
                }
                if (error && (error.name === 'TypeError' || /failed to fetch|network error/i.test(error.message || ''))) {
                    var networkError = new Error('Proseview server is not responding. It will keep trying to reconnect.');
                    networkError.name = 'NetworkError';
                    throw networkError;
                }
                throw error;
            }).finally(function() {
                if (timeout !== null) clearTimeout(timeout);
            });
        }

        function setDiscussConnection(state, reason) {
            var node = document.getElementById('discussConnection');
            node.textContent = state + (reason ? ' — ' + reason : '');
            node.dataset.state = state;
        }

        // Where focus lands when the dock closes. A trigger inside the dock --
        // one of the tabs -- is no use: closing hides the subtree it lives in,
        // so focusing it silently fails and focus falls to the document. Prefer
        // the toolbar button that owns the panel, then the back button.
        function _returnFocusTarget(trigger) {
            const outside = trigger
                && trigger.getClientRects && trigger.getClientRects().length
                && !trigger.closest('#discussPanel, #terminalPanel');
            if (outside) return trigger;
            const modal = document.querySelector('#sceneModal .discuss-open-btn');
            const preview = document.querySelector('#file-preview-panel .discuss-open-btn');
            const visible = function(el) {
                return el && el.getClientRects && el.getClientRects().length ? el : null;
            };
            return visible(modal) || visible(preview)
                || document.querySelector('#sceneModal .scene-back-btn');
        }

        function openDiscuss(trigger, options) {
            options = options || {};
            closeDiscussModelPicker();
            var doc = discussDocument();
            if (!doc) {
                alert('Open a scene or supported text file before starting a discussion.');
                return;
            }
            if (_discussEventSource) { _discussEventSource.close(); _discussEventSource = null; }
            clearTimeout(_discussRefreshTimer);
            clearTimeout(_discussReconnectTimer);
            _discussReturnFocus = _returnFocusTarget(trigger);
            _discussSelection = options.selection !== undefined ? String(options.selection || '') : captureDiscussSelection();
            _discussSelectionRange = options.selectionRange && typeof options.selectionRange.start === 'number'
                ? {start: options.selectionRange.start, end: options.selectionRange.end}
                : null;
            _discussSelectionSnapshot = options.selectionSnapshot || null;
            _discussSelectionSourceTaskId = options.selectionSourceTaskId || null;
            _discussLiveDocument = options.liveDocument || null;
            _discussDraftDocument = null;
            _discussAttachments = [];
            _discussIncludeCurrentDocument = false;
            _discussPendingAction = options.actionId || null;
            _discussRepositoryAction = null;
            _discussRetryOfTaskId = null;
            _discussSelectedSkill = null;
            _discussAutoRun = !!options.runImmediately;
            var requestedAutoRun = _discussAutoRun;
            var requestedAction = _discussPendingAction;
            var requestedSelection = _discussSelection;
            var requestedSelectionRange = _discussSelectionRange;
            var requestedSelectionSnapshot = _discussSelectionSnapshot;
            var requestedSelectionSourceTaskId = _discussSelectionSourceTaskId;
            var requestedLiveDocument = _discussLiveDocument;
            closeDiscussContextPicker();
            clearDiscussError();
            var panel = document.getElementById('discussPanel');
            panel.hidden = false;
            // Whatever the dock was showing, it is showing Codex now. Doing
            // this here rather than in showDiscussTab keeps every route in --
            // the tab, a selection action, a keyboard shortcut -- agreeing.
            _showDiscussBody();
            document.getElementById('discussSend').disabled = true;
            _discussOpenFailed = false;
            _discussConversationId = null;
            document.body.classList.add('discuss-open');
            try { sessionStorage.setItem('proseview-panel-open', 'true'); } catch(e) {}
            if (typeof _termDock !== 'undefined' && _termDock === 'right') {
                var terminal = document.getElementById('terminalPanel');
                if (terminal && !terminal.hidden) terminal.hidden = true;
                document.body.classList.remove('terminal-right-open');
            }
            renderDiscussContext();
            renderDiscussTaskMode();
            setDiscussConnection('Restoring conversation', '');
            var key = discussDocumentKey(doc);
            _discussDocumentKey = key;
            var input = document.getElementById('discussInput');
            _restoreDiscussAgentLocal();
            _discussPreservedDraft = restoreDiscussDraft(doc, _discussAgent);
            input.value = requestedAutoRun ? '' : _discussPreservedDraft;
            if (requestedSelection || requestedAction) {
                _discussDraftDocument = Object.assign({}, doc);
                _discussIncludeCurrentDocument = false;
            } else if (input.value && !_discussDraftDocument) {
                _discussDraftDocument = Object.assign({}, doc);
            }
            renderDiscussContext();
            var openBody = {kind: doc.kind, path: doc.path, agent: _discussAgent};
            discussApi('/api/discuss/conversations/open', openBody).then(function(data) {
                if (_discussDocumentKey !== key) return;
                _discussConversationId = data.conversation_id;
                _discussSnapshot = data.snapshot;
                renderDiscussSnapshot();
                connectDiscussEvents();
                // The composer was rendered after its selection/draft state was
                // restored above. Rebuilding it here detaches an open presets
                // popover while the conversation request finishes.
                document.getElementById('discussSend').disabled = false;
                _markDiscussAgentTab(_discussAgent, false);
                loadDiscussModelCatalog(_discussAgent);
                loadDiscussAgentAvailability();
                loadDiscussScenePasses();
                _pollInactiveDiscussAgent();
                if (options.showSkills) loadDiscussSkills();
                else if (requestedAutoRun && requestedAction) {
                    runDiscussSelectionAction(
                        requestedAction, requestedSelection, requestedSelectionRange, requestedLiveDocument,
                        0, null, requestedSelectionSnapshot, requestedSelectionSourceTaskId
                    );
                }
                else input.focus();
            }).catch(function(error) {
                _discussOpenFailed = true;
                
                var msg = error.message || '';
                var label = discussAgentLabel();
                if (msg.includes('Codex') || msg.includes('API key') || msg.includes('app-server')
                    || msg.includes('claude-agent-sdk') || msg.includes('Claude Code')) {
                    setDiscussConnection('Not connected', '');
                    // Built as nodes, not markup: the reason is an error string
                    // and must never be interpolated into innerHTML.
                    var empty = document.createElement('div');
                    empty.className = 'discuss-empty-state';
                    var icon = document.createElement('div');
                    icon.className = 'discuss-empty-icon';
                    icon.textContent = '🤖';
                    var heading = document.createElement('h3');
                    heading.textContent = label + ' is not connected';
                    var blurb = document.createElement('p');
                    blurb.className = 'discuss-empty-blurb';
                    blurb.textContent = 'Proseview runs entirely locally, but you can connect an AI agent '
                        + 'to discuss your manuscript, fix continuity errors, and review pacing.';
                    var reason = document.createElement('p');
                    reason.className = 'discuss-empty-reason';
                    var em = document.createElement('em');
                    em.textContent = msg;
                    reason.appendChild(em);
                    [icon, heading, blurb, reason].forEach(function(node) { empty.appendChild(node); });
                    var log = document.getElementById('discussLog');
                    log.textContent = '';
                    log.appendChild(empty);
                    document.getElementById('discussComposerArea').hidden = true;
                    document.getElementById('discussAnnouncement').textContent = label + ' is not connected.';
                    return;
                }

                setDiscussConnection('Unavailable', error.message);
                renderDiscussError(error.message, {kind: error.name === 'NetworkError' ? 'transport' : 'request'});
                var button = document.getElementById('discussSend');
                button.textContent = 'Try again';
                button.disabled = false;
                document.getElementById('discussAnnouncement').textContent = 'Discuss could not open. ' + error.message;
            });
        }

        function openDiscussForSelection(trigger, selection, options) {
            options = options || {};
            options.selection = String(selection || '');
            var panel = document.getElementById('discussPanel');
            var doc = discussDocument();
            if (panel && !panel.hidden && doc && _discussConversationId && _discussDocumentKey === discussDocumentKey(doc)) {
                _discussReturnFocus = (trigger && trigger.getClientRects && trigger.getClientRects().length)
                    ? trigger : document.querySelector('#sceneModal .scene-back-btn');
                _discussSelection = options.selection;
                _discussDraftDocument = Object.assign({}, doc);
                _discussIncludeCurrentDocument = false;
                _discussSelectionRange = options.selectionRange || null;
                _discussSelectionSnapshot = options.selectionSnapshot || null;
                _discussSelectionSourceTaskId = options.selectionSourceTaskId || null;
                _discussLiveDocument = options.liveDocument || null;
                _discussPendingAction = options.actionId || null;
                _discussRepositoryAction = null;
                _discussRetryOfTaskId = null;
                _discussSelectedSkill = null;
                _discussAutoRun = !!options.runImmediately;
                renderDiscussContext(); renderDiscussTaskMode();
                if (options.showSkills) loadDiscussSkills();
                else if (_discussAutoRun && _discussPendingAction) {
                    runDiscussSelectionAction(
                        _discussPendingAction, _discussSelection, _discussSelectionRange, _discussLiveDocument,
                        0, null, _discussSelectionSnapshot, _discussSelectionSourceTaskId
                    );
                } else document.getElementById('discussInput').focus();
                return;
            }
            openDiscuss(trigger, options);
        }

        function closeDiscuss() {
            saveDiscussDraft();
            _saveDiscussAgentLocal();
            closeDiscussContextPicker();
            var panel = document.getElementById('discussPanel');
            panel.hidden = true;
            document.body.classList.remove('discuss-open');
            try { sessionStorage.setItem('proseview-panel-open', 'false'); } catch(e) {}
            if (_discussEventSource) { _discussEventSource.close(); _discussEventSource = null; }
            clearTimeout(_discussRefreshTimer);
            clearTimeout(_discussReconnectTimer);
            var focus = _discussReturnFocus;
            _discussReturnFocus = null;
            if (focus && focus.isConnected && typeof focus.focus === 'function') focus.focus();
            _syncDiscussAmbientSignals();
        }

        function hideDiscussForTerminal() {
            var panel = document.getElementById('discussPanel');
            if (panel && !panel.hidden) {
                saveDiscussDraft();
                _saveDiscussAgentLocal();
                panel.hidden = true;
                document.body.classList.remove('discuss-open');
            }
        }

        // ── Scene panel tabs ────────────────────────────────────────────
        //
        // Four tabs in one dock, ordered by how much they can surprise you.
        // Scene and Analysis are deterministic -- frontmatter, counts, and the
        // highlight passes, all derived from the file on disk. Codex is the
        // only surface that calls a model. Terminal is a shell. Keeping that
        // boundary legible is the point of the split.
        const SCENE_PANEL_TABS = ['scene', 'analysis', 'history', 'codex', 'claude', 'terminal'];
        const SCENE_PANEL_TAB_KEY = 'proseview-scene-panel-tab';

        function _readScenePanelTab() {
            var saved = null;
            try { saved = localStorage.getItem(SCENE_PANEL_TAB_KEY); } catch (e) {}
            // "details" was this tab's name before it split into Scene and
            // Analysis; a stored value from then must not leave the dock blank.
            if (saved === 'details') saved = 'scene';
            // "discuss" was the single agent tab before Codex and Claude got
            // one each. Send those readers to whichever agent they last used
            // rather than dropping them on Scene.
            if (saved === 'discuss') saved = _discussAgent;
            return SCENE_PANEL_TABS.indexOf(saved) >= 0 ? saved : 'scene';
        }

        // Both copies of the tab row -- the panel's and the terminal's -- are
        // driven from here, so whichever one you are looking at agrees with
        // what the dock is actually showing.
        const UTILITY_TAB_IDS = {
            scene: ['utilityTabScene', 'termUtilityTabScene'],
            analysis: ['utilityTabAnalysis', 'termUtilityTabAnalysis'],
            history: ['utilityTabHistory', 'termUtilityTabHistory'],
            codex: ['utilityTabCodex', 'termUtilityTabCodex'],
            claude: ['utilityTabClaude', 'termUtilityTabClaude'],
            terminal: ['utilityTabTerminal', 'termUtilityTabTerminal']
        };

        function _setUtilityTab(name) {
            SCENE_PANEL_TABS.forEach(function(tab) {
                UTILITY_TAB_IDS[tab].forEach(function(id) {
                    const el = document.getElementById(id);
                    if (!el) return;
                    const on = tab === name;
                    el.classList.toggle('active', on);
                    el.setAttribute('aria-selected', on ? 'true' : 'false');
                });
            });
            // Terminal is a destination, not a scene view: reopening the panel
            // on it would show a shell where the reader expected their scene.
            if (name === 'terminal') return;
            try { localStorage.setItem(SCENE_PANEL_TAB_KEY, name); } catch (e) {}
        }

        function _setDockHeading(text, showConnection) {
            const title = document.getElementById('discussTitle');
            const conn = document.getElementById('discussConnection');
            const heading = document.querySelector('.discuss-heading');
            if (title && text !== null) {
                if (!title.dataset.discussLabel) title.dataset.discussLabel = title.textContent;
                title.textContent = text;
            } else if (title && title.dataset.discussLabel) {
                title.textContent = title.dataset.discussLabel;
            }
            if (conn) conn.hidden = !showConnection;
            // The scene tabs are already named by the tab that is lit. Repeating
            // the name underneath is the duplication that made the old header
            // read as two competing sets of controls. Codex keeps its heading
            // because the connection state hangs off it.
            if (heading) heading.hidden = !showConnection;
        }

        // Everything a scene pane has to cover, and -- separately -- the parts
        // that come back when Codex returns. "New activity" is not in the
        // second list: it earns its visibility from the log, so restoring it
        // blind would announce activity that had already been read.
        function _discussBodyEls() {
            return ['discussContext', 'discussLog', 'discussComposerArea', 'discussNewActivity', 'discussTurnStatus']
                .map(function(id) { return document.getElementById(id); }).filter(Boolean);
        }

        function _discussVisibleEls() {
            return ['discussContext', 'discussLog', 'discussComposerArea']
                .map(function(id) { return document.getElementById(id); }).filter(Boolean);
        }

        const SCENE_PANEL_PANES = {
            scene: {id: 'sceneDetailsPane', heading: 'Scene', render: renderSceneDetailsPane},
            analysis: {id: 'sceneAnalysisPane', heading: 'Analysis', render: renderSceneAnalysisPane},
            history: {id: 'sceneHistoryPane', heading: 'History', render: function() { if(paths[curIdx]) loadSceneHistory(paths[curIdx]); }}
        };

        function showScenePanelTab(name) {
            const spec = SCENE_PANEL_PANES[name];
            const panel = document.getElementById('discussPanel');
            if (!spec || !panel) return;
            const pane = document.getElementById(spec.id);
            if (!pane) return;
            hideRightTerminalForPanel();
            panel.hidden = false;
            document.body.classList.add('discuss-open');
            try { sessionStorage.setItem('proseview-panel-open', 'true'); } catch(e) {}
            _discussBodyEls().forEach(function(el) { el.hidden = true; });
            Object.keys(SCENE_PANEL_PANES).forEach(function(key) {
                const other = document.getElementById(SCENE_PANEL_PANES[key].id);
                if (other) other.hidden = key !== name;
            });
            // "Codex / Live" names the Discuss connection; it has no business
            // sitting above a pane of counts and frontmatter.
            _setDockHeading(spec.heading, false);
            spec.render();
            _setUtilityTab(name);
        }

        // Kept as a named seam: it is the entry point the scene modal and the
        // tests reach for, and it now means "the deterministic scene tab".
        function showSceneDetailsTab() { showScenePanelTab('scene'); }
        function showSceneAnalysisTab() { showScenePanelTab('analysis'); }

        function _showDiscussBody() {
            Object.keys(SCENE_PANEL_PANES).forEach(function(key) {
                const pane = document.getElementById(SCENE_PANEL_PANES[key].id);
                if (pane) pane.hidden = true;
            });
            _discussVisibleEls().forEach(function(el) { el.hidden = false; });
            _setDockHeading(null, true);
            _applyDiscussAgentLabels();
            _setUtilityTab(_discussAgent);
        }

        function showDiscussTab(trigger) { showDiscussAgentTab(_discussAgent, trigger); }

        function hideRightTerminalForPanel() {
            const term = document.getElementById('terminalPanel');
            if (term && typeof _termDock !== 'undefined' && _termDock === 'right') term.hidden = true;
        }

        // ── Dock scope ──────────────────────────────────────────────────────
        // Leaving a scene closes the dock. Three of the four tabs describe the
        // document that just closed -- Scene and Analysis directly, and Codex
        // refuses to start without one, see the guard at the top of
        // openDiscuss. Only Terminal stands alone, and switching to it would
        // drop the reader into a shell they never asked for, spawning a fresh
        // one when none was running: a process created as a side effect of
        // clicking "Dashboard".
        //
        // Closing costs nothing. closeScenePanel only hides the terminal, so a
        // live session keeps running and comes back intact when the dock is
        // reopened; _termSessions is never touched.
        const SCENE_SCOPED_TABS = ['scene', 'analysis', 'history', 'codex', 'claude'];

        function _terminalIsAvailable() {
            return typeof terminalAvailable === 'undefined' || !!terminalAvailable;
        }

        function _sceneScopedViewIsOpen() {
            const view = document.documentElement.dataset.view;
            return view === 'scene' || view === 'file';
        }

        function syncScenePanelScope() {
            const scoped = _sceneScopedViewIsOpen();
            SCENE_SCOPED_TABS.forEach(function(tab) {
                UTILITY_TAB_IDS[tab].forEach(function(id) {
                    const el = document.getElementById(id);
                    if (el) el.hidden = !scoped;
                });
            });
            // Terminal is all the dashboard dock can offer, so without one the
            // button would open an empty panel.
            const dashBtn = document.getElementById('dashboardPanelBtn');
            if (dashBtn) dashBtn.hidden = !_terminalIsAvailable();

            if (!scoped) {
                closeScenePanel();
            } else {
                var shouldBeOpen = false;
                try { shouldBeOpen = sessionStorage.getItem('proseview-panel-open') === 'true'; } catch(e) {}
                var panel = document.getElementById('discussPanel');
                if (shouldBeOpen && panel && panel.hidden) {
                    var tab = _readScenePanelTab();
                    if (DISCUSS_AGENTS.indexOf(tab) >= 0) showDiscussAgentTab(tab);
                    else showScenePanelTab(tab);
                }
            }
        }

        // Observed rather than called from each transition: `data-view` is set
        // and cleared in seven places across three files, and the dock being
        // left on a stale scene is exactly what happens when one is missed.
        new MutationObserver(syncScenePanelScope).observe(
            document.documentElement,
            {attributes: true, attributeFilter: ['data-view']}
        );
        document.addEventListener('DOMContentLoaded', syncScenePanelScope);

        function toggleScenePanel(trigger) {
            const panel = document.getElementById('discussPanel');
            const term = document.getElementById('terminalPanel');
            const termInDock = term && !term.hidden
                && typeof _termDock !== 'undefined' && _termDock === 'right';
            if ((panel && !panel.hidden) || termInDock) { closeScenePanel(); return; }
            const tab = _readScenePanelTab();
            // On the dashboard the Terminal is the only tab with anything to
            // show. Opening it here is fine where falling into it on navigation
            // was not: this is a button the reader pressed. `_setUtilityTab`
            // never stores 'terminal', so their preferred tab survives.
            if (!_sceneScopedViewIsOpen() && SCENE_SCOPED_TABS.indexOf(tab) >= 0) {
                if (_terminalIsAvailable()) showRightTerminal();
                return;
            }
            // One call, not two: openDiscuss opens a conversation on the
            // server, and doing it twice queued a second thread/read behind the
            // first for every reader whose last tab was an agent.
            if (DISCUSS_AGENTS.indexOf(tab) >= 0) showDiscussAgentTab(tab, trigger);
            else showScenePanelTab(tab);
        }

        // The Panel button owns the whole dock, so closing has to cover the
        // right-docked terminal too -- otherwise pressing it while a shell was
        // showing did nothing visible.
        function closeScenePanel() {
            const term = document.getElementById('terminalPanel');
            var termInDock = term && typeof _termDock !== 'undefined' && _termDock === 'right';
            const panel = document.getElementById('discussPanel');
            var wasOpen = (panel && !panel.hidden) || (term && !term.hidden && termInDock);

            if (termInDock) term.hidden = true;
            if (panel && !panel.hidden) closeDiscuss();
            
            if (wasOpen) {
                try { sessionStorage.setItem('proseview-panel-open', 'false'); } catch(e) {}
            }
        }

        function renderSceneDetailsPane() {
            const pane = document.getElementById('sceneDetailsPane');
            if (!pane) return;
            pane.replaceChildren();
            // The context body starts life detached, built by the scene
            // renderer. It carries frontmatter, story fields, characters,
            // related documents and tasks.
            const body = window._sceneContextBody;
            if (body) { body.hidden = false; pane.appendChild(body); }
            else pane.appendChild(_scenePanelEmpty('Open a scene to see its frontmatter, links and tasks.'));
        }

        function _scenePanelEmpty(text) {
            const p = document.createElement('p');
            p.className = 'scene-panel-empty';
            p.textContent = text;
            return p;
        }

        function renderSceneAnalysisPane() {
            const pane = document.getElementById('sceneAnalysisPane');
            if (!pane) return;
            pane.replaceChildren();
            // Cache the stat grid on first sight. replaceChildren() detaches
            // it, so a second render could not find it through getElementById
            // and the pane silently lost its numbers.
            if (!window._sceneDetailsNodes) {
                window._sceneDetailsNodes = {stats: document.getElementById('modalStats')};
            }
            const stats = window._sceneDetailsNodes.stats;
            const path = (typeof paths !== 'undefined' && typeof curIdx !== 'undefined')
                ? paths[curIdx] : null;
            if (!path) {
                pane.appendChild(_scenePanelEmpty('Open a scene to see its measures and highlight passes.'));
                return;
            }
            pane.appendChild(_scenePanelHeading('Measures'));
            if (stats) { stats.hidden = false; pane.appendChild(stats); }
            const passHeading = _scenePanelHeading('Highlight passes');
            const allBtn = document.createElement('button');
            allBtn.type = 'button';
            allBtn.id = 'scenePassAllBtn';
            allBtn.className = 'scene-pass-all';
            allBtn.textContent = 'All';
            allBtn.setAttribute('aria-pressed', 'false');
            allBtn.title = 'Turn every pass on, or clear them all';
            allBtn.onclick = toggleAllHighlights;
            passHeading.appendChild(allBtn);
            pane.appendChild(passHeading);
            pane.appendChild(_scenePassList(path));
            syncAllBtn();

            const m = (typeof meta !== 'undefined' && meta[path]) || {};
            const dlg = (m.top_dlg && m.top_dlg.length) ? m.top_dlg.join(', ') : null;
            if (dlg) {
                const note = document.createElement('p');
                note.className = 'scene-panel-note';
                note.append('Top dialogue keywords: ');
                const strong = document.createElement('strong');
                strong.textContent = dlg;
                note.appendChild(strong);
                pane.appendChild(note);
            }
        }

        function _scenePanelHeading(text) {
            const h = document.createElement('p');
            h.className = 'scene-panel-heading';
            h.textContent = text;
            return h;
        }

        // One row per pass: a switch, the name, its examples, and how many
        // matches this scene actually has. The count is the reason the list is
        // ordered by it -- a pass with nothing to show should not sit above one
        // with thirty hits, and a zero row says "nothing here" without being
        // clicked.
        function _scenePassList(path) {
            const list = document.createElement('div');
            list.className = 'scene-pass-list';
            list.id = 'scenePassList';
            const byPass = ((typeof highlightsByPath !== 'undefined' && highlightsByPath[path])
                || {}).highlights || {};
            PASS_ORDER.map(function(name) {
                const hits = byPass[name];
                return {name: name, count: Array.isArray(hits) ? hits.length : 0};
            }).sort(function(a, b) {
                return b.count - a.count || PASS_ORDER.indexOf(a.name) - PASS_ORDER.indexOf(b.name);
            }).forEach(function(entry) {
                list.appendChild(_scenePassRow(entry.name, entry.count));
            });
            return list;
        }

        function _scenePassRow(name, count) {
            const on = !!hls[name];
            const row = document.createElement('button');
            row.type = 'button';
            row.className = 'scene-pass-row' + (on ? ' is-on' : '') + (count ? '' : ' is-empty');
            row.id = 'pass-row-' + name;
            row.dataset.pass = name;
            row.setAttribute('aria-pressed', on ? 'true' : 'false');
            // The examples are always visible; the note is the hover and
            // keyboard-focus layer, never something you must reach to read the row.
            if (PASS_NOTES[name]) row.title = PASS_NOTES[name];
            row.onclick = function() { toggleHighlight(name); };

            const sw = document.createElement('span');
            sw.className = 'scene-pass-switch';
            sw.setAttribute('aria-hidden', 'true');

            const label = document.createElement('span');
            label.className = 'scene-pass-label';
            const title = document.createElement('span');
            title.className = 'scene-pass-name';
            title.textContent = PASS_LABELS[name] || name;
            const example = document.createElement('span');
            example.className = 'scene-pass-example';
            example.textContent = PASS_EXAMPLES[name] || '';
            label.append(title, example);

            const n = document.createElement('span');
            n.className = 'scene-pass-count';
            n.textContent = String(count);
            n.title = count === 1 ? '1 match in this scene' : count + ' matches in this scene';

            row.append(sw, label, n);
            return row;
        }

        // Called whenever a pass is toggled from anywhere, so the list agrees
        // with the prose without being rebuilt.
        function syncScenePassRows() {
            document.querySelectorAll('#scenePassList .scene-pass-row').forEach(function(row) {
                const on = !!hls[row.dataset.pass];
                row.classList.toggle('is-on', on);
                row.setAttribute('aria-pressed', on ? 'true' : 'false');
            });
        }

        function showRightTerminal() {
            hideDiscussForTerminal();
            if (typeof _termDock !== 'undefined') _termDock = 'right';
            try { localStorage.setItem('proseview-terminal-dock', 'right'); } catch(e) {}
            _setUtilityTab('terminal');
            var panel = document.getElementById('terminalPanel');
            if (typeof _termSessions !== 'undefined' && _termSessions.length) {
                panel.hidden = false;
                _applyTerminalDock();
            } else {
                openShellTerminal();
            }
        }

        function discussFollowActiveDocument() {
            var panel = document.getElementById('discussPanel');
            if (!panel || panel.hidden) return;
            var doc = discussDocument();
            var key = discussDocumentKey(doc);
            if (!doc || key === _discussDocumentKey) return;
            _discussDocumentKey = key;
            renderDiscussContext();
            renderDiscussTaskMode();
            var waitingLegacyDraft = hasLegacyDiscussDraft(doc, _discussAgent);
            if (!_discussDraftDocument && activateLegacyDiscussDraft(doc, _discussAgent)) {
                document.getElementById('discussAnnouncement').textContent =
                    'Conversation kept open. Restored the saved draft for ' + doc.path + '.';
            } else if (_discussDraftDocument && waitingLegacyDraft) {
                document.getElementById('discussAnnouncement').textContent =
                    'Conversation kept open. Your current draft is still here. A saved draft for this file '
                    + 'is waiting and will appear after you send or clear this draft.';
            } else {
                document.getElementById('discussAnnouncement').textContent = _discussIncludeCurrentDocument
                    ? 'Conversation kept open. ' + discussTurnDocument().path + ' remains attached.'
                    : _discussDraftDocument
                    ? 'Conversation kept open. Your draft is still here; no file is attached.'
                    : 'Conversation kept open. No file is attached.';
            }
        }

        function connectDiscussEvents() {
            if (_discussEventSource) _discussEventSource.close();
            clearTimeout(_discussReconnectTimer);
            if (!_discussConversationId) return;
            var cid = _discussConversationId;
            var source = new EventSource(pvEventSourceUrl(
                '/api/discuss/conversations/' + encodeURIComponent(cid) + '/events'
            ));
            _discussEventSource = source;
            source.onopen = function() {
                if (_discussConversationId === cid && (!_discussSnapshot || _discussSnapshot.connection !== 'Unavailable')) {
                    clearDiscussTransportError();
                    setDiscussConnection('Live', '');
                }
            };
            source.onerror = function() {
                if (_discussConversationId !== cid || _discussEventSource !== source) return;
                if (source.readyState === EventSource.CLOSED) {
                    showDiscussReloadRequired(source);
                    return;
                }
                setDiscussConnection('Reconnecting', 'Proseview server unavailable');
                scheduleDiscussReconnectProbe(cid, source);
            };
            source.addEventListener('snapshot', function(event) {
                setDiscussConnection('Restoring conversation', '');
                _discussSnapshot = JSON.parse(event.data);
                renderDiscussSnapshot();
            });
            ['connection', 'conversation.reset', 'turn.queued', 'turn.cancelled', 'turn.preparing', 'turn.started', 'turn.completed', 'turn.idle', 'response.completed', 'progress.delta',
             'plan.updated', 'activity.updated', 'approval.requested', 'approval.resolved', 'approval.expired', 'task.ready', 'task.failed',
             'task.updated', 'tasks.cleared', 'notice.dismissed', 'model.changed', 'warning', 'error'].forEach(function(type) {
                source.addEventListener(type, function(event) {
                    if (type === 'connection') {
                        var detail = JSON.parse(event.data);
                        setDiscussConnection(detail.state, detail.reason || '');
                    }
                    if (type === 'turn.started' || type === 'conversation.reset') _discussStreamText = '';
                    if (type === 'approval.requested') {
                        var request = JSON.parse(event.data);
                        _discussLastApproval = request.request_id || '';
                    }
                    if (type === 'task.ready' || type === 'task.failed') {
                        var completedTask = JSON.parse(event.data);
                        var requestId = String(completedTask.client_request_id || '');
                        var submittedHere = !!_discussAutoReviewRequests[requestId];
                        if (submittedHere) delete _discussAutoReviewRequests[requestId];
                        if (type === 'task.ready' && completedTask.kind === 'alternatives' && submittedHere) {
                            autoReviewDiscussTask(completedTask.task_id);
                        }
                    }
                    scheduleDiscussSnapshot();
                });
            });
            source.addEventListener('skills.changed', function() { if (!document.getElementById('discussSkillsPicker').hidden) loadDiscussSkills(true); });
            source.addEventListener('response.delta', function(event) {
                var detail = JSON.parse(event.data);
                appendDiscussStreamDelta(detail.text || '');
            });
        }

        function scheduleDiscussReconnectProbe(cid, source) {
            clearTimeout(_discussReconnectTimer);
            _discussReconnectTimer = setTimeout(function() {
                var panel = document.getElementById('discussPanel');
                if (!panel || panel.hidden || _discussConversationId !== cid || _discussEventSource !== source) return;
                fetch('/api/discuss/conversations/' + encodeURIComponent(cid) + '/snapshot', {cache: 'no-store'})
                    .then(function(response) {
                        if (response.status === 404) {
                            showDiscussReloadRequired(source);
                            return null;
                        }
                        if (!response.ok) throw new Error('Request failed (' + response.status + ')');
                        return response.json();
                    })
                    .then(function(data) {
                        if (!data || !data.snapshot || _discussConversationId !== cid) return;
                        _discussSnapshot = data.snapshot;
                        clearDiscussTransportError();
                        renderDiscussSnapshot();
                        if (source.readyState === EventSource.CLOSED) connectDiscussEvents();
                    })
                    .catch(function() {
                        if (_discussConversationId !== cid) return;
                        renderDiscussError(
                            'Proseview server is not responding. It will keep trying to reconnect.',
                            {kind: 'transport'}
                        );
                    });
            }, 900);
        }

        function showDiscussReloadRequired(source) {
            clearTimeout(_discussReconnectTimer);
            if (source) source.close();
            if (_discussEventSource === source) _discussEventSource = null;
            saveDiscussDraft();
            setDiscussConnection('Reload required', 'Proseview server restarted');
            renderDiscussError(
                'Proseview restarted. Reload this page to reconnect.',
                {kind: 'transport', reload: true}
            );
            var button = document.getElementById('discussSend');
            button.disabled = true;
            button.textContent = 'Reload';
            document.getElementById('discussAnnouncement').textContent = 'Proseview restarted. Reload the page to reconnect; your question draft is saved.';
        }

        function scheduleDiscussSnapshot() {
            clearTimeout(_discussRefreshTimer);
            _discussRefreshTimer = setTimeout(function() {
                if (!_discussConversationId) return;
                fetch('/api/discuss/conversations/' + encodeURIComponent(_discussConversationId) + '/snapshot', {cache: 'no-store'})
                    .then(function(response) { return response.json(); })
                    .then(function(data) { if (data.snapshot) { _discussSnapshot = data.snapshot; renderDiscussSnapshot(); } })
                    .catch(function() {
                        var panel = document.getElementById('discussPanel');
                        if (panel && !panel.hidden) setDiscussConnection('Reconnecting', '');
                    });
            }, 35);
        }

        function appendDiscussStreamDelta(text) {
            var log = document.getElementById('discussLog');
            var atBottom = discussIsAtBottom(log);
            // The text lives outside the DOM so the next snapshot render can
            // put it back; the node alone did not survive replaceChildren().
            _discussStreamText += text;
            var draft = log.querySelector('.discuss-stream-draft');
            if (!draft) {
                draft = elementWith('discuss-message assistant discuss-stream-draft');
                draft.appendChild(elementWith('discuss-message-label', discussAgentLabel()));
                draft.appendChild(document.createTextNode(''));
                log.appendChild(draft);
            }
            draft.lastChild.textContent = _discussStreamText;
            discussAfterActivity(atBottom);
        }

        function normalizeProsviewRepositoryPath(value) {
            var parts = String(value || '').replace(/\\/g, '/').split('/');
            var clean = [];
            for (var i = 0; i < parts.length; i++) {
                var part = parts[i];
                if (!part || part === '.') continue;
                if (part === '..') {
                    if (!clean.length) return null;
                    clean.pop();
                } else clean.push(part);
            }
            return clean.join('/');
        }

        function prosviewRepositoryRoot() {
            if (_prosviewRepositoryRootCache !== null) return _prosviewRepositoryRootCache;
            var keys = Object.keys(typeof repositoryFileByPath === 'undefined' ? {} : repositoryFileByPath);
            for (var i = 0; i < keys.length; i++) {
                var node = repositoryFileByPath[keys[i]];
                if (!node || !node.is_scene || !node.scene_path || !meta[node.scene_path]) continue;
                var absolute = String(meta[node.scene_path].abs_path || '').replace(/\\/g, '/');
                var suffix = '/' + String(node.path || '').replace(/\\/g, '/');
                if (absolute.endsWith(suffix)) {
                    _prosviewRepositoryRootCache = absolute.slice(0, -suffix.length);
                    return _prosviewRepositoryRootCache;
                }
            }
            _prosviewRepositoryRootCache = '';
            return _prosviewRepositoryRootCache;
        }

        function prosviewTargetForRepositoryPath(path, line) {
            var clean = normalizeProsviewRepositoryPath(path);
            if (!clean) return null;
            if (paths.indexOf(clean) >= 0) return {kind: 'scene', path: clean, line: line};
            var node = (typeof repositoryFileByPath !== 'undefined' && repositoryFileByPath[clean])
                || (typeof repoFileByPath !== 'undefined' && repoFileByPath[clean]);
            if (!node) return null;
            if (node.is_scene && node.scene_path && paths.indexOf(node.scene_path) >= 0) {
                return {kind: 'scene', path: node.scene_path, line: line};
            }
            return {kind: 'file', path: node.path, line: line};
        }

        function currentProsviewRepositoryPath() {
            if (document.documentElement.dataset.view === 'file') {
                var title = document.getElementById('filePreviewTitle');
                return title ? String(title.textContent || '') : '';
            }
            if (document.documentElement.dataset.view !== 'scene' || curIdx < 0 || !paths[curIdx]) return '';
            var scenePath = paths[curIdx];
            var keys = Object.keys(typeof repositoryFileByPath === 'undefined' ? {} : repositoryFileByPath);
            for (var i = 0; i < keys.length; i++) {
                var node = repositoryFileByPath[keys[i]];
                if (node && node.is_scene && node.scene_path === scenePath) return node.path;
            }
            return scenePath;
        }

        function resolveProsviewFileReference(value) {
            var raw = String(value || '').trim();
            if (!raw) return null;
            try { raw = decodeURIComponent(raw); } catch(e) {}
            if (/^file:\/\//i.test(raw)) {
                try {
                    var fileUrl = new URL(raw);
                    if (fileUrl.protocol !== 'file:' || (fileUrl.host && fileUrl.host !== 'localhost')) return null;
                    raw = fileUrl.pathname;
                } catch(e) { return null; }
            } else if (/^[a-z][a-z0-9+.-]*:/i.test(raw)) {
                return null;
            }

            var line = null;
            var hashLine = raw.match(/#L(\d+)(?:C\d+)?$/i);
            if (hashLine) {
                line = parseInt(hashLine[1], 10);
                raw = raw.slice(0, hashLine.index);
            } else {
                var suffixLine = raw.match(/:(\d+)(?::\d+)?$/);
                if (suffixLine) {
                    line = parseInt(suffixLine[1], 10);
                    raw = raw.slice(0, suffixLine.index);
                }
            }
            if (!Number.isInteger(line) || line < 1) line = null;
            raw = raw.replace(/\\/g, '/');

            if (raw.charAt(0) === '/') {
                var sceneKeys = Object.keys(meta || {});
                for (var i = 0; i < sceneKeys.length; i++) {
                    var scenePath = sceneKeys[i];
                    if (String((meta[scenePath] || {}).abs_path || '').replace(/\\/g, '/') === raw) {
                        return {kind: 'scene', path: scenePath, line: line};
                    }
                }
                var fileKeys = Object.keys(typeof repoFileByPath === 'undefined' ? {} : repoFileByPath);
                for (var j = 0; j < fileKeys.length; j++) {
                    var fileNode = repoFileByPath[fileKeys[j]];
                    if (String((fileNode || {}).abs_path || '').replace(/\\/g, '/') === raw) {
                        return prosviewTargetForRepositoryPath(fileNode.path, line);
                    }
                }
                var root = prosviewRepositoryRoot();
                if (!root || (raw !== root && !raw.startsWith(root + '/'))) return null;
                return prosviewTargetForRepositoryPath(raw.slice(root.length + 1), line);
            }

            var direct = prosviewTargetForRepositoryPath(raw, line);
            if (direct) return direct;
            var current = currentProsviewRepositoryPath();
            if (!current) return null;
            var slash = current.lastIndexOf('/');
            var relative = (slash >= 0 ? current.slice(0, slash + 1) : '') + raw;
            return prosviewTargetForRepositoryPath(relative, line);
        }

        function focusProsviewSourceLine(line) {
            if (!Number.isInteger(line) || line < 1) return;
            var blocks = Array.prototype.slice.call(document.querySelectorAll('#sceneProseHost .ProseMirror > [data-line]'));
            if (!blocks.length) return;
            var target = null;
            for (var i = 0; i < blocks.length; i++) {
                var blockLine = parseInt(blocks[i].getAttribute('data-line') || '', 10);
                if (!Number.isInteger(blockLine)) continue;
                if (blockLine === line) { target = blocks[i]; break; }
                if (blockLine < line) target = blocks[i];
                else if (!target) { target = blocks[i]; break; }
            }
            if (target && typeof _flashAndScrollTo === 'function') _flashAndScrollTo(target);
        }

        function openProsviewFileReference(target) {
            if (!target) return;
            var currentScene = document.documentElement.dataset.view === 'scene' && curIdx >= 0 ? paths[curIdx] : '';
            var sameScene = target.kind === 'scene' && target.path === currentScene;
            if (_pmEditMode && _pmDirty && !sameScene) {
                var warning = 'Save or cancel your scene edits before opening another file.';
                document.getElementById('discussAnnouncement').textContent = warning;
                renderDiscussError(warning);
                return;
            }
            if (target.kind === 'scene') {
                if (!sameScene && typeof openSceneModal === 'function') openSceneModal(target.path);
                window.setTimeout(function() { focusProsviewSourceLine(target.line); }, 0);
                document.getElementById('discussAnnouncement').textContent = 'Opened ' + target.path + (target.line ? ' at line ' + target.line : '');
                return;
            }
            if (typeof closeSceneModal === 'function' && document.documentElement.dataset.view === 'scene') closeSceneModal();
            if (typeof previewRepoFile === 'function') previewRepoFile(target.path, {focus: true});
            document.getElementById('discussAnnouncement').textContent = 'Opened ' + target.path + ' in Prosview';
        }

        function safeDiscussUrl(value) {
            try {
                if (!/^(?:https?:\/\/|mailto:)/i.test(String(value || '').trim())) return null;
                var parsed = new URL(value);
                return ['http:', 'https:', 'mailto:'].indexOf(parsed.protocol) >= 0 ? value : null;
            } catch(e) { return null; }
        }

        // ── Images ──────────────────────────────────────────────────────────
        //
        // `imagesConfig.mode` is 'all' | 'local' | 'off'. Remote images inside
        // agent output are gated separately: in Discuss the model chooses the
        // URL, so loading it would report the reader's IP and the fact that the
        // document was opened to a host neither of us picked.

        function repoAssetUrl(src, basePath) {
            // Resolve a Markdown src against the document that referenced it and
            // return a /repo-asset/ URL, or null if it escapes the repository.
            var raw = String(src || '').trim();
            if (!raw) return null;
            var parts = [];
            if (raw.charAt(0) !== '/') {
                var baseDir = String(basePath || '').split('/');
                baseDir.pop();
                parts = baseDir;
            }
            // A `..` with nothing left to pop is an escape attempt, not a
            // no-op. Array.pop() on an empty array returns undefined silently,
            // which would quietly rewrite ../../../../etc/passwd into a
            // different in-repo path instead of refusing it.
            var escaped = false;
            raw.split('/').forEach(function(segment) {
                if (!segment || segment === '.') return;
                if (segment === '..') {
                    if (!parts.length) escaped = true;
                    else parts.pop();
                    return;
                }
                parts.push(segment);
            });
            if (escaped || !parts.length) return null;
            return '/repo-asset/' + parts.map(encodeURIComponent).join('/');
        }

        function resolveImageSrc(src, ctx) {
            var mode = (typeof imagesConfig === 'object' && imagesConfig ? imagesConfig.mode : 'all');
            if (mode === 'off') return null;
            var raw = String(src || '').trim();

            if (/^data:image\//i.test(raw)) return raw;   // inline bytes reach nobody
            if (/^(?:https?:)?\/\//i.test(raw)) {
                if (mode !== 'all') return null;
                if (ctx && ctx.agentOutput
                    && !(imagesConfig && imagesConfig.remoteInAgentOutput)) return null;
                return safeDiscussUrl(raw);
            }
            return repoAssetUrl(raw, ctx && ctx.basePath);
        }

        function imageNode(src, alt, title, ctx) {
            var resolved = resolveImageSrc(src, ctx);
            if (!resolved) {
                var placeholder = document.createElement('span');
                placeholder.className = 'md-image-placeholder';
                placeholder.textContent = decodeMarkdownText(alt || title || 'image');
                placeholder.title = 'Image not shown: ' + String(src || '');
                return placeholder;
            }
            var img = document.createElement('img');
            img.className = 'md-image';
            img.src = resolved;
            img.alt = decodeMarkdownText(alt || '');
            if (title) img.title = decodeMarkdownText(title);
            img.loading = 'lazy';
            // A referrer would leak the local URL, and the page it came from, to
            // any remote host.
            img.referrerPolicy = 'no-referrer';
            return img;
        }

        //: Attributes copied off a raw <img> tag. Everything else -- onerror and
        //: friends especially -- is dropped, so the tag cannot carry script.
        var IMG_ATTR_ALLOWLIST = ['src', 'alt', 'title', 'width', 'height'];

        function rawImgNode(html, ctx) {
            // Parse a literal <img> tag out of an `html` token without ever
            // handing the markup to innerHTML.
            if (ctx && ctx.allowRawImages === false) return null;
            var match = /^\s*<img\b([^>]*)>\s*$/i.exec(String(html || ''));
            if (!match) return null;
            var attrs = {};
            var pattern = /([a-zA-Z-]+)\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))/g;
            var found;
            while ((found = pattern.exec(match[1])) !== null) {
                var name = found[1].toLowerCase();
                if (IMG_ATTR_ALLOWLIST.indexOf(name) < 0) continue;
                attrs[name] = found[3] !== undefined ? found[3]
                    : found[4] !== undefined ? found[4] : found[5];
            }
            if (!attrs.src) return null;
            var node = imageNode(attrs.src, attrs.alt, attrs.title, ctx);
            if (node.tagName === 'IMG') {
                if (attrs.width && /^\d+$/.test(attrs.width)) node.width = parseInt(attrs.width, 10);
                if (attrs.height && /^\d+$/.test(attrs.height)) node.height = parseInt(attrs.height, 10);
            }
            return node;
        }

        function decodeMarkdownText(value) {
            var named = {amp: '&', lt: '<', gt: '>', quot: '"', apos: "'"};
            var decoded = String(value || '');
            // Marked escapes text tokens for HTML output. This renderer uses
            // text nodes instead, so reverse that one encoding layer.
            for (var pass = 0; pass < 1; pass += 1) {
                var next = decoded.replace(/&(?:#(\d+)|#x([0-9a-f]+)|(amp|lt|gt|quot|apos));/gi, function(match, decimal, hex, name) {
                    var code = decimal ? Number(decimal) : (hex ? parseInt(hex, 16) : null);
                    if (code !== null) {
                        if (!Number.isInteger(code) || code < 1 || code > 0x10ffff || (code >= 0xd800 && code <= 0xdfff)) return '\ufffd';
                        return String.fromCodePoint(code);
                    }
                    return named[String(name || '').toLowerCase()] || match;
                });
                if (next === decoded) break;
                decoded = next;
            }
            return decoded;
        }

        function appendMarkdownTokens(parent, tokens, ctx) {
            (tokens || []).forEach(function(token) {
                var node;
                if (token.type === 'space') return;
                if (token.type === 'text' || token.type === 'escape') {
                    if (token.tokens) appendMarkdownTokens(parent, token.tokens, ctx);
                    else parent.appendChild(document.createTextNode(decodeMarkdownText(token.text || token.raw || '')));
                    return;
                }
                if (token.type === 'html') {
                    var rawImg = rawImgNode(token.raw || token.text || '', ctx);
                    if (rawImg) { parent.appendChild(rawImg); return; }
                    parent.appendChild(document.createTextNode(token.raw || token.text || ''));
                    return;
                }
                if (token.type === 'paragraph') node = document.createElement('p');
                else if (token.type === 'heading') node = document.createElement('h' + Math.min(6, Math.max(1, token.depth || 3)));
                else if (token.type === 'strong') node = document.createElement('strong');
                else if (token.type === 'em') node = document.createElement('em');
                else if (token.type === 'codespan') { node = document.createElement('code'); node.textContent = decodeMarkdownText(token.text || ''); }
                else if (token.type === 'code') { node = document.createElement('pre'); var code = document.createElement('code'); code.textContent = decodeMarkdownText(token.text || ''); node.appendChild(code); }
                else if (token.type === 'blockquote') node = document.createElement('blockquote');
                else if (token.type === 'list') node = document.createElement(token.ordered ? 'ol' : 'ul');
                else if (token.type === 'list_item') node = document.createElement('li');
                else if (token.type === 'link') {
                    let localTarget = resolveProsviewFileReference(token.href || '');
                    var href = localTarget ? null : safeDiscussUrl(token.href || '');
                    node = (localTarget || href) ? document.createElement('a') : document.createElement('span');
                    if (localTarget) {
                        node.href = '#/' + localTarget.kind + '/' + encodeURIComponent(localTarget.path);
                        node.dataset.prosviewKind = localTarget.kind;
                        node.dataset.prosviewPath = localTarget.path;
                        if (localTarget.line) node.dataset.prosviewLine = String(localTarget.line);
                        var titleLine = localTarget.kind === 'scene' ? localTarget.line : null;
                        node.title = 'Open in Prosview' + (titleLine ? ' at line ' + titleLine : '');
                        node.onclick = function(event) {
                            if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                            event.preventDefault();
                            openProsviewFileReference(localTarget);
                        };
                    } else if (href) {
                        node.href = href;
                        node.rel = 'noopener noreferrer';
                        if (node.protocol !== 'mailto:') node.target = '_blank';
                    }
                } else if (token.type === 'br') node = document.createElement('br');
                else if (token.type === 'hr') node = document.createElement('hr');
                else if (token.type === 'image') {
                    parent.appendChild(imageNode(token.href, token.text, token.title, ctx));
                    return;
                }
                else if (token.type === 'del') node = document.createElement('del');
                else if (token.type === 'table') {
                    // Built cell by cell rather than via innerHTML: this same
                    // renderer draws agent output in Discuss, so every node here
                    // has to be safe for untrusted text.
                    node = document.createElement('table');
                    var align = token.align || [];
                    var thead = document.createElement('thead');
                    var headRow = document.createElement('tr');
                    (token.header || []).forEach(function(cell, i) {
                        var th = document.createElement('th');
                        if (align[i]) th.style.textAlign = align[i];
                        appendMarkdownTokens(th, cell.tokens || [], ctx);
                        headRow.appendChild(th);
                    });
                    thead.appendChild(headRow);
                    node.appendChild(thead);
                    var tbody = document.createElement('tbody');
                    (token.rows || []).forEach(function(row) {
                        var tr = document.createElement('tr');
                        (row || []).forEach(function(cell, i) {
                            var td = document.createElement('td');
                            if (align[i]) td.style.textAlign = align[i];
                            appendMarkdownTokens(td, cell.tokens || [], ctx);
                            tr.appendChild(td);
                        });
                        tbody.appendChild(tr);
                    });
                    node.appendChild(tbody);
                    // Wrap so a wide table scrolls itself instead of the page.
                    var scroller = document.createElement('div');
                    scroller.className = 'md-table-scroll';
                    scroller.appendChild(node);
                    parent.appendChild(scroller);
                    return;
                }
                else { parent.appendChild(document.createTextNode(token.raw || token.text || '')); return; }
                if (token.type === 'list') {
                    (token.items || []).forEach(function(item) { appendMarkdownTokens(node, [item], ctx); });
                } else if (token.type !== 'code' && token.type !== 'codespan'
                           && token.type !== 'br' && token.type !== 'hr') {
                    if (token.tokens) appendMarkdownTokens(node, token.tokens, ctx);
                    else if (token.text) node.textContent = decodeMarkdownText(token.text);
                }
                parent.appendChild(node);
            });
        }

        function renderSafeMarkdown(parent, text, ctx) {
            try { appendMarkdownTokens(parent, marked.lexer(String(text || ''), {gfm: true}), ctx || {}); }
            catch(e) { parent.textContent = String(text || ''); }
        }

        function renderDiscussMarkdown(parent, text) {
            // Agent output: relative paths have no document to resolve against,
            // and remote ones obey images.remote_in_agent_output.
            renderSafeMarkdown(parent, text, {agentOutput: true});
        }

        function elementWith(className, text) {
            var node = document.createElement('div');
            node.className = className;
            if (text !== undefined) node.textContent = text;
            return node;
        }

        // One card. Its title and explanation come from the skill file when the
        // server has answered with them, and from the fallback row when it has
        // not. Both go in as text nodes: a description is writer-authored.
        function discussPassGroup(rows, run) {
            var group = elementWith('discuss-story-actions');
            rows.forEach(function(row) {
                var live = _discussActionCopy[row.id] || {};
                var card = document.createElement('button');
                card.type = 'button'; card.className = 'discuss-story-action';
                card.appendChild(elementWith('discuss-story-action-title', live.label || row.label));
                card.appendChild(elementWith('discuss-story-action-copy', live.description || row.copy));
                card.onclick = function() { run(row.id); };
                group.appendChild(card);
            });
            return group;
        }

        function discussIsAtBottom(log) {
            return log.scrollHeight - log.scrollTop - log.clientHeight <= 1;
        }

        function captureDiscussScroll(log) {
            return {atBottom: discussIsAtBottom(log), scrollTop: log.scrollTop};
        }

        function restoreDiscussScroll(log, state) {
            var previousBehavior = log.style.scrollBehavior;
            log.style.scrollBehavior = 'auto';
            log.scrollTop = state.atBottom ? log.scrollHeight : state.scrollTop;
            requestAnimationFrame(function() { log.style.scrollBehavior = previousBehavior; });
            document.getElementById('discussNewActivity').hidden = state.atBottom;
        }

        function renderDiscussNotice(notice) {
            var node = elementWith('discuss-notice ' + (notice.kind === 'error' ? 'error' : 'warning'));
            if (notice.id) node.dataset.noticeId = notice.id;
            node.appendChild(elementWith('discuss-notice-message', notice.message || ''));
            if (notice.id) {
                var dismiss = document.createElement('button');
                dismiss.type = 'button';
                dismiss.className = 'discuss-notice-dismiss';
                dismiss.textContent = '×';
                dismiss.title = 'Dismiss';
                dismiss.setAttribute('aria-label', 'Dismiss notice');
                dismiss.onclick = function() {
                    dismiss.disabled = true;
                    discussApi(
                        '/api/discuss/conversations/' + encodeURIComponent(_discussConversationId)
                        + '/notices/' + encodeURIComponent(notice.id) + '/dismiss',
                        {}
                    ).then(function() {
                        if (_discussSnapshot) {
                            _discussSnapshot.notices = (_discussSnapshot.notices || []).filter(function(candidate) {
                                return candidate.id !== notice.id;
                            });
                        }
                        renderDiscussSnapshot();
                        document.getElementById('discussLog').focus();
                        document.getElementById('discussAnnouncement').textContent = 'Notice dismissed';
                    }).catch(function(error) {
                        dismiss.disabled = false;
                        renderDiscussError(error.message);
                    });
                };
                node.appendChild(dismiss);
            }
            return node;
        }

        // ---- Turn status -------------------------------------------------
        // One strip answers "is it still working?". Everything it says is
        // derived from the snapshot, so a reload or a reconnect cannot leave it
        // claiming work that already finished.
        var _discussTurnBase = {ms: null, at: 0};
        var _discussTurnTimer = null;
        var _discussTurnTrailOpen = false;
        var _discussTurnLastKind = '';
        var _discussTurnPhraseAt = 0;
        var _discussTurnPhrase = '';
        var _discussTurnDoneUntil = 0;
        var _discussAgentState = {};
        var _discussBaseTitle = '';

        function discussFormatDuration(ms) {
            var total = Math.max(0, Math.round((ms || 0) / 1000));
            return Math.floor(total / 60) + ':' + String(total % 60).padStart(2, '0');
        }

        function discussSpokenDuration(ms) {
            var total = Math.max(0, Math.round((ms || 0) / 1000));
            var minutes = Math.floor(total / 60);
            var seconds = total % 60;
            var parts = [];
            if (minutes) parts.push(minutes + ' minute' + (minutes === 1 ? '' : 's'));
            if (seconds || !minutes) parts.push(seconds + ' second' + (seconds === 1 ? '' : 's'));
            return parts.join(' ');
        }

        function discussShortPath(value) {
            var parts = String(value || '').replace(/\\/g, '/').split('/').filter(Boolean);
            if (!parts.length) return '';
            return parts.slice(-2).join('/');
        }

        // Protocol nouns are not writer language. "commandExecution · failed"
        // says nothing about what was tried, and a search that matched nothing
        // is an outcome rather than an error.
        function discussCommandPhrase(activity) {
            var command = String(activity.command || '').trim();
            var head = command.split(/\s+/)[0] || '';
            var name = head.split('/').pop();
            var quoted = command.match(/(["'])(.*?)\1/);
            var term = quoted ? quoted[2] : '';
            if (name === 'grep' || name === 'rg' || name === 'ugrep') {
                return term ? 'Searching for “' + term + '”' : 'Searching the manuscript';
            }
            if (name === 'cat' || name === 'head' || name === 'tail' || name === 'sed') {
                var target = command.split(/\s+/).filter(function(part) { return part.indexOf('-') !== 0; }).pop();
                return target && target !== name ? 'Reading ' + discussShortPath(target) : 'Reading a file';
            }
            if (name === 'ls' || name === 'find' || name === 'fd') return 'Looking through files';
            if (name === 'git') return 'Checking the repository';
            return command ? 'Running ' + name : 'Running a command';
        }

        function discussActivityPhrase(activity) {
            var kind = activity.kind || '';
            var tool = String(activity.tool || '');
            if (kind === 'commandExecution') return discussCommandPhrase(activity);
            if (kind === 'fileChange') {
                var changes = activity.changes || [];
                var path = changes.length ? discussShortPath(changes[0].path) : '';
                return path ? 'Editing ' + path : 'Editing a file';
            }
            if (kind === 'webSearch') {
                return activity.query ? 'Searching the web for “' + activity.query + '”' : 'Searching the web';
            }
            if (tool === 'Read' || tool === 'NotebookRead') {
                return activity.path ? 'Reading ' + discussShortPath(activity.path) : 'Reading a file';
            }
            if (tool === 'Grep') {
                return activity.query ? 'Searching for “' + activity.query + '”' : 'Searching the manuscript';
            }
            if (tool === 'Glob') return 'Looking through files';
            if (tool) return 'Using ' + tool;
            if (kind === 'mcpToolCall') return 'Using a connected tool';
            return 'Working';
        }

        function discussApprovalPhrase(approval) {
            if (approval.kind === 'command' || approval.kind === 'network') return 'Wants to run a shell command';
            if (approval.kind === 'fileChange') return 'Wants to edit a file';
            if (approval.kind === 'permissions') return 'Wants wider permissions';
            return 'Wants your decision';
        }

        function discussSettledApprovalPhrase(approval) {
            var what = approval.kind === 'fileChange' ? 'an edit'
                : approval.kind === 'permissions' ? 'wider permissions'
                : 'a command';
            if (approval.status === 'expired') return 'The request for ' + what + ' expired';
            if (approval.decision === 'decline' || approval.decision === 'cancel') return 'You declined ' + what;
            return 'You allowed ' + what;
        }

        function discussTurnActivities(snapshot, turnId) {
            var rows = (snapshot.activities || []);
            if (!turnId) return rows;
            var scoped = rows.filter(function(row) { return (row.turn_id || '') === turnId; });
            // An agent that never labels its activities with a turn is still
            // reporting this turn's work: the projection is cleared when the
            // next turn starts.
            return scoped.length ? scoped : rows;
        }

        function discussTurnTrailRows(snapshot, turnId, running) {
            var rows = discussTurnActivities(snapshot, turnId).map(function(activity) {
                var phrase = discussActivityPhrase(activity);
                var status = activity.status || '';
                if (status === 'inProgress') return {text: phrase, state: running ? 'now' : 'settled'};
                if (status === 'failed') {
                    return phrase.indexOf('Searching') === 0
                        ? {text: phrase + ' — no matches', state: 'empty'}
                        : {text: phrase + ' — failed', state: 'failed'};
                }
                return {text: phrase, state: 'done'};
            });
            (snapshot.approvals || []).forEach(function(approval) {
                if (approval.status === 'pending' || approval.status === 'resolving') return;
                if (turnId && approval.turn_id && approval.turn_id !== turnId) return;
                rows.push({text: discussSettledApprovalPhrase(approval), state: 'settled'});
            });
            return rows;
        }

        // The last thing the agent said it was doing. Activities beat progress:
        // "Reading alice.md" is a fact, a reasoning summary is a paraphrase.
        function discussTurnDoingLine(snapshot) {
            var scoped = discussTurnActivities(snapshot, snapshot.active_turn_id);
            var running = scoped.filter(function(row) { return row.status === 'inProgress'; });
            if (running.length) return discussActivityPhrase(running[running.length - 1]);
            var tail = (snapshot.progress || []).join('').split('\n').filter(function(line) {
                return line.trim();
            }).pop();
            if (tail) return tail.trim().slice(0, 160);
            if (scoped.length) return discussActivityPhrase(scoped[scoped.length - 1]);
            return '';
        }

        function discussTurnModel(snapshot) {
            var label = discussAgentLabel();
            var pending = (snapshot.approvals || []).filter(function(row) { return row.status === 'pending'; });
            if (pending.length) {
                return {
                    kind: 'waiting',
                    state: label + ' needs your permission',
                    doing: discussApprovalPhrase(pending[0]),
                    action: {label: 'Review', run: function() { discussFocusApproval(pending[0].request_id); }}
                };
            }
            if (snapshot.active_turn_id) {
                var queued = (snapshot.queue || []).length;
                return {
                    kind: 'working',
                    state: label + ' is working' + (queued ? ' · ' + queued + ' queued' : ''),
                    doing: discussTurnDoingLine(snapshot)
                };
            }
            if (snapshot.active_request_id) {
                return {
                    kind: 'starting',
                    state: 'Starting ' + label + '…',
                    doing: 'Waiting for the local agent to accept the question'
                };
            }
            var last = snapshot.last_turn || {};
            if (last.status) {
                var duration = discussFormatDuration(last.duration_ms);
                if (last.status === 'completed') {
                    var steps = last.steps || 0;
                    return {
                        kind: 'done',
                        state: 'Answered in ' + duration,
                        doing: steps ? steps + ' step' + (steps === 1 ? '' : 's') : ''
                    };
                }
                if (last.status === 'interrupted' || last.status === 'cancelled') {
                    return {kind: 'failed', state: 'Stopped after ' + duration, doing: 'You stopped ' + label};
                }
                return {
                    kind: 'failed',
                    state: label + ' could not finish',
                    doing: last.error ? String(last.error).split('\n')[0].slice(0, 160) : 'The turn ended without an answer'
                };
            }
            return {kind: 'idle'};
        }

        function discussFocusApproval(requestId) {
            var card = document.getElementById('discussLog')
                .querySelector('[data-approval-id="' + CSS.escape(String(requestId || '')) + '"]');
            if (!card) return;
            card.scrollIntoView({block: 'center'});
            var button = card.querySelector('button');
            if (button) button.focus();
        }

        function toggleDiscussTurnTrail() {
            _discussTurnTrailOpen = !_discussTurnTrailOpen;
            if (_discussSnapshot) renderDiscussTurnStatus(_discussSnapshot);
        }

        function discussTurnElapsedMs() {
            if (_discussTurnBase.ms === null) return null;
            return _discussTurnBase.ms + (Date.now() - _discussTurnBase.at);
        }

        function updateDiscussTurnClock() {
            var elapsed = discussTurnElapsedMs();
            var clock = document.getElementById('discussTurnClock');
            if (elapsed === null) { clock.hidden = true; return; }
            clock.hidden = false;
            clock.textContent = discussFormatDuration(elapsed);
            // Silence is a fact worth reporting. A turn that has produced
            // nothing for a minute may be thinking or may be wedged, and
            // pretending it is fine is how a panel loses its writer's trust.
            if (_discussTurnLastKind === 'working' && Date.now() - _discussTurnPhraseAt > 60000) {
                var quiet = Math.floor((Date.now() - _discussTurnPhraseAt) / 60000);
                document.getElementById('discussTurnDoing').textContent =
                    'Still working — no output for ' + quiet + 'm';
            }
        }

        function _syncDiscussTurnTimer(running) {
            if (running && !_discussTurnTimer) {
                _discussTurnTimer = setInterval(updateDiscussTurnClock, 1000);
            } else if (!running && _discussTurnTimer) {
                clearInterval(_discussTurnTimer);
                _discussTurnTimer = null;
            }
        }

        function renderDiscussTurnStatus(snapshot) {
            var wrap = document.getElementById('discussTurnStatus');
            if (!wrap) return;
            var model = discussTurnModel(snapshot);
            var kind = model.kind;
            // A finished turn holds its result briefly, then gets out of the way.
            if (kind === 'done') {
                if (_discussTurnLastKind !== 'done') _discussTurnDoneUntil = Date.now() + DISCUSS_TURN_DONE_MS;
                if (Date.now() > _discussTurnDoneUntil) kind = 'idle';
            } else if (kind !== 'idle') {
                _discussTurnDoneUntil = 0;
            }
            if (kind !== _discussTurnLastKind) {
                if (kind === 'done' && (_discussTurnLastKind === 'working' || _discussTurnLastKind === 'starting')) {
                    document.getElementById('discussAnnouncement').textContent =
                        discussAgentLabel() + ' finished in ' + discussSpokenDuration((snapshot.last_turn || {}).duration_ms);
                    setTimeout(function() {
                        if (_discussSnapshot) renderDiscussTurnStatus(_discussSnapshot);
                    }, DISCUSS_TURN_DONE_MS + 200);
                }
                if (kind === 'working' || kind === 'starting') _discussTurnTrailOpen = true;
                if (kind === 'idle' || kind === 'done') _discussTurnTrailOpen = false;
                _discussTurnLastKind = kind;
            }

            _discussAgentState[_discussAgent] = kind === 'waiting' ? 'attention'
                : (kind === 'working' || kind === 'starting') ? 'busy' : '';
            _syncDiscussAmbientSignals();

            if (kind === 'idle') {
                wrap.hidden = true;
                wrap.dataset.state = 'idle';
                _syncDiscussTurnTimer(false);
                return;
            }
            wrap.hidden = false;
            wrap.dataset.state = kind;

            document.getElementById('discussTurnState').textContent = model.state || '';
            var doing = model.doing || '';
            if (doing !== _discussTurnPhrase) { _discussTurnPhrase = doing; _discussTurnPhraseAt = Date.now(); }
            document.getElementById('discussTurnDoing').textContent = doing;

            var running = kind === 'working' || kind === 'starting';
            _discussTurnBase = running
                ? {ms: snapshot.active_turn_elapsed_ms || 0, at: Date.now()}
                : {ms: null, at: 0};
            _syncDiscussTurnTimer(running);
            updateDiscussTurnClock();

            var action = document.getElementById('discussTurnAction');
            if (model.action) {
                action.hidden = false;
                action.textContent = model.action.label;
                action.onclick = model.action.run;
            } else {
                action.hidden = true;
                action.onclick = null;
            }

            var planHost = document.getElementById('discussTurnPlan');
            planHost.replaceChildren();
            planHost.hidden = !(snapshot.plan || []).length;
            if (!planHost.hidden) {
                var plan = document.createElement('details');
                plan.className = 'discuss-plan';
                plan.open = true;
                var planSummary = document.createElement('summary');
                planSummary.textContent = 'Plan';
                plan.appendChild(planSummary);
                var list = document.createElement('ol');
                snapshot.plan.forEach(function(row) {
                    var item = document.createElement('li');
                    item.className = row.status || '';
                    item.textContent = row.step || '';
                    list.appendChild(item);
                });
                plan.appendChild(list);
                planHost.appendChild(plan);
            }

            var rows = discussTurnTrailRows(
                snapshot, snapshot.active_turn_id || (snapshot.last_turn || {}).turn_id, running);
            var toggle = document.getElementById('discussTurnTrailToggle');
            var trail = document.getElementById('discussTurnTrail');
            toggle.hidden = !rows.length;
            toggle.textContent = _discussTurnTrailOpen
                ? 'Hide'
                : (running ? 'Details' : rows.length + ' step' + (rows.length === 1 ? '' : 's'));
            toggle.setAttribute('aria-expanded', _discussTurnTrailOpen ? 'true' : 'false');
            trail.hidden = !(_discussTurnTrailOpen && rows.length);
            wrap.dataset.trail = trail.hidden ? 'closed' : 'open';
            trail.replaceChildren();
            rows.forEach(function(row) {
                var item = document.createElement('li');
                item.className = row.state;
                var text = document.createElement('span');
                text.textContent = row.text;
                item.appendChild(text);
                trail.appendChild(item);
            });
        }

        // ---- Signals outside the panel ------------------------------------
        // You are usually reading, not watching the dock. The tab dot and the
        // document title are the only things that reach you from there.
        function _syncDiscussAmbientSignals() {
            DISCUSS_AGENTS.forEach(function(agent) {
                _markDiscussAgentTab(agent, _discussAgentState[agent] || '');
            });
            if (!_discussBaseTitle) _discussBaseTitle = document.title;
            var panel = document.getElementById('discussPanel');
            var away = !panel || panel.hidden || !document.hasFocus();
            var waiting = DISCUSS_AGENTS.filter(function(a) { return _discussAgentState[a] === 'attention'; });
            var working = DISCUSS_AGENTS.filter(function(a) { return _discussAgentState[a] === 'busy'; });
            var prefix = '';
            if (away && waiting.length) prefix = '· ' + discussAgentLabel(waiting[0]) + ' needs you — ';
            else if (away && working.length) prefix = '· ' + discussAgentLabel(working[0]) + ' working — ';
            var next = prefix + _discussBaseTitle;
            if (document.title !== next) document.title = next;
        }

        function renderDiscussSnapshot() {
            var snapshot = _discussSnapshot;
            if (!snapshot) return;
            setDiscussConnection(snapshot.connection || 'Live', snapshot.unavailable_reason || '');
            var log = document.getElementById('discussLog');
            var scrollState = captureDiscussScroll(log);
            log.replaceChildren();
            var notices = snapshot.notices || [];
            var renderedNotices = Object.create(null);
            function appendNoticesForRequest(requestId) {
                if (!requestId) return;
                notices.forEach(function(notice, index) {
                    if (!renderedNotices[index] && notice.client_request_id === requestId) {
                        log.appendChild(renderDiscussNotice(notice));
                        renderedNotices[index] = true;
                    }
                });
            }
            function appendUnassociatedNotices() {
                notices.forEach(function(notice, index) {
                    if (!renderedNotices[index] && !notice.client_request_id) {
                        log.appendChild(renderDiscussNotice(notice));
                        renderedNotices[index] = true;
                    }
                });
            }
            var hasNoDiscussActivity = !(snapshot.messages || []).length
                && !(snapshot.progress || []).length
                && !(snapshot.tasks || []).length;
            if (hasNoDiscussActivity) {
                var empty = elementWith('discuss-empty');
                var title = document.createElement('strong');
                if (_discussRepositoryAction) {
                    var scanStarting = document.getElementById('discussSend').disabled;
                    if (scanStarting) {
                        title.textContent = _discussRepositoryAction === 'scene_continuity'
                            ? 'Starting continuity scan…'
                            : 'Starting canon scan…';
                        empty.appendChild(title);
                        empty.appendChild(document.createTextNode('Gathering the configured story evidence. This can take a moment.'));
                    } else if (_discussRepositoryAction === 'scene_continuity') {
                        title.textContent = 'Ready to scan this scene';
                        empty.appendChild(title);
                        empty.appendChild(document.createTextNode('Add an optional focus below, or scan the active scene as-is.'));
                        var readyActions = elementWith('discuss-story-actions');
                        var scanNow = document.createElement('button'); scanNow.type = 'button'; scanNow.className = 'discuss-primary'; scanNow.textContent = 'Scan scene now';
                        scanNow.onclick = function() { runDiscussRepositoryAction(); };
                        readyActions.appendChild(scanNow); empty.appendChild(readyActions);
                    } else {
                        title.textContent = 'Ready to trace a canon change';
                        empty.appendChild(title);
                        empty.appendChild(document.createTextNode('Describe the old and new fact below, then scan for consequences.'));
                    }
                } else {
                    title.textContent = 'What do you want to examine?';
                    empty.appendChild(title);
                    empty.appendChild(document.createTextNode('Ask about what you are reading, or run a pass over it.'));
                    // The scene on screen comes first. The repository scans are
                    // the rarer, heavier work and no longer lead.
                    var emptyDocument = discussTurnDocument();
                    if (emptyDocument && emptyDocument.kind === 'scene') {
                        empty.appendChild(elementWith('discuss-story-group', 'Read this scene'));
                        empty.appendChild(discussPassGroup(DISCUSS_SCENE_PASSES, runDiscussScenePass));
                    }
                    empty.appendChild(elementWith('discuss-story-group', 'Across the story'));
                    empty.appendChild(discussPassGroup(DISCUSS_REPOSITORY_PASSES, startDiscussRepositoryAction));
                }
                log.appendChild(empty);
            }
            appendUnassociatedNotices();
            groupDiscussTasks(snapshot.tasks || []).forEach(function(group) {
                log.appendChild(renderDiscussTask(group.latest, group.previous));
                appendNoticesForRequest(group.latest.client_request_id);
            });
            (snapshot.messages || []).forEach(function(message) {
                var wrap = elementWith('discuss-message ' + (message.role === 'user' ? 'user' : 'assistant'));
                var label = elementWith('discuss-message-label', message.role === 'user' ? 'You' : discussAgentLabel());
                wrap.appendChild(label);
                if (message.role === 'assistant') renderDiscussMarkdown(wrap, message.text);
                else wrap.appendChild(document.createTextNode(message.text || ''));
                log.appendChild(wrap);
                if (message.role === 'user') appendNoticesForRequest(message.client_request_id);
            });
            // Words appearing are the strongest sign of life there is, and the
            // rebuild above would erase them 35ms after they arrived. Re-hang
            // the draft until the message it becomes has landed.
            if (_discussStreamText && snapshot.active_turn_id) {
                var landed = (snapshot.messages || []).some(function(message) {
                    return message.role === 'assistant'
                        && String(message.text || '').indexOf(_discussStreamText.slice(0, 200)) >= 0;
                });
                if (landed) _discussStreamText = '';
                else {
                    var draft = elementWith('discuss-message assistant discuss-stream-draft');
                    draft.appendChild(elementWith('discuss-message-label', discussAgentLabel()));
                    draft.appendChild(document.createTextNode(_discussStreamText));
                    log.appendChild(draft);
                }
            }
            // Progress and activities now belong to the turn strip, in the
            // order they happened. Only an approval that still needs a decision
            // earns a card here; a settled one is a line in the turn's trail.
            var pendingApprovals = {};
            (snapshot.approvals || []).forEach(function(approval) {
                if (approval.status === 'pending' || approval.status === 'resolving') {
                    pendingApprovals[approval.item_id] = true;
                    log.appendChild(renderDiscussApproval(approval));
                }
            });
            
            (snapshot.activities || []).forEach(function(activity) {
                if (activity.kind === 'fileChange' && activity.changes && activity.changes.length > 0 && activity.changes[0].diff && !pendingApprovals[activity.id]) {
                    log.appendChild(renderDiscussActivityCard(activity));
                }
            });
            notices.forEach(function(notice, index) {
                if (!renderedNotices[index]) log.appendChild(renderDiscussNotice(notice));
            });
            appendDiscussLocalError(log);
            if ((snapshot.queue || []).length) {
                var queueCard = elementWith('discuss-queue');
                var queueTitle = document.createElement('strong'); queueTitle.textContent = snapshot.queue.length + ' item' + (snapshot.queue.length === 1 ? '' : 's') + ' queued'; queueCard.appendChild(queueTitle);
                snapshot.queue.forEach(function(item) {
                    var row = elementWith('discuss-queue-item', item.label || 'Question');
                    var remove = document.createElement('button');
                    remove.type = 'button'; remove.className = 'discuss-queue-remove';
                    remove.textContent = 'Remove';
                    remove.setAttribute('aria-label', 'Remove ' + (item.label || 'question') + ' from queue');
                    remove.onclick = function() {
                        remove.disabled = true;
                        discussApi('/api/discuss/conversations/' + encodeURIComponent(_discussConversationId) + '/queue/' + encodeURIComponent(item.client_request_id) + '/cancel', {})
                            .then(function() {
                                document.getElementById('discussAnnouncement').textContent = (item.label || 'Question') + ' removed from queue';
                                scheduleDiscussSnapshot();
                            })
                            .catch(function(error) { remove.disabled = false; renderDiscussError(error.message); });
                    };
                    row.appendChild(remove); queueCard.appendChild(row);
                });
                log.appendChild(queueCard);
            }
            var stopButton = document.getElementById('discussStop');
            stopButton.hidden = !snapshot.active_turn_id;
            if (!snapshot.active_turn_id) {
                stopButton.disabled = false;
                stopButton.textContent = 'Stop ' + discussAgentLabel();
            }
            var clearResults = document.getElementById('discussHistoryClear');
            var hasClearableResults = (snapshot.tasks || []).some(function(task) {
                return task.status !== 'queued' && task.status !== 'running';
            });
            clearResults.hidden = !hasClearableResults;
            clearResults.disabled = !!(
                snapshot.active_turn_id
                || snapshot.active_request_id
                || (snapshot.queue || []).length
                || (snapshot.tasks || []).some(function(task) {
                    return task.status === 'queued' || task.status === 'running';
                })
            );
            clearResults.title = clearResults.disabled
                ? 'Wait for assistance to finish before clearing results'
                : 'Clear assistance results from this conversation';
            var pendingApproval = (snapshot.approvals || []).some(function(approval) { return approval.status === 'pending'; });
            var newConversation = document.getElementById('discussNewConversation');
            var newConversationHint = document.getElementById('discussNewConversationHint');
            var unavailableReason = '';
            if (snapshot.active_turn_id) unavailableReason = 'Stop ' + discussAgentLabel() + ' before starting a new conversation.';
            else if (snapshot.active_request_id) unavailableReason = 'Wait for ' + discussAgentLabel() + ' to start this question before starting a new conversation.';
            else if ((snapshot.queue || []).length) unavailableReason = 'Remove or wait for queued items before starting a new conversation.';
            else if (pendingApproval) unavailableReason = 'Resolve the ' + discussAgentLabel() + ' approval request before starting a new conversation.';
            newConversation.disabled = !!unavailableReason;
            newConversation.title = unavailableReason;
            if (newConversationHint.textContent !== unavailableReason) newConversationHint.textContent = unavailableReason;
            newConversationHint.hidden = !unavailableReason;
            log.setAttribute('aria-busy', snapshot.active_turn_id ? 'true' : 'false');
            renderDiscussModelChip();
            renderDiscussTurnStatus(snapshot);
            restoreDiscussScroll(log, scrollState);
            if (_discussLastApproval) {
                var target = log.querySelector('[data-approval-id="' + CSS.escape(_discussLastApproval) + '"] button');
                // An earlier scheduled snapshot may race the approval SSE
                // event. Keep the id until a snapshot renders its controls.
                if (target) {
                    target.focus();
                    document.getElementById('discussAnnouncement').textContent = discussAgentLabel() + ' is requesting approval';
                    _discussLastApproval = '';
                }
            }
        }

        function groupDiscussTasks(tasks) {
            var groups = [];
            var byRoot = Object.create(null);
            tasks.forEach(function(task) {
                var rootId = task.retry_root_id || task.id;
                var group = byRoot[rootId];
                if (!group) {
                    group = {attempts: []};
                    byRoot[rootId] = group;
                    groups.push(group);
                }
                group.attempts.push(task);
            });
            groups.forEach(function(group) {
                group.attempts.sort(function(left, right) {
                    var attemptDelta = Number(left.attempt || 1) - Number(right.attempt || 1);
                    return attemptDelta || Number(left.created_at || 0) - Number(right.created_at || 0);
                });
                group.latest = group.attempts[group.attempts.length - 1];
                group.previous = group.attempts.slice(0, -1);
            });
            return groups;
        }

        function discussTaskStatusLabel(status) {
            if (status === 'applied' || status === 'staged') return 'Applied · Not saved';
            if (status === 'saved') return 'Saved';
            if (status === 'reviewing') return 'Reviewing';
            if (status === 'ready') return 'Ready';
            var label = String(status || 'Unknown').replace(/_/g, ' ');
            return label.charAt(0).toUpperCase() + label.slice(1);
        }

        function renderDiscussAlternatives(task, result) {
            var fragment = document.createDocumentFragment();
            if (task.instruction) {
                fragment.appendChild(elementWith('discuss-task-instruction', 'Instruction · ' + task.instruction));
            }
            var alternatives = result.alternatives || [];
            var selected = Number.isInteger(task.selected_option) ? task.selected_option : -1;
            if (selected >= 0 && selected < alternatives.length) {
                var used = elementWith('discuss-task-used');
                used.appendChild(elementWith('discuss-task-used-label', 'Used suggestion ' + String(selected + 1)));
                used.appendChild(elementWith('discuss-alternative-text', alternatives[selected].text || ''));
                fragment.appendChild(used);
            }
            var details = document.createElement('details'); details.className = 'discuss-alternatives';
            var summary = document.createElement('summary');
            summary.textContent = 'View ' + alternatives.length + ' suggestion' + (alternatives.length === 1 ? '' : 's');
            details.appendChild(summary);
            alternatives.forEach(function(alternative, index) {
                var row = elementWith('discuss-alternative');
                var label = 'Suggestion ' + String(index + 1);
                if (index === selected) label += ' · Used';
                row.appendChild(elementWith('discuss-alternative-label', label));
                row.appendChild(elementWith('discuss-alternative-text', alternative.text || ''));
                if (alternative.rationale) row.appendChild(elementWith('discuss-alternative-rationale', alternative.rationale));
                details.appendChild(row);
            });
            fragment.appendChild(details);
            return fragment;
        }

        function discussAlternativesStateSummary(task, result) {
            var count = (result.alternatives || []).length;
            var prefix = count + ' suggestion' + (count === 1 ? '' : 's');
            if (task.status === 'applied' || task.status === 'staged') return prefix + ' · applied to draft, not saved';
            if (task.status === 'saved') return prefix + ' · saved to manuscript';
            return prefix + ' · manuscript unchanged';
        }

        function openContinuityFinding(finding) {
            var target = resolveProsviewFileReference(finding.file + '#L' + String(finding.line || 1));
            if (!target) {
                renderDiscussError('This scanned file is not available in the current Proseview sidebar.');
                return;
            }
            openProsviewFileReference(target);
        }

        function setContinuityFindingDecision(task, finding, decision, button) {
            if (button) button.disabled = true;
            discussApi(
                '/api/discuss/conversations/' + encodeURIComponent(_discussConversationId)
                + '/tasks/' + encodeURIComponent(task.id) + '/findings/' + encodeURIComponent(finding.id) + '/decision',
                {decision: decision}
            ).then(function() {
                finding.decision = decision;
                renderDiscussSnapshot();
                document.getElementById('discussAnnouncement').textContent = decision === 'intentional'
                    ? 'Reference marked intentional' : 'Continuity decision updated';
                scheduleDiscussSnapshot();
            }).catch(function(error) { if (button) button.disabled = false; renderDiscussError(error.message); });
        }

        function reviewContinuityFinding(task, finding, button) {
            if (button) button.disabled = true;
            discussApi(
                '/api/discuss/conversations/' + encodeURIComponent(_discussConversationId)
                + '/tasks/' + encodeURIComponent(task.id) + '/findings/' + encodeURIComponent(finding.id) + '/proposal',
                {client_id: (typeof aiClientId === 'function' ? aiClientId() : null)}
            ).then(function(data) {
                if (data.proposal && typeof aiFocusProposal === 'function') aiFocusProposal(data.proposal, true);
                document.getElementById('discussAnnouncement').textContent = 'Proposed edit opened for review. The manuscript is unchanged.';
                scheduleDiscussSnapshot();
            }).catch(function(error) { if (button) button.disabled = false; renderDiscussError(error.message); });
        }

        function renderContinuityReport(task, result) {
            var fragment = document.createDocumentFragment();
            fragment.appendChild(elementWith('discuss-task-summary', result.summary || 'Continuity scan complete.'));
            var scope = task.scope || {};
            var scopeCopy = String(scope.files_scanned || 0) + ' files · ' + Math.ceil(Number(scope.bytes_scanned || 0) / 1024) + ' KB';
            fragment.appendChild(elementWith('discuss-refactor-scope', '✓ Read-only scan complete · ' + scopeCopy + ' · no manuscript files changed'));
            if (Number(scope.files_omitted || 0) > 0) {
                fragment.appendChild(elementWith(
                    'discuss-queue',
                    discussAgentLabel() + ' input limit · scanned ' + String(scope.files_scanned || 0)
                    + ' of ' + String(scope.files_available || 0) + ' configured files; '
                    + String(scope.files_omitted) + ' files were omitted. Narrow repo_tab.folders for a complete scan.'
                ));
            }
            var scopeDetails = document.createElement('details'); scopeDetails.className = 'discuss-refactor-scope-details';
            var scopeSummary = document.createElement('summary'); scopeSummary.textContent = Number(scope.files_omitted || 0) > 0 ? 'Configured folders' : 'Scanned folders'; scopeDetails.appendChild(scopeSummary);
            var scopeList = document.createElement('ul');
            (scope.roots || []).forEach(function(root) { var item = document.createElement('li'); item.textContent = root; scopeList.appendChild(item); });
            scopeDetails.appendChild(scopeList); fragment.appendChild(scopeDetails);
            var groups = [
                ['direct', 'Direct contradictions'],
                ['judgment', 'Needs your judgment'],
                ['intentional', 'Likely intentional']
            ];
            var findings = result.findings || [];
            if (!findings.length) {
                fragment.appendChild(elementWith('discuss-refactor-clear', task.verify_of
                    ? 'No unexplained continuity findings remain in the scanned scope.'
                    : 'No supported continuity findings were found in the scanned scope.'));
            }
            groups.forEach(function(group) {
                var rows = findings.filter(function(finding) { return finding.category === group[0]; });
                if (!rows.length) return;
                var section = document.createElement('section'); section.className = 'discuss-refactor-group';
                var heading = document.createElement('h4'); heading.textContent = group[1] + ' · ' + String(rows.length); section.appendChild(heading);
                rows.forEach(function(finding) {
                    var row = elementWith('discuss-refactor-finding'); row.dataset.decision = finding.decision || 'open';
                    var source = document.createElement('button'); source.type = 'button'; source.className = 'discuss-refactor-source';
                    source.textContent = finding.file + '#L' + finding.line; source.onclick = function() { openContinuityFinding(finding); };
                    row.appendChild(source);
                    var quote = document.createElement('q'); quote.textContent = finding.quote || ''; row.appendChild(quote);
                    row.appendChild(elementWith('discuss-finding-detail', finding.explanation || ''));
                    if (finding.decision && finding.decision !== 'open') {
                        row.appendChild(elementWith('discuss-refactor-decision', 'Decision · ' + String(finding.decision).replace(/_/g, ' ')));
                    }
                    var actions = elementWith('discuss-refactor-finding-actions');
                    if (finding.replacement && finding.proposal_eligible && finding.category !== 'intentional') {
                        var review = document.createElement('button'); review.type = 'button'; review.className = 'discuss-primary'; review.textContent = 'Review proposed edit';
                        review.onclick = function() { reviewContinuityFinding(task, finding, review); }; actions.appendChild(review);
                    }
                    var intentional = document.createElement('button'); intentional.type = 'button'; intentional.className = 'discuss-secondary';
                    intentional.textContent = finding.decision === 'intentional' ? 'Mark unresolved' : 'Mark intentional';
                    intentional.onclick = function() {
                        setContinuityFindingDecision(task, finding, finding.decision === 'intentional' ? 'open' : 'intentional', intentional);
                    };
                    actions.appendChild(intentional); row.appendChild(actions); section.appendChild(row);
                });
                fragment.appendChild(section);
            });
            if (findings.length >= Number(scope.finding_limit || 50)) {
                fragment.appendChild(elementWith('discuss-error', 'The finding limit was reached; this report may not include every consequence.'));
            }
            if (task.action_id !== 'verify_refactor') {
                var verify = document.createElement('button'); verify.type = 'button'; verify.className = 'discuss-secondary discuss-refactor-verify';
                verify.textContent = 'Verify after edits'; verify.onclick = function() { runDiscussRepositoryAction('verify_refactor', task.id); };
                fragment.appendChild(verify);
            }
            return fragment;
        }

        function renderDiscussTask(task, previousAttempts) {
            var card = elementWith('discuss-task'); card.dataset.taskId = task.id;
            var heading = document.createElement('div'); heading.className = 'discuss-task-heading';
            var title = document.createElement('strong'); title.textContent = task.label || selectionActionLabel(task.action_id);
            var status = document.createElement('span'); status.className = 'discuss-task-status status-' + task.status; status.textContent = discussTaskStatusLabel(task.status);
            heading.appendChild(title); heading.appendChild(status); card.appendChild(heading);
            var target = task.target || {};
            if (task.kind === 'continuity_report') {
                card.appendChild(elementWith('discuss-task-selection', task.change_request || task.instruction || 'Repository continuity scan'));
            } else {
                if (target.scope === 'scene') {
                    // Quoting the first 120 characters of a whole scene back at
                    // its writer says nothing about what the pass read.
                    var words = String(target.selection || '').split(/\s+/).filter(Boolean).length;
                    card.appendChild(elementWith('discuss-task-selection', 'This scene · ' + words.toLocaleString() + ' words'));
                } else {
                    var preview = elementWith('discuss-task-selection', '“' + String(target.selection || '').slice(0, 120) + (String(target.selection || '').length > 120 ? '…' : '') + '”');
                    card.appendChild(preview);
                }
            }
            previousAttempts = previousAttempts || [];
            if (previousAttempts.length) {
                card.appendChild(elementWith('discuss-task-meta', 'Attempt ' + String(task.attempt || previousAttempts.length + 1)));
            }
            if (task.restored) {
                var restoredLabel = task.status === 'restored'
                    ? 'Historical result · reselect the passage to use it safely'
                    : 'Restored from ' + discussAgentLabel() + ' history';
                card.appendChild(elementWith('discuss-task-meta', restoredLabel));
            }
            if (task.skill && task.skill.name) card.appendChild(elementWith('discuss-task-meta', 'Skill · ' + task.skill.name));
            if (target && target.scope_note) card.appendChild(elementWith('discuss-task-meta', target.scope_note));
            if (task.error) card.appendChild(elementWith('discuss-error', task.error));
            if (previousAttempts.length) {
                var attempts = document.createElement('details'); attempts.className = 'discuss-attempts';
                var attemptsSummary = document.createElement('summary');
                attemptsSummary.textContent = previousAttempts.length + ' previous attempt' + (previousAttempts.length === 1 ? '' : 's');
                attempts.appendChild(attemptsSummary);
                var attemptsList = document.createElement('ul');
                previousAttempts.forEach(function(previous) {
                    var attempt = document.createElement('li');
                    attempt.textContent = 'Attempt ' + String(previous.attempt || 1) + ' · ' + String(previous.status || 'unknown');
                    attemptsList.appendChild(attempt);
                });
                attempts.appendChild(attemptsList); card.appendChild(attempts);
            }
            var result = task.result || {};
            if (result.kind === 'continuity_report') {
                card.appendChild(renderContinuityReport(task, result));
            } else if (result.kind === 'critique') {
                var list = document.createElement('ol'); list.className = 'discuss-findings';
                (result.findings || []).forEach(function(finding) {
                    var item = document.createElement('li');
                    var observation = document.createElement('strong'); observation.textContent = finding.observation; item.appendChild(observation);
                    var evidence = document.createElement('q'); evidence.textContent = finding.evidence; item.appendChild(evidence);
                    item.appendChild(elementWith('discuss-finding-detail', finding.why_it_matters));
                    item.appendChild(elementWith('discuss-finding-next', 'Next: ' + finding.next_step));
                    list.appendChild(item);
                });
                card.appendChild(list);
                if (discussTaskDocument(target)) {
                    var propose = document.createElement('button'); propose.type = 'button'; propose.className = 'discuss-secondary'; propose.textContent = 'Propose a revision';
                    propose.onclick = function() {
                        var taskDocument = discussTaskDocument(target);
                        _discussSelection = target.selection || '';
                        _discussSelectionRange = target.range || null;
                        _discussDraftDocument = taskDocument;
                        _discussSelectionSnapshot = discussSelectionSnapshotFor(taskDocument);
                        _discussSelectionSourceTaskId = task.id;
                        _discussLiveDocument = discussLiveDocumentFor(taskDocument);
                        _discussPendingAction = 'rephrase';
                        _discussRetryOfTaskId = null;
                        var input = document.getElementById('discussInput'); input.value = 'Address the critique while preserving the passage’s facts, point of view, and tense.';
                        renderDiscussContext(); renderDiscussTaskMode(); saveDiscussDraft(); input.focus();
                    };
                    card.appendChild(propose);
                }
            } else if (result.kind === 'alternatives') {
                card.appendChild(elementWith('discuss-task-summary', result.summary || 'Rewrite alternatives are ready.'));
                card.appendChild(elementWith('discuss-task-meta', discussAlternativesStateSummary(task, result)));
                card.appendChild(renderDiscussAlternatives(task, result));
                if ((task.status === 'ready' || task.status === 'reviewing') && task.reviewable !== false) {
                    var review = document.createElement('button'); review.type = 'button'; review.className = 'discuss-primary'; review.textContent = 'Review changes';
                    review.onclick = function() { reviewDiscussTask(task, review); }; card.appendChild(review);
                }
            }
            if ((task.status === 'failed' || task.status === 'cancelled' || task.status === 'stale')
                && discussTaskDocument(target)) {
                var retry = document.createElement('button'); retry.type = 'button'; retry.className = 'discuss-secondary'; retry.textContent = 'Try again';
                retry.onclick = function() {
                    var taskDocument = discussTaskDocument(target);
                    _discussSelection = target.selection || '';
                    _discussSelectionRange = target.range || null;
                    _discussDraftDocument = taskDocument;
                    _discussSelectionSnapshot = discussSelectionSnapshotFor(taskDocument);
                    _discussSelectionSourceTaskId = task.id;
                    _discussLiveDocument = discussLiveDocumentFor(taskDocument);
                    _discussPendingAction = task.action_id;
                    _discussRetryOfTaskId = task.id;
                    var input = document.getElementById('discussInput');
                    input.value = task.instruction || '';
                    renderDiscussContext(); renderDiscussTaskMode(); saveDiscussDraft();
                    if (task.action_id === 'custom_rewrite' || task.instruction) {
                        input.focus();
                        document.getElementById('discussAnnouncement').textContent = 'Review the restored instruction, then run the action again';
                    } else runDiscussSelectionAction(
                        task.action_id, target.selection || '', target.range || null, _discussLiveDocument,
                        0, task.id, _discussSelectionSnapshot, task.id
                    );
                };
                card.appendChild(retry);
            }
            return card;
        }

        function reviewDiscussTask(task, button) {
            if (button) button.disabled = true;
            var opened = false;
            discussApi('/api/discuss/conversations/' + encodeURIComponent(_discussConversationId) + '/tasks/' + encodeURIComponent(task.id) + '/proposal', {
                client_id: (typeof aiClientId === 'function' ? aiClientId() : null)
            }).then(function(data) {
                opened = true;
                if (data.proposal && typeof aiFocusProposal === 'function') aiFocusProposal(data.proposal, true);
                document.getElementById('discussAnnouncement').textContent = 'Rewrite ready for review. The manuscript is unchanged.';
                scheduleDiscussSnapshot();
            }).catch(function(error) {
                renderDiscussError(error.message);
                scheduleDiscussSnapshot();
            }).finally(function() { if (!opened && button) button.disabled = false; });
        }

        function autoReviewDiscussTask(taskId) {
            var key = String(_discussConversationId || '') + ':' + String(taskId || '');
            if (!taskId || _discussAutoReviewedTasks[key]) return;
            _discussAutoReviewedTasks[key] = true;
            reviewDiscussTask({id: taskId}, null);
        }

        function downloadDiscussHistory(data, title) {
            data.exported_at = new Date().toISOString();
            var blob = new Blob([JSON.stringify(data, null, 2) + '\n'], {type: 'application/json'});
            var url = URL.createObjectURL(blob); var link = document.createElement('a'); link.href = url;
            var slug = String(title || 'conversation').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 60);
            link.download = 'prosview-' + (slug || 'conversation') + '.json'; link.click();
            setTimeout(function() { URL.revokeObjectURL(url); }, 0);
            document.getElementById('discussAnnouncement').textContent = 'Conversation exported locally';
        }

        function historyActionButton(label, handler) {
            var button = document.createElement('button');
            button.type = 'button'; button.textContent = label; button.onclick = handler;
            return button;
        }

        function renderDiscussHistoryRows(rows) {
            var list = document.getElementById('discussHistoryList');
            var status = document.getElementById('discussHistoryStatus');
            list.replaceChildren();
            if (!rows.length) {
                status.textContent = 'No saved conversations for this project yet.'; status.hidden = false;
                return;
            }
            status.hidden = true;
            rows.forEach(function(item) {
                var row = elementWith('discuss-history-row');
                var copy = elementWith('discuss-history-copy');
                var title = document.createElement('strong'); title.textContent = item.title || 'Previous conversation'; copy.appendChild(title);
                if (item.preview) { var preview = document.createElement('span'); preview.textContent = item.preview; copy.appendChild(preview); }
                var meta = document.createElement('span');
                var stamp = item.updated_at ? new Date(item.updated_at * 1000).toLocaleString() : '';
                meta.textContent = (item.current ? 'Current conversation' : 'Saved conversation') + (stamp ? ' · ' + stamp : ''); copy.appendChild(meta);
                row.appendChild(copy);

                var actions = elementWith('discuss-history-actions');
                var openButton = historyActionButton(item.current ? 'Current' : 'Open', function() {
                    openButton.disabled = true;
                    discussApi('/api/discuss/conversations/' + encodeURIComponent(_discussConversationId) + '/history/' + encodeURIComponent(item.thread_id) + '/open', {})
                        .then(function(data) {
                            _discussSnapshot = data.snapshot; renderDiscussSnapshot();
                            document.getElementById('discussHistoryDialog').close('opened');
                            document.getElementById('discussAnnouncement').textContent = 'Conversation opened';
                            document.getElementById('discussInput').focus();
                        })
                        .catch(function(error) { openButton.disabled = false; status.textContent = error.message; status.hidden = false; });
                });
                var workInProgress = !!(_discussSnapshot && (
                    _discussSnapshot.active_request_id || _discussSnapshot.active_turn_id || (_discussSnapshot.queue || []).length
                ));
                openButton.disabled = !!item.current || workInProgress; actions.appendChild(openButton);
                var more = document.createElement('details');
                var summary = document.createElement('summary'); summary.textContent = 'More'; more.appendChild(summary);
                var menu = elementWith('discuss-history-menu');
                var renameForm = elementWith('discuss-history-rename'); renameForm.hidden = true;
                var renameLabel = document.createElement('label'); renameLabel.className = 'sr-only'; renameLabel.textContent = 'Conversation title';
                var renameInput = document.createElement('input'); renameInput.type = 'text'; renameInput.maxLength = 200; renameInput.value = item.title || '';
                renameLabel.appendChild(renameInput); renameForm.appendChild(renameLabel);
                menu.appendChild(historyActionButton('Rename', function() { more.open = false; renameForm.hidden = false; renameInput.focus(); renameInput.select(); }));
                menu.appendChild(historyActionButton('Export JSON', function() {
                    discussApi('/api/discuss/conversations/' + encodeURIComponent(_discussConversationId) + '/history/' + encodeURIComponent(item.thread_id) + '/export', {})
                        .then(function(data) { downloadDiscussHistory(data.export, item.title); })
                        .catch(function(error) { status.textContent = error.message; status.hidden = false; });
                }));
                var removeButton = historyActionButton('Remove from history', function() {
                    if (!window.confirm('Remove this conversation from Prosview history? It will remain in Codex history.')) return;
                    removeButton.disabled = true;
                    discussApi('/api/discuss/conversations/' + encodeURIComponent(_discussConversationId) + '/history/' + encodeURIComponent(item.thread_id) + '/remove', {})
                        .then(loadDiscussHistory)
                        .then(function() { document.getElementById('discussAnnouncement').textContent = 'Conversation removed from Prosview history'; })
                        .catch(function(error) { removeButton.disabled = false; status.textContent = error.message; status.hidden = false; });
                });
                removeButton.disabled = !!item.current; menu.appendChild(removeButton);
                more.appendChild(menu); actions.appendChild(more); row.appendChild(actions);
                renameForm.appendChild(historyActionButton('Save', function() {
                    var clean = renameInput.value.trim(); if (!clean) { renameInput.focus(); return; }
                    discussApi('/api/discuss/conversations/' + encodeURIComponent(_discussConversationId) + '/history/' + encodeURIComponent(item.thread_id) + '/rename', {title: clean})
                        .then(loadDiscussHistory)
                        .catch(function(error) { status.textContent = error.message; status.hidden = false; });
                }));
                renameForm.appendChild(historyActionButton('Cancel', function() { renameForm.hidden = true; summary.focus(); }));
                row.appendChild(renameForm); list.appendChild(row);
            });
        }

        function loadDiscussHistory() {
            var status = document.getElementById('discussHistoryStatus');
            status.textContent = 'Loading conversations…'; status.hidden = false;
            return discussApi('/api/discuss/conversations/' + encodeURIComponent(_discussConversationId) + '/history/list', {})
                .then(function(data) { renderDiscussHistoryRows(data.conversations || []); })
                .catch(function(error) { status.textContent = error.message; status.hidden = false; });
        }

        function openDiscussHistoryDialog() {
            if (!_discussConversationId) return;
            var dialog = document.getElementById('discussHistoryDialog');
            document.getElementById('discussHistoryList').replaceChildren();
            dialog.showModal(); loadDiscussHistory(); document.getElementById('discussHistoryClose').focus();
        }

        function clearDiscussHistory() {
            if (!_discussConversationId) return;
            if (!window.confirm('Clear assistance results for this conversation? This cannot be undone.')) return;
            discussApi('/api/discuss/conversations/' + encodeURIComponent(_discussConversationId) + '/tasks/clear', {})
                .then(function() { scheduleDiscussSnapshot(); document.getElementById('discussAnnouncement').textContent = 'Assistance results cleared'; })
                .catch(function(error) { renderDiscussError(error.message); });
        }

        function renderDiscussApproval(approval) {
            var card = elementWith('discuss-approval'); card.dataset.approvalId = approval.request_id;
            var title = document.createElement('h3'); title.textContent = approval.status === 'pending' ? 'Approval required' : 'Approval ' + approval.status; card.appendChild(title);
            card.appendChild(document.createTextNode(approval.reason || approval.kind || 'Codex requested an action.'));
            if (approval.command) { var code = document.createElement('code'); code.textContent = approval.command; card.appendChild(code); }
            if (approval.grant_root) { var root = document.createElement('code'); root.textContent = 'Write access: ' + approval.grant_root; card.appendChild(root); }
            if (approval.network) { var network = document.createElement('code'); network.textContent = 'Network: ' + JSON.stringify(approval.network); card.appendChild(network); }
            if (approval.permissions) { var permissions = document.createElement('code'); permissions.textContent = 'Permissions: ' + JSON.stringify(approval.permissions); card.appendChild(permissions); }
            if (approval.status === 'pending') {
                if (approval.kind === 'fileChange' && _discussSnapshot) {
                    var activity = (_discussSnapshot.activities || []).find(function(a) { return a.id === approval.item_id; });
                    if (activity && activity.changes && activity.changes.length > 0 && activity.changes[0].diff) {
                        var diffString = activity.changes[0].diff;
                        card.appendChild(createDiscussDiffViewer(diffString));
                    }
                }
                var actions = elementWith('discuss-approval-actions');
                var options = [
                    ['accept', 'Accept once'], ['accept_for_session', 'Accept for session ⚠'], ['decline', 'Decline'], ['cancel', 'Cancel']
                ];
                options.forEach(function(option) {
                    var wire = option[0] === 'accept_for_session' ? 'acceptForSession' : option[0];
                    if ((approval.available_decisions || []).indexOf(wire) < 0) return;
                    var button = document.createElement('button'); button.type = 'button'; button.textContent = option[1];
                    if (option[0] === 'accept_for_session') button.title = 'Allows matching requests until this Codex session ends';
                    button.onclick = function() { resolveDiscussApproval(approval.request_id, option[0], button, approval.permissions); };
                    actions.appendChild(button);
                });
                card.appendChild(actions);
            }
            return card;
        }

        


function createDiscussDiffViewer(diffString) {
    var wrapper = document.createElement('div');
    wrapper.style.marginTop = '12px';
    
    var header = document.createElement('div');
    header.style.display = 'flex';
    header.style.justifyContent = 'space-between';
    header.style.alignItems = 'center';
    header.style.marginBottom = '6px';
    
    var expandBtn = document.createElement('button');
    expandBtn.className = 'discuss-chip discuss-chip-outline';
    expandBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="margin-right: 4px; vertical-align: text-bottom;"><polyline points="15 3 21 3 21 9"></polyline><polyline points="9 21 3 21 3 15"></polyline><line x1="21" y1="3" x2="14" y2="10"></line><line x1="3" y1="21" x2="10" y2="14"></line></svg> Expand view';
    expandBtn.style.cursor = 'pointer';
    expandBtn.onclick = function(e) { e.preventDefault(); openDiscussDiffModal(diffString); };
    
    var toggleGroup = document.createElement('div');
    toggleGroup.style.display = 'flex';
    toggleGroup.style.gap = '4px';
    
    var btnInline = document.createElement('button');
    btnInline.className = 'discuss-chip';
    btnInline.textContent = 'Inline';
    btnInline.style.cursor = 'pointer';
    
    var btnSplit = document.createElement('button');
    btnSplit.className = 'discuss-chip discuss-chip-outline';
    btnSplit.textContent = 'Split';
    btnSplit.style.cursor = 'pointer';
    
    toggleGroup.appendChild(btnInline);
    toggleGroup.appendChild(btnSplit);
    
    header.appendChild(expandBtn);
    header.appendChild(toggleGroup);
    
    var diffContainer = document.createElement('div');
    diffContainer.className = 'discuss-diff-viewer';
    diffContainer.style.maxHeight = '300px';
    diffContainer.style.overflowY = 'auto';
    diffContainer.style.backgroundColor = 'var(--surface-bg, #0d1117)';
    diffContainer.style.border = '1px solid var(--border-color, #30363d)';
    diffContainer.style.borderRadius = '6px';
    
    wrapper.appendChild(header);
    wrapper.appendChild(diffContainer);
    
    function loadDiff(mode) {
        btnInline.className = mode === 'inline' ? 'discuss-chip' : 'discuss-chip discuss-chip-outline';
        btnSplit.className = mode === 'side-by-side' ? 'discuss-chip' : 'discuss-chip discuss-chip-outline';
        
        diffContainer.innerHTML = '<div style="padding: 12px; text-align: center; color: var(--text-muted);">Loading diff...</div>';
        
        discussApi('/api/discuss/format_patch', {patch: diffString, mode: mode})
            .then(function(res) {
                diffContainer.innerHTML = res.diff_html;
            })
            .catch(function(err) {
                diffContainer.textContent = 'Error loading diff: ' + err.message;
            });
    }
    
    btnInline.onclick = function(e) { e.preventDefault(); loadDiff('inline'); };
    btnSplit.onclick = function(e) { e.preventDefault(); loadDiff('side-by-side'); };
    
    loadDiff('inline');
    
    return wrapper;
}



var _currentDiscussDiffString = null;
var _currentDiscussDiffMode = 'inline';

function openDiscussDiffModal(diffString) {
    _currentDiscussDiffString = diffString;
    document.getElementById("discussDiffModalOverlay").hidden = false;
    loadDiscussDiffMode(_currentDiscussDiffMode);
}

function closeDiscussDiffModal() {
    document.getElementById("discussDiffModalOverlay").hidden = true;
    _currentDiscussDiffString = null;
}

function setDiscussDiffMode(mode) {
    _currentDiscussDiffMode = mode;
    
    var btnInline = document.getElementById('discussDiffToggleInline');
    var btnSplit = document.getElementById('discussDiffToggleSideBySide');
    if (btnInline && btnSplit) {
        if (mode === 'inline') {
            btnInline.classList.add('active');
            btnSplit.classList.remove('active');
        } else {
            btnSplit.classList.add('active');
            btnInline.classList.remove('active');
        }
    }
    loadDiscussDiffMode(mode);
}

function loadDiscussDiffMode(mode) {
    if (!_currentDiscussDiffString) return;
    var contentDiv = document.getElementById('discussDiffModalContent');
    contentDiv.innerHTML = '<div style="padding: 24px; text-align: center; color: var(--text-muted);">Loading diff...</div>';
    discussApi('/api/discuss/format_patch', {patch: _currentDiscussDiffString, mode: mode})
        .then(function(res) {
            contentDiv.innerHTML = res.diff_html;
        })
        .catch(function(err) {
            contentDiv.textContent = 'Error loading diff: ' + err.message;
        });
}

// Close discuss modal when clicking outside
(function() {
    const overlay = document.getElementById("discussDiffModalOverlay");
    if (overlay) {
        overlay.addEventListener("click", function(event) {
            if (event.target === overlay) {
                closeDiscussDiffModal();
            }
        });
    }
});


function renderDiscussActivityCard(activity) {
            var card = elementWith('discuss-approval');
            var header = elementWith('discuss-approval-header');
            var title = document.createElement('strong'); title.textContent = 'Auto-accepted file change';
            header.appendChild(title);
            card.appendChild(header);
            
            var kindName = document.createElement('div');
            kindName.textContent = 'fileChange';
            kindName.style.fontSize = '14px';
            kindName.style.marginTop = '4px';
            card.appendChild(kindName);
            
            var diffString = activity.changes[0].diff;
            card.appendChild(createDiscussDiffViewer(diffString));
            
            if (activity.status !== 'rejected') {
                var actions = elementWith('discuss-approval-actions');
                var button = document.createElement('button'); 
                button.type = 'button'; 
                button.textContent = 'Reject & Revert';
                button.onclick = function() { rejectDiscussActivity(activity.id, button); };
                actions.appendChild(button);
                card.appendChild(actions);
            } else {
                var rejectedMsg = document.createElement('div');
                rejectedMsg.style.marginTop = '12px';
                rejectedMsg.style.color = 'var(--text-danger)';
                rejectedMsg.style.fontWeight = 'bold';
                rejectedMsg.textContent = 'This change was rejected and reverted.';
                card.appendChild(rejectedMsg);
            }
            return card;
        }

        function rejectDiscussActivity(activityId, button) {
            button.disabled = true;
            discussApi('/api/discuss/conversations/' + encodeURIComponent(_discussConversationId) + '/activities/' + encodeURIComponent(activityId) + '/reject', {})
                .then(function() { document.getElementById('discussAnnouncement').textContent = 'Activity rejected'; scheduleDiscussSnapshot(); })
                .catch(function(error) { button.disabled = false; renderDiscussError(error.message); scheduleDiscussSnapshot(); });
        }

        function resolveDiscussApproval(requestId, decision, button, permissions) {
            button.disabled = true;
            var payload = {decision: decision};
            if (permissions && (decision === 'accept' || decision === 'accept_for_session')) payload.permissions = permissions;
            discussApi('/api/discuss/conversations/' + encodeURIComponent(_discussConversationId) + '/approvals/' + encodeURIComponent(requestId), payload)
                .then(function() { document.getElementById('discussAnnouncement').textContent = 'Approval ' + decision.replace('_', ' '); scheduleDiscussSnapshot(); })
                .catch(function(error) { renderDiscussError(error.message); scheduleDiscussSnapshot(); })
                .finally(function() { document.getElementById('discussInput').focus(); });
        }

        function appendDiscussLocalError(log) {
            if (!_discussLocalError) return;
            var node = elementWith('discuss-error discuss-local-error');
            node.appendChild(elementWith('discuss-local-error-message', _discussLocalError));
            if (_discussLocalErrorReload) {
                var reload = document.createElement('button');
                reload.type = 'button'; reload.className = 'discuss-secondary'; reload.textContent = 'Reload page';
                reload.onclick = function() { saveDiscussDraft(); location.reload(); };
                node.appendChild(reload);
            }
            log.appendChild(node);
            return node;
        }

        function renderDiscussError(message, options) {
            options = options || {};
            _discussLocalError = String(message || 'Something went wrong');
            _discussLocalErrorKind = String(options.kind || (
                _discussLocalError.indexOf('Proseview server is not responding.') === 0 ? 'transport' : 'request'
            ));
            _discussLocalErrorReload = !!options.reload;
            var log = document.getElementById('discussLog');
            var existing = log.querySelector('.discuss-local-error');
            if (existing) existing.remove();
            var node = appendDiscussLocalError(log);
            if (node) node.scrollIntoView({block: 'nearest'});
        }

        function clearDiscussError() {
            _discussLocalError = '';
            _discussLocalErrorKind = '';
            _discussLocalErrorReload = false;
            var existing = document.querySelector('#discussLog .discuss-local-error');
            if (existing) existing.remove();
        }

        function clearDiscussTransportError() {
            if (_discussLocalErrorKind === 'transport') clearDiscussError();
        }

        function discussAfterActivity(wasAtBottom) {
            var log = document.getElementById('discussLog');
            if (wasAtBottom) requestAnimationFrame(function() { log.scrollTop = log.scrollHeight; });
            else document.getElementById('discussNewActivity').hidden = false;
        }

        function discussScrollToEnd() {
            var log = document.getElementById('discussLog'); log.scrollTop = log.scrollHeight;
            document.getElementById('discussNewActivity').hidden = true;
        }

        function renderDiscussContext() {
            var context = document.getElementById('discussContext'); context.replaceChildren();
            var doc = discussTurnDocument();
            if (doc && _discussIncludeCurrentDocument) {
                var current = elementWith('discuss-chip discuss-chip-current', doc.path);
                current.title = 'File attached to the next question';
                var removeCurrent = document.createElement('button');
                removeCurrent.type = 'button'; removeCurrent.textContent = '×';
                removeCurrent.setAttribute('aria-label', 'Remove current document ' + doc.path);
                removeCurrent.onclick = function() {
                    _discussIncludeCurrentDocument = false;
                    if (!document.getElementById('discussInput').value && !_discussSelection
                        && !_discussPendingAction && !_discussRepositoryAction) {
                        _discussDraftDocument = null;
                    }
                    saveDiscussDraft();
                    renderDiscussContext();
                    document.getElementById('discussAnnouncement').textContent = 'Document removed from context';
                };
                current.appendChild(removeCurrent); context.appendChild(current);
            } else if (!_discussSelection && !_discussPendingAction && !_discussRepositoryAction) {
                var visibleDocument = discussDocument();
                if (visibleDocument) {
                    var attachCurrent = document.createElement('button');
                    attachCurrent.type = 'button';
                    attachCurrent.className = 'discuss-chip discuss-chip-attach';
                    attachCurrent.textContent = 'Attach current · ' + visibleDocument.path;
                    attachCurrent.setAttribute('aria-label', 'Attach current document ' + visibleDocument.path);
                    attachCurrent.onclick = function() {
                        _discussDraftDocument = Object.assign({}, visibleDocument);
                        _discussIncludeCurrentDocument = true;
                        saveDiscussDraft();
                        renderDiscussContext();
                        document.getElementById('discussAnnouncement').textContent = visibleDocument.path + ' attached to the next question';
                    };
                    context.appendChild(attachCurrent);
                }
            }
            _discussAttachments.forEach(function(attachment, index) {
                var chip = elementWith('discuss-chip', attachment.path);
                var remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '×'; remove.setAttribute('aria-label', 'Remove ' + attachment.path);
                remove.onclick = function() { _discussAttachments.splice(index, 1); renderDiscussContext(); }; chip.appendChild(remove); context.appendChild(chip);
            });
            var selection = document.getElementById('discussSelectionChip');
            selection.hidden = !_discussSelection;
            selection.replaceChildren();
            if (_discussSelection) {
                var words = _discussSelection.trim().split(/\s+/).filter(Boolean).length;
                selection.appendChild(document.createTextNode('Selection · ' + words + ' words · “' + _discussSelection.slice(0, 72) + (_discussSelection.length > 72 ? '…' : '') + '”'));
                var removeSelection = document.createElement('button');
                removeSelection.type = 'button'; removeSelection.textContent = '×';
                removeSelection.setAttribute('aria-label', 'Remove selected text from ' + discussAgentLabel() + ' context');
                removeSelection.onclick = function() {
                    _discussSelection = ''; _discussSelectionRange = null; _discussSelectionSnapshot = null;
                    _discussSelectionSourceTaskId = null; _discussLiveDocument = null;
                    _discussPendingAction = null; _discussRetryOfTaskId = null;
                    if (!document.getElementById('discussInput').value) _discussDraftDocument = null;
                    saveDiscussDraft(); renderDiscussContext(); renderDiscussTaskMode();
                    document.getElementById('discussAnnouncement').textContent = 'Selection removed from context';
                };
                selection.appendChild(removeSelection);
            }
        }

        function selectionActionLabel(actionId) {
            return ({
                rephrase: 'Rephrase', tighten: 'Tighten', clarify: 'Clarify', sensory_detail: 'Add sensory detail',
                show_moment: 'Show the moment', custom_rewrite: 'Custom rewrite', quick_critique: 'Quick critique', voice_character: 'Voice and character',
                pacing_tension: 'Pacing and tension', clarity_flow: 'Clarity and flow', continuity: 'Continuity check',
                canon_refactor: 'Trace a canon change', scene_continuity: "Check this scene's continuity",
                verify_refactor: 'Verify a canon change'
            })[actionId] || 'Selection action';
        }

        function normalizedDiscussInstructions(rows, limit) {
            if (!Array.isArray(rows)) return [];
            var normalized = [];
            rows.forEach(function(row) {
                if (typeof row !== 'string') return;
                var value = row.trim();
                if (value.length > 32768) return;
                if (value && normalized.indexOf(value) < 0) normalized.push(value);
            });
            return normalized.slice(0, limit);
        }

        function recentDiscussInstructions() {
            try { return normalizedDiscussInstructions(JSON.parse(localStorage.getItem('proseview-codex-recent-instructions') || '[]'), 8); }
            catch(e) { return []; }
        }

        function rememberDiscussInstruction(value) {
            value = String(value || '').trim();
            if (!value) return;
            var rows = recentDiscussInstructions().filter(function(row) { return row !== value; });
            rows.unshift(value);
            try { localStorage.setItem('proseview-codex-recent-instructions', JSON.stringify(rows.slice(0, 8))); } catch(e) {}
        }

        function favoriteDiscussInstructions() {
            try { return normalizedDiscussInstructions(JSON.parse(localStorage.getItem('proseview-codex-favorite-instructions') || '[]'), 12); }
            catch(e) { return []; }
        }

        function configuredDiscussInstructions() {
            return normalizedDiscussInstructions(
                typeof discussSelectionPresets === 'undefined' ? [] : discussSelectionPresets,
                12
            );
        }

        function presetDiscussInstructions() {
            return normalizedDiscussInstructions(
                favoriteDiscussInstructions().concat(configuredDiscussInstructions()),
                24
            );
        }

        function toggleDiscussInstructionFavorite(value, reopenMenu) {
            var rows = favoriteDiscussInstructions(); var index = rows.indexOf(value);
            if (index >= 0) rows.splice(index, 1); else rows.unshift(value);
            try { localStorage.setItem('proseview-codex-favorite-instructions', JSON.stringify(rows.slice(0, 12))); } catch(e) {}
            renderDiscussTaskMode();
            if (reopenMenu) {
                var details = document.querySelector('#discussTaskMode .discuss-presets-more');
                if (details) {
                    details.open = true;
                    var label = (index >= 0 ? 'Add to favorites: ' : 'Remove from favorites: ') + value;
                    Array.prototype.some.call(details.querySelectorAll('.discuss-favorite'), function(button) {
                        if (button.getAttribute('aria-label') !== label) return false;
                        button.focus();
                        return true;
                    });
                }
            }
        }

        function chooseDiscussInstruction(value, details) {
            document.getElementById('discussInput').value = value;
            var visibleDocument = discussDocument();
            if (visibleDocument && !_discussSelection && !_discussPendingAction && !_discussRepositoryAction) {
                _discussDraftDocument = Object.assign({}, visibleDocument);
                _discussIncludeCurrentDocument = true;
                renderDiscussContext();
            }
            saveDiscussDraft();
            if (details) details.open = false;
            document.getElementById('discussInput').focus();
        }

        function appendDiscussPresetMenuRow(node, value, favorite, details) {
            var wrap = elementWith('discuss-preset-menu-row');
            var button = document.createElement('button'); button.type = 'button'; button.className = 'discuss-preset-menu-choice'; button.textContent = value;
            button.title = value;
            button.onclick = function() { chooseDiscussInstruction(value, details); };
            var star = document.createElement('button'); star.type = 'button'; star.className = 'discuss-favorite'; star.textContent = favorite ? '★' : '☆';
            star.setAttribute('aria-label', (favorite ? 'Remove from favorites: ' : 'Add to favorites: ') + value);
            star.onclick = function() { toggleDiscussInstructionFavorite(value, true); };
            wrap.appendChild(button); wrap.appendChild(star); node.appendChild(wrap);
        }

        function appendDiscussPresetsMenu(node, presets, recents) {
            var details = document.createElement('details'); details.className = 'discuss-presets-more';
            var summary = document.createElement('summary'); summary.textContent = presets.length ? 'More…' : 'Add from recent…';
            summary.setAttribute('role', 'button');
            summary.setAttribute('aria-label', 'More presets and recent instructions');
            details.appendChild(summary);
            var popover = elementWith('discuss-presets-popover'); popover.id = 'discussPresetsPopover';
            var favorites = favoriteDiscussInstructions();
            if (presets.length) {
                var presetHeading = document.createElement('strong'); presetHeading.textContent = 'Presets'; popover.appendChild(presetHeading);
                presets.forEach(function(value) {
                    appendDiscussPresetMenuRow(popover, value, favorites.indexOf(value) >= 0, details);
                });
            }
            if (recents.length) {
                var recentHeading = document.createElement('strong'); recentHeading.textContent = 'Recent'; popover.appendChild(recentHeading);
                recents.forEach(function(value) { appendDiscussPresetMenuRow(popover, value, false, details); });
            }
            details.appendChild(popover); node.appendChild(details);
        }

        function renderDiscussTaskMode() {
            var node = document.getElementById('discussTaskMode');
            if (!node) return;
            node.replaceChildren();
            if (_discussRepositoryAction) {
                node.hidden = false;
                var repositoryTitle = document.createElement('strong');
                repositoryTitle.textContent = selectionActionLabel(_discussRepositoryAction); node.appendChild(repositoryTitle);
                var repositoryHelp = document.createElement('span');
                repositoryHelp.textContent = 'Read-only scan · configured manuscript and repository folders'; node.appendChild(repositoryHelp);
                var changeAction = document.createElement('button');
                changeAction.type = 'button'; changeAction.className = 'discuss-secondary'; changeAction.textContent = 'Change action';
                changeAction.onclick = cancelDiscussRepositoryAction; node.appendChild(changeAction);
                document.getElementById('discussInput').placeholder = _discussRepositoryAction === 'canon_refactor'
                    ? 'Describe the canon change, including the old and new fact…'
                    : 'Optional: name the continuity concern to focus on…';
                document.getElementById('discussSend').textContent = 'Scan';
                return;
            }
            if (_discussPendingAction) {
                node.hidden = false;
                var title = document.createElement('strong'); title.textContent = selectionActionLabel(_discussPendingAction) + ' selection'; node.appendChild(title);
                var help = document.createElement('span'); help.textContent = 'Optional constraint'; node.appendChild(help);
                document.getElementById('discussInput').placeholder = 'Add a constraint, or run as shown…';
                document.getElementById('discussSend').textContent = 'Run';
                return;
            }
            document.getElementById('discussSend').textContent = 'Send';
            var presets = presetDiscussInstructions();
            var recents = recentDiscussInstructions().filter(function(value) { return presets.indexOf(value) < 0; });
            document.getElementById('discussInput').placeholder = _discussSelection
                ? 'Ask anything about this selection…'
                : 'Ask anything about your story…';
            node.hidden = !(_discussSelection && (presets.length || recents.length));
            if (!node.hidden) {
                var label = document.createElement('strong'); label.textContent = 'Presets'; node.appendChild(label);
                var inline = elementWith('discuss-presets-inline');
                presets.slice(0, 3).forEach(function(value) {
                    var button = document.createElement('button');
                    button.type = 'button'; button.className = 'discuss-preset-inline'; button.textContent = value; button.title = value;
                    button.onclick = function() { chooseDiscussInstruction(value); };
                    inline.appendChild(button);
                });
                if (presets.length) node.appendChild(inline);
                if (presets.length > 3 || recents.length || favoriteDiscussInstructions().length) {
                    appendDiscussPresetsMenu(node, presets, recents);
                }
            }
        }

        function loadDiscussSkills(forceReload) {
            var picker = document.getElementById('discussSkillsPicker');
            picker.hidden = false; picker.textContent = 'Loading Codex skills…';
            discussApi('/api/discuss/skills', {force_reload: !!forceReload}).then(function(data) {
                _discussSkills = data.skills || []; renderDiscussSkills();
            }).catch(function(error) { picker.textContent = 'Skills unavailable: ' + error.message; });
        }

        function renderDiscussSkills() {
            var picker = document.getElementById('discussSkillsPicker'); picker.replaceChildren(); picker.hidden = false;
            var title = document.createElement('strong'); title.textContent = 'Run a skill'; picker.appendChild(title);
            var search = document.createElement('input'); search.type = 'search'; search.className = 'discuss-skill-search'; search.placeholder = 'Search skills'; search.setAttribute('aria-label', 'Search Codex skills'); picker.appendChild(search);
            var results = elementWith('discuss-skill-results'); picker.appendChild(results);
            function showSkills() {
                var query = search.value.trim().toLowerCase(); results.replaceChildren();
                var matches = _discussSkills.filter(function(skill) { return !query || (skill.name + ' ' + skill.display_name + ' ' + skill.description).toLowerCase().includes(query); });
                if (!matches.length) results.appendChild(document.createTextNode(_discussSkills.length ? 'No matching skills.' : 'No enabled Codex skills were found.'));
                matches.forEach(function(skill) {
                    var button = document.createElement('button'); button.type = 'button'; button.className = 'discuss-skill';
                    var name = document.createElement('strong'); name.textContent = skill.display_name || skill.name;
                    var description = document.createElement('span'); description.textContent = skill.description || 'Codex skill';
                    var metadata = document.createElement('small'); metadata.textContent = 'Conversation / unknown output · ' + (skill.scope || 'Codex');
                    button.appendChild(name); button.appendChild(description); button.appendChild(metadata);
                    if (skill.dependencies && Object.keys(skill.dependencies).length) {
                        var dependency = document.createElement('small'); dependency.textContent = 'Dependencies: ' + JSON.stringify(skill.dependencies); button.appendChild(dependency);
                    }
                    button.onclick = function() {
                        _discussSelectedSkill = {name: skill.name, path: skill.path};
                        picker.hidden = true;
                        var input = document.getElementById('discussInput');
                        input.placeholder = 'Tell ' + (skill.display_name || skill.name) + ' what to do with this selection…'; input.focus();
                        document.getElementById('discussAnnouncement').textContent = (skill.display_name || skill.name) + ' selected';
                    };
                    results.appendChild(button);
                });
            }
            search.oninput = showSkills; showSkills();
            var cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'discuss-secondary'; cancel.textContent = 'Cancel';
            cancel.onclick = function() { picker.hidden = true; document.getElementById('discussInput').focus(); };
            picker.appendChild(cancel); search.focus();
        }


        // ---- Model picker --------------------------------------------------
        // What the next turn runs as, chosen where the turn is typed. The
        // choice belongs to this conversation: starting a new one goes back to
        // the agent's own default, so an expensive setting picked for one hard
        // question does not quietly become the standing cost of every later
        // one. Both agents apply it from the next turn, never to the running
        // one, and the chip says so rather than implying otherwise.
        var _discussModelCatalogs = {codex: null, claude: null};
        var _discussModelCatalogPending = {codex: false, claude: false};
        var _discussModelPickerOpen = false;
        var _discussModelBusy = false;

        function discussModelSelection() {
            var selection = (_discussSnapshot && _discussSnapshot.model) || {};
            return {model: selection.model || '', effort: selection.effort || ''};
        }

        function discussModelCatalog() {
            return _discussModelCatalogs[_discussAgent] || null;
        }

        function discussModelRow(catalog, modelId) {
            if (!catalog || !modelId) return null;
            var rows = catalog.models || [];
            for (var i = 0; i < rows.length; i++) {
                if (rows[i].id === modelId) return rows[i];
            }
            return null;
        }

        function discussModelEfforts(catalog, modelId) {
            var row = discussModelRow(catalog, modelId);
            return (row && row.efforts) || [];
        }

        function loadDiscussModelCatalog(agent) {
            agent = DISCUSS_AGENTS.indexOf(agent) >= 0 ? agent : _discussAgent;
            if (_discussModelCatalogs[agent] || _discussModelCatalogPending[agent]) return;
            _discussModelCatalogPending[agent] = true;
            discussApi('/api/discuss/models', {agent: agent}).then(function(data) {
                _discussModelCatalogs[agent] = data.catalog || null;
                if (agent === _discussAgent) renderDiscussModelChip();
            }).catch(function() {
                // A roster Prosview could not read is not a roster to invent.
                // The chip stays hidden and questions keep working on the
                // agent's own default.
                _discussModelCatalogs[agent] = null;
            }).finally(function() {
                _discussModelCatalogPending[agent] = false;
            });
        }

        function renderDiscussModelChip() {
            var chip = document.getElementById('discussModelChip');
            if (!chip) return;
            var catalog = discussModelCatalog();
            if (!catalog) {
                chip.hidden = true;
                closeDiscussModelPicker();
                return;
            }
            chip.hidden = false;
            var selection = discussModelSelection();
            var fallback = catalog.default || {};
            var inherited = !selection.model && !selection.effort;
            var row = discussModelRow(catalog, selection.model);
            var name = selection.model
                ? ((row && row.label) || selection.model)
                : (fallback.label || fallback.model || 'Default');
            var effort = selection.effort || (selection.model ? ((row && row.default_effort) || '') : (fallback.effort || ''));
            chip.dataset.inherited = inherited ? 'true' : 'false';
            document.getElementById('discussModelName').textContent = name;
            var effortNode = document.getElementById('discussModelEffort');
            effortNode.textContent = effort;
            effortNode.previousElementSibling.hidden = !effort;
            var snapshot = _discussSnapshot || {};
            var reason = '';
            if (snapshot.active_turn_id) reason = 'Wait for ' + discussAgentLabel() + ' to finish before changing model.';
            else if (snapshot.active_request_id) reason = 'Wait for this question to start before changing model.';
            _discussModelBusy = !!reason;
            chip.disabled = _discussModelBusy;
            chip.title = reason || (inherited
                ? 'Following ' + (catalog.default.source || 'the agent default') + '. Click to choose a model.'
                : 'Pinned to this conversation. Click to change.');
            chip.setAttribute('aria-label', 'Model: ' + name + (effort ? ', effort ' + effort : '') + '. Change model');
            if (_discussModelBusy) closeDiscussModelPicker();
            if (_discussModelPickerOpen) renderDiscussModelPicker();
        }

        function renderDiscussModelPicker() {
            var catalog = discussModelCatalog();
            var list = document.getElementById('discussModelList');
            var efforts = document.getElementById('discussModelEfforts');
            if (!catalog || !list) return;
            var selection = discussModelSelection();
            // Choosing anything rebuilds both rows, which would drop a keyboard
            // user back to the document. Remember where they were standing.
            var active = document.activeElement;
            var restore = active && active.dataset && (active.dataset.discussModel !== undefined
                ? {kind: 'model', id: active.dataset.discussModel}
                : (active.dataset.discussEffort ? {kind: 'effort', id: active.dataset.discussEffort} : null));
            list.replaceChildren();
            var rows = [{
                id: '',
                label: 'Follow ' + discussAgentLabel() + ' settings',
                description: catalog.default.label
                    ? (catalog.default.label + (catalog.default.effort ? ' · ' + catalog.default.effort : '')
                       + ', from ' + (catalog.default.source || 'your own configuration'))
                    : (catalog.default.source || 'Whatever this agent is configured to use'),
                badge: 'Default'
            }].concat(catalog.models || []);
            rows.forEach(function(row) {
                var option = document.createElement('button');
                option.type = 'button';
                option.className = 'discuss-model-option';
                option.setAttribute('role', 'radio');
                var checked = (row.id || '') === selection.model;
                option.setAttribute('aria-checked', checked ? 'true' : 'false');
                option.appendChild(elementWith('discuss-model-option-tick', checked ? '●' : ''));
                var body = document.createElement('span');
                body.style.minWidth = '0';
                var name = document.createElement('strong');
                name.textContent = row.label || row.id;
                body.appendChild(name);
                body.appendChild(elementWith('', row.description || ''));
                option.appendChild(body);
                if (row.badge) option.appendChild(elementWith('discuss-model-badge', row.badge));
                else if (row.retiring) option.appendChild(elementWith('discuss-model-badge retiring', 'Retiring'));
                else option.appendChild(document.createElement('span'));
                option.dataset.discussModel = row.id || '';
                option.onclick = function() { chooseDiscussModel(row.id || ''); };
                list.appendChild(option);
            });
            var ladder = selection.model ? discussModelEfforts(catalog, selection.model) : [];
            var describedBy = null;
            efforts.replaceChildren();
            // Always the full union, with this model's missing rungs greyed
            // out: a row that silently loses its top entries hides the reason
            // a cheaper model cannot think as hard.
            discussModelEffortUnion(catalog).forEach(function(entry) {
                var button = document.createElement('button');
                button.type = 'button';
                button.className = 'discuss-model-effort-option';
                button.textContent = entry.id;
                var supported = !ladder.length ? false : ladder.some(function(row) { return row.id === entry.id; });
                button.disabled = !supported;
                var active = supported && selection.effort === entry.id;
                button.setAttribute('aria-pressed', active ? 'true' : 'false');
                if (active) describedBy = entry.description || '';
                if (entry.description) button.title = entry.description;
                button.dataset.discussEffort = entry.id;
                button.onclick = function() { chooseDiscussEffort(entry.id); };
                efforts.appendChild(button);
            });
            if (restore) {
                var again = restore.kind === 'model'
                    ? list.querySelector('[data-discuss-model="' + CSS.escape(restore.id) + '"]')
                    : efforts.querySelector('[data-discuss-effort="' + CSS.escape(restore.id) + '"]:not(:disabled)');
                if (again) again.focus();
            }
            document.getElementById('discussModelEffortDesc').textContent = selection.model
                ? (describedBy || 'Model default')
                : 'Set by your ' + discussAgentLabel() + ' configuration';
            var foot = document.getElementById('discussModelFoot');
            if (_discussModelNote) {
                foot.textContent = _discussModelNote;
                foot.dataset.kind = 'warning';
            } else {
                foot.dataset.kind = 'info';
                foot.textContent = selection.model
                    ? 'Pinned to this conversation, from your next message on.'
                    : 'Prosview sends no model, so ' + discussAgentLabel() + ' uses its own configuration.';
            }
        }

        // Codex advertises a different ladder per model, so the row shows every
        // rung any model offers and greys out the ones this model does not.
        function discussModelEffortUnion(catalog) {
            var seen = Object.create(null);
            var union = [];
            (catalog.models || []).forEach(function(row) {
                (row.efforts || []).forEach(function(entry) {
                    if (seen[entry.id]) return;
                    seen[entry.id] = true;
                    union.push(entry);
                });
            });
            return union;
        }

        var _discussModelNote = '';

        function saveDiscussModel(selection, note) {
            if (!_discussConversationId) return;
            _discussModelNote = note || '';
            if (_discussSnapshot) _discussSnapshot.model = {model: selection.model, effort: selection.effort};
            renderDiscussModelChip();
            if (_discussModelPickerOpen) renderDiscussModelPicker();
            discussApi(
                '/api/discuss/conversations/' + encodeURIComponent(_discussConversationId) + '/model',
                {model: selection.model, effort: selection.effort}
            ).then(function(data) {
                if (_discussSnapshot) _discussSnapshot.model = data.model || {model: '', effort: ''};
                renderDiscussModelChip();
                document.getElementById('discussAnnouncement').textContent =
                    'Model set to ' + document.getElementById('discussModelName').textContent
                    + (selection.effort ? ' at ' + selection.effort + ' effort' : '');
            }).catch(function(error) {
                renderDiscussError(error.message);
                scheduleDiscussSnapshot();
            });
        }

        function chooseDiscussModel(modelId) {
            var catalog = discussModelCatalog();
            if (!catalog) return;
            var selection = discussModelSelection();
            if (!modelId) {
                saveDiscussModel({model: '', effort: ''}, '');
                return;
            }
            var ladder = discussModelEfforts(catalog, modelId);
            // Carry forward whatever the chip was showing, including an effort
            // that came from the agent's own configuration: picking a model
            // should not quietly change how hard it thinks, and leaving it
            // unpinned would leave the chip claiming an effort nobody sent.
            var effort = selection.effort || (catalog.default && catalog.default.effort) || '';
            var supported = ladder.some(function(entry) { return entry.id === effort; });
            var note = '';
            if (!effort) {
                effort = '';
            } else if (!supported) {
                // Keep the writer's intent -- they asked for the hardest
                // thinking this model has -- rather than silently dropping to
                // the model's own default.
                var wanted = effort;
                effort = ladder.length ? ladder[ladder.length - 1].id : '';
                var row = discussModelRow(catalog, modelId);
                note = wanted + ' is not available on ' + ((row && row.label) || modelId)
                    + (effort ? ' — using ' + effort + '.' : '.');
            }
            saveDiscussModel({model: modelId, effort: effort}, note);
        }

        function chooseDiscussEffort(effortId) {
            var selection = discussModelSelection();
            if (!selection.model) return;
            saveDiscussModel({model: selection.model, effort: effortId}, '');
        }

        function toggleDiscussModelPicker() {
            if (_discussModelPickerOpen) {
                closeDiscussModelPicker();
                return;
            }
            var catalog = discussModelCatalog();
            if (!catalog || _discussModelBusy) return;
            _discussModelNote = '';
            _discussModelPickerOpen = true;
            document.getElementById('discussModelPicker').hidden = false;
            document.getElementById('discussModelChip').setAttribute('aria-expanded', 'true');
            renderDiscussModelPicker();
            var first = document.querySelector('#discussModelList .discuss-model-option[aria-checked="true"]')
                || document.querySelector('#discussModelList .discuss-model-option');
            if (first) first.focus();
        }

        function closeDiscussModelPicker(options) {
            if (!_discussModelPickerOpen) return;
            _discussModelPickerOpen = false;
            _discussModelNote = '';
            var picker = document.getElementById('discussModelPicker');
            if (picker) picker.hidden = true;
            var chip = document.getElementById('discussModelChip');
            if (chip) {
                chip.setAttribute('aria-expanded', 'false');
                if (options && options.focus && !chip.hidden && !chip.disabled) chip.focus();
            }
        }

        // mousedown, not click: choosing an option rebuilds the list, so by the
        // time a click bubbles to the document its target is detached and no
        // longer answers closest() -- which dismissed the picker on every
        // selection made inside it.
        document.addEventListener('mousedown', function(event) {
            if (!_discussModelPickerOpen) return;
            if (event.target.closest && event.target.closest('.discuss-model-wrap')) return;
            closeDiscussModelPicker();
        });

        // Only while the picker is open: the dock otherwise leaves Escape to
        // whatever the writer is inside, and an always-on handler would steal
        // the key from the editor underneath.
        document.addEventListener('keydown', function(event) {
            if (!_discussModelPickerOpen || event.key !== 'Escape') return;
            event.preventDefault();
            event.stopPropagation();
            closeDiscussModelPicker({focus: true});
        });

        document.getElementById('discussModelPicker').addEventListener('keydown', function(event) {
            if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
            var options = Array.prototype.slice.call(
                this.querySelectorAll('.discuss-model-option, .discuss-model-effort-option:not(:disabled)')
            );
            var index = options.indexOf(document.activeElement);
            if (index < 0) return;
            event.preventDefault();
            var step = event.key === 'ArrowDown' ? 1 : options.length - 1;
            options[(index + step) % options.length].focus();
        });

        function discussMentionAtCaret() {
            var input = document.getElementById('discussInput');
            if (input.selectionStart !== input.selectionEnd) return null;
            var before = input.value.slice(0, input.selectionStart);
            var match = before.match(/(^|\s)@([^\s@]*)$/);
            if (!match) return null;
            return {start: before.length - match[2].length - 1, end: input.selectionStart, query: match[2]};
        }

        function discussContextCandidates() {
            if (_discussContextCandidateCache) return _discussContextCandidateCache;
            var candidates = [];
            function visit(nodes) {
                (nodes || []).forEach(function(node) {
                    if (!node.path) return;
                    var kind = node.is_file ? 'file' : 'folder';
                    if ((!node.is_file && node.attachable) || (node.is_file && node.attachable)) {
                        candidates.push({kind: kind, path: node.path, name: node.name || node.path});
                    }
                    if (!node.is_file) visit(node.children);
                });
            }
            visit(repositoryTree);
            _discussContextCandidateCache = candidates;
            return _discussContextCandidateCache;
        }

        function setDiscussContextExpanded(expanded) {
            document.getElementById('discussContextButton').setAttribute('aria-expanded', String(expanded));
            document.getElementById('discussInput').setAttribute('aria-expanded', String(expanded));
        }

        function positionDiscussContextPicker() {
            var picker = document.getElementById('discussContextPicker');
            if (!picker || picker.hidden) return;
            var composer = document.getElementById('discussComposerArea');
            var options = document.getElementById('discussContextOptions');
            var zoom = parseFloat(getComputedStyle(document.body).zoom) || 1;
            var availableRenderedHeight = Math.max(96, composer.getBoundingClientRect().top - 8);
            var maxLogicalHeight = Math.min(330, availableRenderedHeight / zoom);
            picker.style.maxHeight = maxLogicalHeight + 'px';
            options.style.maxHeight = Math.max(70, maxLogicalHeight - 42) + 'px';
        }

        function closeDiscussContextPicker() {
            var picker = document.getElementById('discussContextPicker');
            if (!picker) return;
            picker.hidden = true;
            _discussContextChoices = [];
            _discussContextActiveIndex = 0;
            _discussMentionRange = null;
            var input = document.getElementById('discussInput');
            input.removeAttribute('aria-activedescendant');
            setDiscussContextExpanded(false);
        }

        function renderDiscussContextOptions() {
            var mention = discussMentionAtCaret();
            if (!mention) { closeDiscussContextPicker(); return; }
            _discussMentionRange = mention;
            var query = mention.query.toLowerCase();
            _discussContextChoices = discussContextCandidates().filter(function(candidate) {
                if (_discussAttachments.some(function(item) { return item.kind === candidate.kind && item.path === candidate.path; })) return false;
                return !query || candidate.path.toLowerCase().includes(query) || candidate.name.toLowerCase().includes(query);
            }).slice(0, 12);
            _discussContextActiveIndex = Math.min(_discussContextActiveIndex, Math.max(0, _discussContextChoices.length - 1));
            var options = document.getElementById('discussContextOptions'); options.replaceChildren();
            if (!_discussContextChoices.length) {
                options.appendChild(elementWith('discuss-context-empty', query ? 'No matching files or folders' : 'Type to search files and folders'));
            }
            _discussContextChoices.forEach(function(choice, index) {
                var option = document.createElement('button');
                option.type = 'button'; option.className = 'discuss-context-option'; option.id = 'discussContextOption' + index;
                option.setAttribute('role', 'option'); option.setAttribute('aria-selected', String(index === _discussContextActiveIndex));
                option.dataset.path = choice.path; option.dataset.kind = choice.kind;
                var icon = elementWith('discuss-context-option-icon', choice.kind === 'file' ? '▤' : '▸'); icon.setAttribute('aria-hidden', 'true');
                option.appendChild(icon); option.appendChild(elementWith('discuss-context-option-path', choice.path));
                option.onmousedown = function(event) { event.preventDefault(); };
                option.onclick = function() { selectDiscussContextOption(index); };
                options.appendChild(option);
            });
            var picker = document.getElementById('discussContextPicker'); picker.hidden = false;
            positionDiscussContextPicker();
            setDiscussContextExpanded(true);
            updateDiscussContextActiveOption();
        }

        function updateDiscussContextActiveOption() {
            var input = document.getElementById('discussInput');
            document.querySelectorAll('#discussContextOptions [role="option"]').forEach(function(option, index) {
                option.setAttribute('aria-selected', String(index === _discussContextActiveIndex));
            });
            if (_discussContextChoices.length) {
                var active = document.getElementById('discussContextOption' + _discussContextActiveIndex);
                input.setAttribute('aria-activedescendant', active.id);
                active.scrollIntoView({block: 'nearest'});
            } else input.removeAttribute('aria-activedescendant');
        }

        function selectDiscussContextOption(index) {
            var choice = _discussContextChoices[index];
            var input = document.getElementById('discussInput');
            var mention = _discussMentionRange;
            if (!choice || !mention) return;
            if (!_discussAttachments.some(function(item) { return item.kind === choice.kind && item.path === choice.path; })) {
                _discussAttachments.push({kind: choice.kind, path: choice.path});
            }
            input.value = input.value.slice(0, mention.start) + input.value.slice(mention.end);
            input.setSelectionRange(mention.start, mention.start);
            closeDiscussContextPicker();
            renderDiscussContext();
            document.getElementById('discussAnnouncement').textContent = 'Attached ' + choice.path;
            input.focus();
        }

        function openDiscussContextPicker() {
            var input = document.getElementById('discussInput');
            input.focus();
            var mention = discussMentionAtCaret();
            if (!mention) {
                var start = input.selectionStart;
                var prefix = start > 0 && !/\s/.test(input.value.charAt(start - 1)) ? ' @' : '@';
                input.setRangeText(prefix, start, input.selectionEnd, 'end');
            }
            _discussContextActiveIndex = 0;
            renderDiscussContextOptions();
        }

        function openNewDiscussConversationDialog() {
            var button = document.getElementById('discussNewConversation');
            if (!_discussConversationId || button.disabled) return;
            var dialog = document.getElementById('discussNewConversationDialog');
            var error = document.getElementById('discussNewConversationError');
            var status = document.getElementById('discussNewConversationStatus');
            var confirm = document.getElementById('discussNewConversationConfirm');
            var cancel = document.getElementById('discussNewConversationCancel');
            error.hidden = true; error.textContent = '';
            status.hidden = true; status.textContent = '';
            dialog.setAttribute('aria-busy', 'false');
            confirm.disabled = false; confirm.textContent = 'Start new conversation';
            cancel.disabled = false;
            dialog.showModal();
            cancel.focus();
        }

        function confirmNewDiscussConversation(event) {
            event.preventDefault();
            if (!_discussConversationId) return;
            var dialog = document.getElementById('discussNewConversationDialog');
            var confirm = document.getElementById('discussNewConversationConfirm');
            var cancel = document.getElementById('discussNewConversationCancel');
            var status = document.getElementById('discussNewConversationStatus');
            var error = document.getElementById('discussNewConversationError');
            confirm.disabled = true; confirm.textContent = 'Starting…';
            cancel.disabled = true;
            dialog.setAttribute('aria-busy', 'true');
            status.hidden = true; status.textContent = '';
            error.hidden = true; error.textContent = '';
            document.getElementById('discussAnnouncement').textContent = 'Starting a new conversation';
            var slowTimer = setTimeout(function() {
                status.textContent = 'Still starting… Prosview is waiting for the local conversation reset to finish.';
                status.hidden = false;
            }, 1200);
            discussApi('/api/discuss/conversations/' + encodeURIComponent(_discussConversationId) + '/new', {})
                .then(function(data) {
                    _discussSnapshot = data.snapshot;
                    _discussSelection = '';
                    _discussSelectionRange = null;
                    _discussSelectionSnapshot = null;
                    _discussSelectionSourceTaskId = null;
                    _discussLiveDocument = null;
                    _discussPendingAction = null;
                    _discussRepositoryAction = null;
                    _discussRetryOfTaskId = null;
                    _discussSelectedSkill = null;
                    _discussAutoRun = false;
                    _discussAttachments = [];
                    _discussIncludeCurrentDocument = false;
                    if (!document.getElementById('discussInput').value) _discussDraftDocument = null;
                    saveDiscussDraft();
                    closeDiscussContextPicker();
                    renderDiscussContext();
                    renderDiscussTaskMode();
                    renderDiscussSnapshot();
                    dialog.close('confirmed');
                    document.getElementById('discussAnnouncement').textContent = 'New conversation started';
                    document.getElementById('discussInput').focus();
                })
                .catch(function(requestError) {
                    status.hidden = true; status.textContent = '';
                    error.textContent = requestError.message;
                    error.hidden = false;
                    confirm.textContent = 'Try again';
                    document.getElementById('discussAnnouncement').textContent = 'New conversation failed. ' + requestError.message;
                })
                .finally(function() {
                    clearTimeout(slowTimer);
                    dialog.setAttribute('aria-busy', 'false');
                    confirm.disabled = false;
                    cancel.disabled = false;
                    if (!error.hidden) confirm.focus();
                });
        }

        document.getElementById('discussNewConversationDialog').addEventListener('close', function() {
            if (this.returnValue !== 'confirmed') document.getElementById('discussNewConversation').focus();
        });
        document.getElementById('discussNewConversationDialog').addEventListener('cancel', function(event) {
            if (this.getAttribute('aria-busy') !== 'true') return;
            event.preventDefault();
            document.getElementById('discussAnnouncement').textContent = 'Wait for the conversation reset to finish';
        });
        document.getElementById('discussHistoryDialog').addEventListener('close', function() {
            if (this.returnValue !== 'opened') document.getElementById('discussHistory').focus();
        });

        function sendDiscussQuestion() {
            var input = document.getElementById('discussInput');
            var question = input.value.trim();
            var button = document.getElementById('discussSend');
            if (_discussOpenFailed) { openDiscuss(_discussReturnFocus); return; }
            if (_discussPendingAction) { runDiscussSelectionAction(); return; }
            if (_discussRepositoryAction) { runDiscussRepositoryAction(); return; }
            if (!question || !_discussConversationId || button.disabled) return;
            var turnDocument = discussTurnDocument();
            if (!turnDocument) return;
            var requestId = (crypto.randomUUID ? crypto.randomUUID() : 'pv-' + Date.now() + '-' + Math.random().toString(36).slice(2));
            clearDiscussError();
            button.disabled = true;
            button.textContent = 'Sending…';
            document.getElementById('discussAnnouncement').textContent = 'Sending question…';
            discussApi('/api/discuss/conversations/' + encodeURIComponent(_discussConversationId) + '/questions', {
                client_request_id: requestId,
                question: question,
                document: turnDocument,
                selection: _discussSelection,
                selection_range: _discussSelectionRange,
                live_document: _discussLiveDocument,
                attachments: _discussAttachments,
                include_current_document: _discussIncludeCurrentDocument,
                skill: _discussSelectedSkill
            }).then(function() {
                rememberDiscussInstruction(question); input.value = '';
                if (!_discussSelection && !_discussIncludeCurrentDocument) _discussDraftDocument = null;
                saveDiscussDraft(); _discussSelectedSkill = null;
                var restoredLegacyDraft = activateLegacyDiscussDraft(discussDocument(), _discussAgent);
                closeDiscussContextPicker(); renderDiscussContext(); renderDiscussTaskMode(); scheduleDiscussSnapshot();
                document.getElementById('discussAnnouncement').textContent = restoredLegacyDraft
                    ? 'Question queued. Restored another saved draft for this file.'
                    : _discussSelection
                    ? 'Question queued. Selection remains attached for follow-up questions.'
                    : 'Question queued';
            }).catch(function(error) {
                renderDiscussError(error.message, {kind: error.name === 'NetworkError' ? 'transport' : 'request'});
                document.getElementById('discussAnnouncement').textContent = 'Question was not confirmed. ' + error.message;
            }).finally(function() {
                button.disabled = false;
                renderDiscussTaskMode();
                var active = document.activeElement;
                if (active === input || active === button || active === document.body) input.focus();
            });
        }

        function startDiscussRepositoryAction(actionId) {
            _discussRepositoryAction = actionId;
            if (!_discussDraftDocument) _discussDraftDocument = discussDocument();
            _discussPendingAction = null;
            _discussSelectedSkill = null;
            renderDiscussTaskMode();
            renderDiscussSnapshot();
            var input = document.getElementById('discussInput');
            input.focus();
            document.getElementById('discussAnnouncement').textContent = selectionActionLabel(actionId) + ' selected';
        }

        function cancelDiscussRepositoryAction() {
            if (!_discussRepositoryAction) return;
            _discussRepositoryAction = null;
            if (!document.getElementById('discussInput').value && !_discussSelection) _discussDraftDocument = null;
            renderDiscussTaskMode();
            renderDiscussSnapshot();
            document.getElementById('discussInput').focus();
            document.getElementById('discussAnnouncement').textContent = 'Choose a story-aware action';
        }

        function runDiscussRepositoryAction(actionOverride, verifyOfTaskId) {
            var actionId = actionOverride || _discussRepositoryAction;
            var input = document.getElementById('discussInput');
            var question = input.value.trim();
            var button = document.getElementById('discussSend');
            if (!actionId || !_discussConversationId || button.disabled) return;
            var turnDocument = discussTurnDocument();
            if (!turnDocument) return;
            if (actionId === 'canon_refactor' && !question) {
                input.focus();
                document.getElementById('discussAnnouncement').textContent = 'Describe the old and new canon fact first';
                return;
            }
            var requestId = (crypto.randomUUID ? crypto.randomUUID() : 'pv-' + Date.now() + '-' + Math.random().toString(36).slice(2));
            clearDiscussError();
            button.disabled = true;
            button.textContent = 'Starting…';
            document.getElementById('discussAnnouncement').textContent = 'Starting ' + selectionActionLabel(actionId).toLowerCase();
            renderDiscussSnapshot();
            document.getElementById('discussLog').setAttribute('aria-busy', 'true');
            discussApi('/api/discuss/conversations/' + encodeURIComponent(_discussConversationId) + '/questions', {
                client_request_id: requestId,
                question: question,
                document: turnDocument,
                action_id: actionId,
                verify_of_task_id: verifyOfTaskId || ''
            }).then(function() {
                if (question) rememberDiscussInstruction(question);
                input.value = '';
                _discussRepositoryAction = null;
                _discussDraftDocument = null;
                saveDiscussDraft(); renderDiscussTaskMode(); scheduleDiscussSnapshot();
                document.getElementById('discussAnnouncement').textContent = selectionActionLabel(actionId) + ' queued. This scan cannot change manuscript files.';
            }).catch(function(error) {
                button.disabled = false;
                renderDiscussTaskMode();
                renderDiscussSnapshot();
                renderDiscussError(error.message);
            }).finally(function() {
                button.disabled = false;
                document.getElementById('discussLog').setAttribute('aria-busy', 'false');
                renderDiscussTaskMode();
                input.focus();
            });
        }

        function runDiscussSelectionAction(
            actionOverride, selectionOverride, rangeOverride, liveDocumentOverride,
            retryCount, retryOfOverride, selectionSnapshotOverride, selectionSourceTaskOverride
        ) {
            var input = document.getElementById('discussInput');
            var button = document.getElementById('discussSend');
            var actionId = actionOverride || _discussPendingAction;
            var selection = selectionOverride || _discussSelection;
            var selectionRange = rangeOverride || _discussSelectionRange;
            var selectionSnapshot = selectionSnapshotOverride || _discussSelectionSnapshot;
            var selectionSourceTaskId = selectionSourceTaskOverride || _discussSelectionSourceTaskId;
            var liveDocument = liveDocumentOverride || _discussLiveDocument;
            var retryOfTaskId = retryOfOverride || _discussRetryOfTaskId;
            var turnDocument = discussTurnDocument();
            if (!actionId || !selection || !_discussConversationId) return;
            if (!turnDocument) return;
            if (button.disabled) {
                retryCount = Number(retryCount || 0);
                if (retryCount < 100) {
                    setTimeout(function() {
                        runDiscussSelectionAction(
                            actionId, selection, selectionRange, liveDocument, retryCount + 1,
                            retryOfTaskId, selectionSnapshot, selectionSourceTaskId
                        );
                    }, 50);
                }
                return;
            }
            var custom = input.value.trim();
            var requestId = (crypto.randomUUID ? crypto.randomUUID() : 'pv-' + Date.now() + '-' + Math.random().toString(36).slice(2));
            clearDiscussError();
            // Only a rewrite has a proposal to open when it lands.
            if (!discussIsReadingAction(actionId)) _discussAutoReviewRequests[requestId] = true;
            button.disabled = true;
            discussApi('/api/discuss/conversations/' + encodeURIComponent(_discussConversationId) + '/questions', {
                client_request_id: requestId,
                question: '',
                document: turnDocument,
                selection: selection,
                selection_range: selectionRange,
                selection_snapshot: selectionSnapshot,
                selection_source_task_id: selectionSourceTaskId,
                live_document: liveDocument,
                attachments: _discussAttachments,
                include_current_document: _discussIncludeCurrentDocument,
                action_id: actionId,
                custom_instruction: custom,
                skill: _discussSelectedSkill,
                retry_of_task_id: retryOfTaskId
            }).then(function() {
                if (custom) rememberDiscussInstruction(custom);
                input.value = _discussAutoRun ? _discussPreservedDraft : '';
                _discussPendingAction = null; _discussRetryOfTaskId = null; _discussSelectedSkill = null; _discussAutoRun = false; saveDiscussDraft();
                // A reading pass leaves its subject attached: the obvious next
                // move is "say more about that", and it should not have to be
                // reselected to ask.
                if (!discussIsReadingAction(actionId)) {
                    _discussSelection = ''; _discussSelectionRange = null; _discussSelectionSnapshot = null;
                    _discussSelectionSourceTaskId = null; _discussLiveDocument = null;
                    _discussDraftDocument = null;
                }
                saveDiscussDraft(); closeDiscussContextPicker(); renderDiscussContext(); renderDiscussTaskMode(); scheduleDiscussSnapshot();
                document.getElementById('discussAnnouncement').textContent = selectionActionLabel(actionId) + ' queued. The manuscript will not change.';
            }).catch(function(error) {
                delete _discussAutoReviewRequests[requestId];
                renderDiscussError(error.message);
            }).finally(function() {
                button.disabled = false;
                var proposalPanel = document.getElementById('aiProposalPanel');
                if (proposalPanel && !proposalPanel.hidden) proposalPanel.focus({preventScroll: true});
                else input.focus();
            });
        }

        // A scene pass takes no selection: the server reads the scene this
        // conversation is looking at. Nothing to highlight, nothing to type --
        // an action that needs a paragraph of setup first is not one anybody
        // reaches for.
        function runDiscussScenePass(actionId) {
            var button = document.getElementById('discussSend');
            var turnDocument = discussTurnDocument();
            if (!actionId || !_discussConversationId || button.disabled) return;
            if (!turnDocument || turnDocument.kind !== 'scene') {
                renderDiscussError('A scene pass reads a manuscript scene. Open one first.');
                return;
            }
            var requestId = (crypto.randomUUID ? crypto.randomUUID() : 'pv-' + Date.now() + '-' + Math.random().toString(36).slice(2));
            clearDiscussError();
            button.disabled = true;
            discussApi('/api/discuss/conversations/' + encodeURIComponent(_discussConversationId) + '/questions', {
                client_request_id: requestId,
                question: '',
                document: turnDocument,
                action_id: actionId,
                action_scope: 'scene',
                attachments: [],
                include_current_document: false
            }).then(function() {
                document.getElementById('discussAnnouncement').textContent =
                    selectionActionLabel(actionId) + ' running on this scene. The manuscript will not change.';
                scheduleDiscussSnapshot();
            }).catch(function(error) {
                renderDiscussError(error.message);
            }).finally(function() {
                button.disabled = false;
            });
        }

        function stopDiscussTurn() {
            if (!_discussSnapshot || !_discussSnapshot.active_turn_id) return;
            var button = document.getElementById('discussStop');
            if (button.disabled) return;
            button.disabled = true;
            button.textContent = 'Stopping…';
            document.getElementById('discussAnnouncement').textContent = 'Stopping ' + discussAgentLabel();
            discussApi('/api/discuss/conversations/' + encodeURIComponent(_discussConversationId) + '/turns/' + encodeURIComponent(_discussSnapshot.active_turn_id) + '/stop', {})
                .then(scheduleDiscussSnapshot).catch(function(error) {
                    button.disabled = false;
                    button.textContent = 'Stop Codex';
                    renderDiscussError(error.message);
                });
        }

        document.getElementById('discussInput').addEventListener('keydown', function(event) {
            var pickerOpen = !document.getElementById('discussContextPicker').hidden;
            if (pickerOpen && event.key === 'ArrowDown') {
                event.preventDefault();
                if (_discussContextChoices.length) _discussContextActiveIndex = (_discussContextActiveIndex + 1) % _discussContextChoices.length;
                updateDiscussContextActiveOption();
            } else if (pickerOpen && event.key === 'ArrowUp') {
                event.preventDefault();
                if (_discussContextChoices.length) _discussContextActiveIndex = (_discussContextActiveIndex + _discussContextChoices.length - 1) % _discussContextChoices.length;
                updateDiscussContextActiveOption();
            } else if (pickerOpen && (event.key === 'Enter' || event.key === 'Tab') && !event.isComposing) {
                event.preventDefault(); selectDiscussContextOption(_discussContextActiveIndex);
            } else if (pickerOpen && event.key === 'Escape') {
                event.preventDefault(); event.stopPropagation(); closeDiscussContextPicker();
            } else if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); sendDiscussQuestion(); }
        });
        document.getElementById('discussInput').addEventListener('input', function() {
            var beforeDocument = discussDocumentKey(discussTurnDocument());
            saveDiscussDraft();
            if (activateLegacyDiscussDraft(discussDocument(), _discussAgent)) {
                document.getElementById('discussAnnouncement').textContent =
                    'Restored another saved draft for this file.';
            }
            if (discussDocumentKey(discussTurnDocument()) !== beforeDocument) renderDiscussContext();
            renderDiscussContextOptions();
        });
        document.addEventListener('mousedown', function(event) {
            var picker = document.getElementById('discussContextPicker');
            if (picker.hidden || picker.contains(event.target) || event.target === document.getElementById('discussContextButton') || event.target === document.getElementById('discussInput')) return;
            closeDiscussContextPicker();
        });
        window.addEventListener('resize', positionDiscussContextPicker);
        window.addEventListener('focus', _syncDiscussAmbientSignals);
        window.addEventListener('blur', _syncDiscussAmbientSignals);
        if (window.visualViewport) window.visualViewport.addEventListener('resize', positionDiscussContextPicker);
        // Escape deliberately does not close the dock: it belongs to whatever
        // the writer is inside -- the composer's context picker handles its own,
        // and the dialogs are <dialog> elements that close natively. Taking it
        // for the panel stole the key from the editor underneath.
        document.getElementById('discussLog').addEventListener('scroll', function() {
            if (discussIsAtBottom(this)) document.getElementById('discussNewActivity').hidden = true;
        });
        (function initDiscussResize() {
            var handle = document.getElementById('discussResizeHandle'); var dragging = false; var startX = 0; var startWidth = 0;
            function setWidth(width) {
                var bounds = workspaceDockWidthBounds(340);
                width = Math.max(bounds.min, Math.min(bounds.max, width));
                var zoomed = document.documentElement.dataset.cssZoom === 'true';
                document.documentElement.style.setProperty(zoomed ? '--css-zoom-dock-width' : '--utility-dock-w', width + 'px');
                var zoom = workspaceZoomFactor();
                updateSeparatorValue(handle, width * zoom, bounds.min * zoom, bounds.max * zoom);
            }
            function currentWidth() {
                var rendered = document.getElementById('discussPanel').getBoundingClientRect().width;
                if (rendered > 0) return rendered / workspaceZoomFactor();
                return parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--utility-dock-w')) || 504;
            }
            handle.addEventListener('mousedown', function(event) { dragging = true; startX = event.clientX; startWidth = currentWidth(); handle.classList.add('is-dragging'); document.body.style.userSelect = 'none'; event.preventDefault(); });
            document.addEventListener('mousemove', function(event) { if (!dragging) return; setWidth(startWidth + (startX - event.clientX) / workspaceZoomFactor()); });
            document.addEventListener('mouseup', function() { if (!dragging) return; dragging = false; handle.classList.remove('is-dragging'); document.body.style.userSelect = ''; });
            handle.addEventListener('keydown', function(event) {
                if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
                var bounds = workspaceDockWidthBounds(340);
                var current = currentWidth();
                var next = current;
                if (event.key === 'Home') next = bounds.min;
                else if (event.key === 'End') next = bounds.max;
                else next += (event.key === 'ArrowRight' ? 1 : -1) * (event.shiftKey ? 50 : 20);
                setWidth(next);
                event.preventDefault();
            });
            var initialBounds = workspaceDockWidthBounds(340);
            var initialWidth = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--utility-dock-w')) || 504;
            setWidth(Math.min(initialBounds.max, Math.max(initialBounds.min, initialWidth)));
            window.addEventListener('resize', function() { setWidth(currentWidth()); });
            window.addEventListener('proseview:workspace-metrics', function() { setWidth(currentWidth()); });
        })();
