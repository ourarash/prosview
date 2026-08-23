        // ── File browser mutations ───────────────────────────────────────────────

        var _sidebarMenuReturnFocus = null;
        var _sidebarDeleteNode = null;
        var _sidebarCreateKind = 'file';

        function sidebarWalk(nodes, visit) {
            (nodes || []).forEach(function(node) {
                visit(node);
                if (!node.is_file) sidebarWalk(node.children || [], visit);
            });
        }

        function sidebarFindNode(path) {
            var found = null;
            sidebarWalk(sidebarTree, function(node) {
                if (!found && node.path === path) found = node;
            });
            return found;
        }

        function sidebarCurrentPath() {
            var active = document.querySelector('#sidebarTree .file-link.active');
            if (active && active.dataset.path) return active.dataset.path;
            var route = typeof parseHashRoute === 'function' ? parseHashRoute() : null;
            if (!route) return '';
            if (route.kind === 'file') return route.arg;
            if (route.kind === 'scene') {
                var match = null;
                sidebarWalk(sidebarTree, function(node) {
                    if (!match && node.is_file && node.scene_path === route.arg) match = node.path;
                });
                return match || '';
            }
            return '';
        }

        function sidebarRunAfterDirtyGuard(action) {
            if (typeof guardDirtySceneNavigation === 'function' && guardDirtySceneNavigation()) {
                showUnsavedDialog({onContinue: action});
                return;
            }
            action();
        }

        function sidebarCloseMenus(restoreFocus) {
            var createMenu = document.getElementById('sidebarCreateMenu');
            var contextMenu = document.getElementById('sidebarContextMenu');
            var createButton = document.getElementById('sidebarCreateBtn');
            if (createMenu) createMenu.hidden = true;
            if (contextMenu) contextMenu.hidden = true;
            if (createButton) createButton.setAttribute('aria-expanded', 'false');
            document.querySelectorAll('.sidebar-row-more[aria-expanded="true"]').forEach(function(button) {
                button.setAttribute('aria-expanded', 'false');
            });
            if (restoreFocus && _sidebarMenuReturnFocus && _sidebarMenuReturnFocus.isConnected) {
                _sidebarMenuReturnFocus.focus({preventScroll: true});
            }
            _sidebarMenuReturnFocus = null;
        }

        function sidebarFocusMenu(menu) {
            var first = menu && menu.querySelector('button:not([disabled])');
            if (first) first.focus();
        }

        function sidebarPositionContextMenu(menu, x, y) {
            menu.hidden = false;
            menu.style.left = Math.max(6, Math.min(x, window.innerWidth - menu.offsetWidth - 6)) + 'px';
            menu.style.top = Math.max(6, Math.min(y, window.innerHeight - menu.offsetHeight - 6)) + 'px';
        }

        function sidebarMenuButton(label, action, onClick) {
            var button = document.createElement('button');
            button.type = 'button';
            button.role = 'menuitem';
            button.textContent = label;
            button.dataset.action = action;
            button.addEventListener('click', function(event) {
                event.stopPropagation();
                sidebarCloseMenus(false);
                onClick();
            });
            return button;
        }

        function sidebarOpenNodeMenu(node, anchor, point) {
            var menu = document.getElementById('sidebarContextMenu');
            if (!menu) return;
            sidebarCloseMenus(false);
            _sidebarMenuReturnFocus = anchor;
            menu.innerHTML = '';
            menu.setAttribute('aria-label', 'Actions for ' + node.name);
            if (!node.is_file) {
                menu.appendChild(sidebarMenuButton('New file here', 'new-file', function() {
                    sidebarOpenCreateDialog('file', node.path);
                }));
                menu.appendChild(sidebarMenuButton('New folder here', 'new-folder', function() {
                    sidebarOpenCreateDialog('folder', node.path);
                }));
            }
            var protectedRoot = Number(anchor.dataset.depth || '0') === 0;
            if (!protectedRoot) {
                if (!node.is_file) menu.appendChild(document.createElement('hr'));
                menu.appendChild(sidebarMenuButton('Rename', 'rename', function() {
                    sidebarRunAfterDirtyGuard(function() { sidebarBeginRename(node, anchor); });
                }));
                menu.appendChild(sidebarMenuButton('Delete…', 'delete', function() {
                    sidebarRunAfterDirtyGuard(function() { sidebarOpenDeleteDialog(node); });
                }));
            }
            var x = point ? point.x : anchor.getBoundingClientRect().right - 8;
            var y = point ? point.y : anchor.getBoundingClientRect().bottom;
            sidebarPositionContextMenu(menu, x, y);
            anchor.setAttribute('aria-expanded', 'true');
            sidebarFocusMenu(menu);
        }

        function sidebarAttachRowActions(node, li, primary, depth) {
            // The project config file is visible for preview but intentionally
            // has no mutation actions. Configured folder roots still get the
            // useful "create here" actions below.
            if (depth === 0 && node.is_file) return;
            var more = document.createElement('button');
            more.type = 'button';
            more.className = 'sidebar-row-more';
            more.dataset.depth = String(depth);
            more.setAttribute('aria-label', 'More actions for ' + node.name);
            more.setAttribute('aria-haspopup', 'menu');
            more.setAttribute('aria-expanded', 'false');
            more.textContent = '…';
            more.addEventListener('click', function(event) {
                event.preventDefault();
                event.stopPropagation();
                sidebarOpenNodeMenu(node, more, null);
            });
            primary.addEventListener('contextmenu', function(event) {
                event.preventDefault();
                sidebarOpenNodeMenu(node, more, {x: event.clientX, y: event.clientY});
            });
            li.appendChild(more);
        }

        function sidebarFolderOptions(selectedPath) {
            var select = document.getElementById('sidebarCreateLocation');
            if (!select) return;
            select.innerHTML = '';
            sidebarWalk(sidebarTree, function(node) {
                if (node.is_file) return;
                var option = document.createElement('option');
                option.value = node.path;
                option.textContent = node.path.split('/').join(' / ');
                select.appendChild(option);
            });
            if (selectedPath && Array.from(select.options).some(function(option) { return option.value === selectedPath; })) {
                select.value = selectedPath;
            }
        }

        function sidebarDefaultCreateParent() {
            var current = sidebarCurrentPath();
            if (current) return current.split('/').slice(0, -1).join('/');
            var manuscript = (sidebarTree || []).find(function(node) { return !node.is_file && node.path === 'manuscript'; });
            return manuscript ? manuscript.path : ((sidebarTree || []).find(function(node) { return !node.is_file; }) || {}).path || '';
        }

        function sidebarCreatePreview() {
            var nameInput = document.getElementById('sidebarCreateName');
            var location = document.getElementById('sidebarCreateLocation');
            var preview = document.getElementById('sidebarCreatePreview');
            if (!nameInput || !location || !preview) return;
            var name = nameInput.value.trim();
            if (_sidebarCreateKind === 'file' && name && !name.toLowerCase().endsWith('.md')) name += '.md';
            preview.textContent = name ? 'Creates ' + location.value + '/' + name : 'Enter one name; folders are not created automatically.';
        }

        function sidebarOpenCreateDialog(kind, parent, dirtyAlreadyHandled) {
            if (!dirtyAlreadyHandled && typeof guardDirtySceneNavigation === 'function' && guardDirtySceneNavigation()) {
                showUnsavedDialog({onContinue: function() {
                    sidebarOpenCreateDialog(kind, parent, true);
                }});
                return;
            }
            var dialog = document.getElementById('sidebarCreateDialog');
            if (!dialog) return;
            _sidebarCreateKind = kind;
            var title = document.getElementById('sidebarCreateTitle');
            var input = document.getElementById('sidebarCreateName');
            var error = document.getElementById('sidebarCreateError');
            title.textContent = kind === 'file' ? 'New file' : 'New folder';
            input.value = '';
            input.placeholder = kind === 'file' ? 'Chapter notes' : 'Locations';
            error.hidden = true;
            sidebarFolderOptions(parent || sidebarDefaultCreateParent());
            sidebarCreatePreview();
            dialog.showModal();
            input.focus();
        }

        function sidebarApi(path, body) {
            return fetch(path, {
                method: 'POST',
                headers: pvHeaders(),
                body: JSON.stringify(body)
            }).then(function(response) {
                return response.json().catch(function() { return {}; }).then(function(data) {
                    if (!response.ok || !data.ok) throw new Error(data.error || 'File operation failed');
                    return data;
                });
            });
        }

        function sidebarStoreFlash(message) {
            try { sessionStorage.setItem('proseview-file-flash', message); } catch (error) {}
        }

        function sidebarFinishMutation(data, activeBefore, routeBefore) {
            var hash = window.location.hash;
            if (data.operation === 'create') {
                if (data.scene_path) {
                    hash = '#/scene/' + encodeURIComponent(data.scene_path);
                    try { sessionStorage.setItem('proseview-auto-edit-scene', data.scene_path); } catch (error) {}
                } else if (data.kind === 'file') {
                    hash = '#/file/' + encodeURIComponent(data.path);
                }
                sidebarStoreFlash('Created ' + data.path + '.');
            } else if (data.operation === 'rename') {
                var affected = activeBefore && (activeBefore === data.old_path || activeBefore.indexOf(data.old_path + '/') === 0);
                if (affected) {
                    var mapped = data.path + activeBefore.slice(data.old_path.length);
                    if (routeBefore && routeBefore.kind === 'scene' && activeBefore.endsWith(routeBefore.arg)) {
                        var prefix = activeBefore.slice(0, activeBefore.length - routeBefore.arg.length);
                        hash = '#/scene/' + encodeURIComponent(mapped.slice(prefix.length));
                    } else {
                        hash = '#/file/' + encodeURIComponent(mapped);
                    }
                }
                sidebarStoreFlash('Renamed to ' + data.path + '.');
            } else if (data.operation === 'delete') {
                if (activeBefore && (activeBefore === data.path || activeBefore.indexOf(data.path + '/') === 0)) {
                    hash = '#/tab/overview';
                }
                sidebarStoreFlash('Moved ' + data.path + ' to Proseview Trash.');
            }
            history.replaceState(history.state, '', hash || '#/tab/overview');
            location.reload();
        }

        function sidebarSubmitCreate() {
            var form = document.getElementById('sidebarCreateForm');
            if (!form.reportValidity()) return;
            var dialog = document.getElementById('sidebarCreateDialog');
            var confirm = document.getElementById('sidebarCreateConfirm');
            var error = document.getElementById('sidebarCreateError');
            var activeBefore = sidebarCurrentPath();
            var routeBefore = typeof parseHashRoute === 'function' ? parseHashRoute() : null;
            error.hidden = true;
            confirm.disabled = true;
            sidebarApi('/api/files/create', {
                parent: document.getElementById('sidebarCreateLocation').value,
                name: document.getElementById('sidebarCreateName').value,
                kind: _sidebarCreateKind
            }).then(function(data) {
                dialog.close();
                sidebarFinishMutation(data, activeBefore, routeBefore);
            }).catch(function(errorValue) {
                error.textContent = errorValue.message;
                error.hidden = false;
                confirm.disabled = false;
            });
        }

        function sidebarBeginRename(node, moreButton) {
            var li = moreButton.closest('li');
            var primary = li && li.querySelector(':scope > [role="treeitem"]');
            var label = primary && primary.querySelector('.sidebar-row-label');
            if (!primary || !label) return;
            var input = document.createElement('input');
            input.className = 'sidebar-inline-rename';
            input.setAttribute('aria-label', 'New name for ' + node.name);
            input.value = node.name;
            label.hidden = true;
            primary.appendChild(input);
            moreButton.hidden = true;
            var finished = false;
            function cancel() {
                if (finished) return;
                finished = true;
                input.remove();
                label.hidden = false;
                moreButton.hidden = false;
                primary.focus();
            }
            input.addEventListener('click', function(event) { event.stopPropagation(); });
            input.addEventListener('blur', cancel);
            input.addEventListener('keydown', function(event) {
                if (event.key === 'Escape') {
                    event.preventDefault();
                    cancel();
                } else if (event.key === 'Enter') {
                    event.preventDefault();
                    if (!input.value.trim()) return;
                    finished = true;
                    input.disabled = true;
                    var activeBefore = sidebarCurrentPath();
                    var routeBefore = typeof parseHashRoute === 'function' ? parseHashRoute() : null;
                    sidebarApi('/api/files/rename', {path: node.path, name: input.value}).then(function(data) {
                        sidebarFinishMutation(data, activeBefore, routeBefore);
                    }).catch(function(errorValue) {
                        finished = false;
                        input.disabled = false;
                        sidebarShowToast(errorValue.message);
                        input.focus();
                    });
                }
                event.stopPropagation();
            });
            input.focus();
            var extensionAt = node.is_file && node.name.toLowerCase().endsWith('.md') ? node.name.length - 3 : node.name.length;
            input.setSelectionRange(0, extensionAt);
        }

        function sidebarOpenDeleteDialog(node) {
            var dialog = document.getElementById('sidebarDeleteDialog');
            _sidebarDeleteNode = node;
            document.getElementById('sidebarDeleteTitle').textContent = node.is_file ? 'Delete file?' : 'Delete folder?';
            document.getElementById('sidebarDeleteDescription').textContent = node.is_file
                ? '“' + node.name + '” will disappear from the file browser.'
                : '“' + node.name + '” and everything inside it will disappear from the file browser.';
            document.getElementById('sidebarDeleteError').hidden = true;
            document.getElementById('sidebarDeleteConfirm').disabled = false;
            dialog.showModal();
        }

        function sidebarSubmitDelete() {
            if (!_sidebarDeleteNode) return;
            var node = _sidebarDeleteNode;
            var confirm = document.getElementById('sidebarDeleteConfirm');
            var error = document.getElementById('sidebarDeleteError');
            var activeBefore = sidebarCurrentPath();
            var routeBefore = typeof parseHashRoute === 'function' ? parseHashRoute() : null;
            confirm.disabled = true;
            error.hidden = true;
            sidebarApi('/api/files/delete', {path: node.path}).then(function(data) {
                document.getElementById('sidebarDeleteDialog').close();
                sidebarFinishMutation(data, activeBefore, routeBefore);
            }).catch(function(errorValue) {
                error.textContent = errorValue.message;
                error.hidden = false;
                confirm.disabled = false;
            });
        }

        function sidebarShowToast(message) {
            var toast = document.getElementById('sidebarFileToast');
            if (!toast || !message) return;
            toast.textContent = message;
            toast.hidden = false;
            setTimeout(function() { toast.hidden = true; }, 5000);
        }

        (function initSidebarFileManagement() {
            var createButton = document.getElementById('sidebarCreateBtn');
            var createMenu = document.getElementById('sidebarCreateMenu');
            var createForm = document.getElementById('sidebarCreateForm');
            var deleteForm = document.getElementById('sidebarDeleteForm');
            if (!createButton || !createMenu || !createForm || !deleteForm) return;

            createButton.addEventListener('click', function(event) {
                event.stopPropagation();
                var opening = createMenu.hidden;
                sidebarCloseMenus(false);
                if (opening) {
                    _sidebarMenuReturnFocus = createButton;
                    createMenu.hidden = false;
                    createButton.setAttribute('aria-expanded', 'true');
                    sidebarFocusMenu(createMenu);
                }
            });
            createMenu.addEventListener('click', function(event) {
                var button = event.target.closest('[data-create-kind]');
                if (!button) return;
                sidebarCloseMenus(false);
                sidebarOpenCreateDialog(button.dataset.createKind, sidebarDefaultCreateParent());
            });
            document.addEventListener('click', function(event) {
                if (!event.target.closest('.sidebar-action-menu') && !event.target.closest('.sidebar-row-more') && event.target !== createButton) {
                    sidebarCloseMenus(false);
                }
            });
            document.addEventListener('keydown', function(event) {
                var menu = event.target.closest && event.target.closest('.sidebar-action-menu');
                if (event.key === 'Escape' && menu) {
                    event.preventDefault();
                    sidebarCloseMenus(true);
                    return;
                }
                if (!menu || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
                var buttons = Array.from(menu.querySelectorAll('button:not([disabled])'));
                var index = buttons.indexOf(document.activeElement);
                if (event.key === 'Home') index = 0;
                else if (event.key === 'End') index = buttons.length - 1;
                else if (event.key === 'ArrowDown') index = (index + 1) % buttons.length;
                else index = (index - 1 + buttons.length) % buttons.length;
                if (buttons[index]) buttons[index].focus();
                event.preventDefault();
            });
            document.getElementById('sidebarCreateName').addEventListener('input', sidebarCreatePreview);
            document.getElementById('sidebarCreateLocation').addEventListener('change', sidebarCreatePreview);
            createForm.addEventListener('submit', function(event) {
                event.preventDefault();
                sidebarRunAfterDirtyGuard(sidebarSubmitCreate);
            });
            deleteForm.addEventListener('submit', function(event) {
                event.preventDefault();
                sidebarSubmitDelete();
            });
            document.querySelectorAll('.sidebar-file-dialog [data-dialog-cancel]').forEach(function(button) {
                button.addEventListener('click', function() { button.closest('dialog').close(); });
            });

            try {
                var flash = sessionStorage.getItem('proseview-file-flash');
                if (flash) {
                    sessionStorage.removeItem('proseview-file-flash');
                    sidebarShowToast(flash);
                }
            } catch (error) {}
        })();

        window.addEventListener('proseview:editor-ready', function() {
            var requested = '';
            try { requested = sessionStorage.getItem('proseview-auto-edit-scene') || ''; } catch (error) {}
            var route = typeof parseHashRoute === 'function' ? parseHashRoute() : null;
            if (!requested || !route || route.kind !== 'scene' || route.arg !== requested) return;
            try { sessionStorage.removeItem('proseview-auto-edit-scene'); } catch (error) {}
            setTimeout(function() {
                if (!window._pmEditMode && typeof toggleSceneEdit === 'function') toggleSceneEdit();
            }, 0);
        });
