let currentTab = 'overview';
        let suppressHashWrite = false;
        const VALID_TABS = ['overview', 'analysis', 'timeline', 'todos', 'notes', 'settings'];

        // Headers for every state-changing request. The session token is the
        // only thing separating this page from any other site the user has
        // open: the server rejects mutations without it, and a cross-origin
        // caller cannot set a custom header without a preflight we refuse.
        function pvHeaders() {
            return {
                'Content-Type': 'application/json',
                'X-Proseview-Session': pageSessionToken,
            };
        }

        // EventSource cannot send the custom header used by mutations. Carry
        // the same per-run token in its URL so a replacement server can answer
        // stale tabs with 204 and make their automatic reconnects stop.
        function pvEventSourceUrl(path) {
            const separator = path.includes('?') ? '&' : '?';
            return path + separator + 'session=' + encodeURIComponent(pageSessionToken);
        }

        const PASS_LABELS = {
            passive_voice: 'Passive Voice',
            filter_verbs: 'Filter Verbs',
            crutch_words: 'Crutch Words',
            hyperbole: 'Hyperbole',
            lyrical: 'Lyrical',
            sensory: 'Sensory',
            comedy_beats: 'Comedy Beats',
            repeats: 'Repeats',
            first_person: 'First Person'
        };
        const PASS_CLASSES = {
            passive_voice: 'hl-passive',
            filter_verbs: 'hl-filter',
            crutch_words: 'hl-crutch',
            hyperbole: 'hl-hyperbole',
            lyrical: 'hl-lyrical',
            sensory: 'hl-sensory',
            comedy_beats: 'hl-comedy',
            repeats: 'hl-repeat',
            first_person: 'hl-first-person'
        };
        // Every pass carries a line of examples rather than a definition:
        // "felt, saw, heard, noticed" says what Filter Verbs are faster than a
        // sentence can, and it survives on a touch screen where a tooltip does
        // not. Two of these names mislead on their own -- Comedy Beats is
        // punctuation, Lyrical is simile markers -- which is the case for
        // showing the vocabulary rather than the label alone.
        const PASS_EXAMPLES = {
            repeats: 'words this scene leans on',
            first_person: 'I, me, my, myself',
            comedy_beats: '! ... ?! and dashes',
            crutch_words: 'just, really, quite, actually',
            lyrical: 'like, as, seemed, became',
            filter_verbs: 'felt, saw, heard, noticed',
            hyperbole: 'always, never, everything',
            sensory: 'sight, sound, smell, touch, taste',
            passive_voice: 'was written, is known'
        };
        // The second layer: what the pass counts, and why it might matter.
        // Shown on hover and on keyboard focus, never required to read the row.
        const PASS_NOTES = {
            repeats: "The scene's own most-repeated content words. Repetition you chose reads as rhythm; repetition you did not is the commonest revision note.",
            first_person: 'First-person pronouns. A high rate in close third can mean the narration has drifted into the character’s head.',
            comedy_beats: 'Exclamation marks, ellipses, interrobangs and em dashes: the punctuation of timing. Clustered, they can make prose read as arch.',
            crutch_words: 'Fifteen hedging words. Each one softens a sentence; together they make prose apologise for itself.',
            lyrical: 'Simile and transformation markers. The vocabulary of figurative writing, useful when you want to know how much of it a scene is carrying.',
            filter_verbs: 'Twenty perception verbs. They put the character between the reader and the thing: “she saw the door open” rather than “the door opened”.',
            hyperbole: 'Fourteen absolutes. Overstatement spends the reader’s trust faster than it buys emphasis.',
            sensory: 'Sixty-seven words across the five senses. The tooltip on each match in the prose names its category.',
            passive_voice: 'A form of “to be” followed by a past participle. Passive is not a fault, but a scene made mostly of it loses its actors.'
        };

        const PASS_INLINE_TIPS = {
            passive_voice: 'A form of "to be" followed by a past participle. Consider if making the subject perform the action fits the scene better (e.g., "the ball was thrown by him" vs "he threw the ball").',
            filter_verbs: 'The verb "{word}" can put distance between the reader and the action. Consider if it is necessary (e.g., "she saw the door open" vs "the door opened").',
            crutch_words: '"{word}" is a hedging word that softens the sentence. The prose might be stronger without it (e.g., "it was really cold" vs "it was freezing").',
            hyperbole: 'Absolute words like "{word}" can sometimes weaken emphasis if overused in a scene (e.g., "he always forgot" vs "he frequently forgot").',
            repeats: 'The word "{word}" is repeated {para} times in this paragraph and {scene} times in the entire scene. Consider if it provides intentional rhythm or if it should be varied.',
            first_person: 'First-person pronouns (I, me, my). A high rate in a scene written in close third person can mean the narration has drifted into the character’s head.',
            lyrical: 'Simile or transformation markers used for figurative writing (e.g., "like", "as", "seemed").',
            sensory: 'Words related to the five senses (e.g., "whisper", "rough", "aroma").',
            comedy_beats: 'Punctuation marks often used for timing (e.g., "!", "...", "?!"). Clustered together, they can affect the rhythm of the prose.'
        };

        const THEME_STORAGE_KEY = 'proseview-theme';
        const THEME_ORDER = ['light', 'dark', 'docsify', 'hopscotch', 'graphite-light', 'graphite-dark'];
        const THEME_LABELS = {
            light: 'Light',
            dark: 'Dark',
            docsify: 'Docsify',
            hopscotch: 'Hopscotch',
            'graphite-light': 'Graphite Light',
            'graphite-dark': 'Graphite Dark'
        };
        const FONT_STORAGE_KEY = 'proseview-font';
        const MODAL_FONT_SIZE_STORAGE_KEY = 'proseview-modal-font-size';
        const MODAL_FONT_SIZE_DEFAULT = 18;
        const MODAL_FONT_SIZE_MIN = 12;
        const MODAL_FONT_SIZE_MAX = 36;
        // Measure, not margins: the reading column is centred, so the only
        // real variable is how wide the text is. Stored in px but shown to the
        // reader as characters per line, which is the number that means
        // something when choosing a comfortable measure.
        const READING_MEASURE_STORAGE_KEY = 'proseview-reading-measure';
        const READING_MEASURE_DEFAULT = 760;
        const READING_MEASURE_MIN = 480;
        const READING_MEASURE_MAX = 1100;
        const VIEW_SCROLL_STORAGE_PREFIX = 'proseview-scroll:';
        const HIGHLIGHTS_STORAGE_KEY = 'proseview-highlights';
        const FONT_ORDER = ['reader', 'literary', 'inter', 'georgia', 'baskerville', 'sans', 'mono'];
        const FONT_LABELS = { reader: 'Reader', literary: 'Literary', inter: 'Inter', georgia: 'Georgia', baskerville: 'Baskerville', sans: 'Sans', mono: 'Mono' };
        const chartRefs = {};
        let curIdx = -1;
        let hls = {};
        PASS_ORDER.forEach(p => hls[p] = false);
        let scrollSaveQueued = false;
        let routeHydrating = false;

        // The dock and the reading column compete for the same pixels. Rather
        // than pick a breakpoint, ask whether the prose can still hold the
        // measure the reader chose: if splitting the viewport would squeeze it
        // below that, the dock overlays instead of docking. A reader on a
        // 1280px laptop who has asked for a wide measure gets the overlay; one
        // on the same screen reading narrow keeps the split view.
        const OVERLAY_MIN_PROSE_WIDTH = 420;

        function _dockWouldCrushTheProse(logicalViewportWidth) {
            if (!document.body.classList.contains('discuss-open')) return false;
            const dock = Math.min(
                parseFloat(getComputedStyle(document.documentElement)
                    .getPropertyValue('--utility-dock-w')) || 504,
                logicalViewportWidth / 2
            );
            const measure = typeof storedReadingMeasure === 'function'
                ? storedReadingMeasure() : READING_MEASURE_DEFAULT;
            // 48px of gutter is the least the reading column looks deliberate in.
            const wanted = Math.min(measure, OVERLAY_MIN_PROSE_WIDTH) + 48;
            return logicalViewportWidth - dock < wanted;
        }

        function syncCssZoomViewport() {
            const root = document.documentElement;
            const body = document.body;
            if (!body) return;
            const zoom = parseFloat(getComputedStyle(body).zoom) || 1;
            if (zoom <= 1) {
                delete root.dataset.cssZoom;
                const width = window.innerWidth;
                if (_dockWouldCrushTheProse(width)) {
                    root.dataset.utilityOverlay = 'true';
                    root.style.setProperty('--css-zoom-body-width', Math.max(220, width) + 'px');
                    root.style.setProperty('--css-zoom-dock-width', Math.max(220, width) + 'px');
                } else {
                    delete root.dataset.utilityOverlay;
                    root.style.removeProperty('--css-zoom-body-width');
                    root.style.removeProperty('--css-zoom-dock-width');
                }
                window.dispatchEvent(new Event('proseview:workspace-metrics'));
                return;
            }
            const logicalViewportWidth = window.innerWidth / zoom;
            // At browser text zoom the sidebar is responsively retracted in
            // CSS, so the reader owns the full logical viewport. Subtracting
            // the hidden sidebar here made the scene toolbar wrap into three
            // rows at 200% zoom.
            const sidebarWidth = 0;
            root.dataset.cssZoom = 'true';
            if (logicalViewportWidth < 700) root.dataset.utilityOverlay = 'true';
            else delete root.dataset.utilityOverlay;
            root.style.setProperty(
                '--css-zoom-body-width',
                Math.max(220, logicalViewportWidth - sidebarWidth) + 'px'
            );
            root.style.setProperty(
                '--css-zoom-dock-width',
                Math.max(220, logicalViewportWidth < 700 ? logicalViewportWidth : logicalViewportWidth / 2) + 'px'
            );
            window.dispatchEvent(new Event('proseview:workspace-metrics'));
        }

        (function observeCssZoomViewport() {
            syncCssZoomViewport();
            window.addEventListener('resize', syncCssZoomViewport);
            if (window.visualViewport) window.visualViewport.addEventListener('resize', syncCssZoomViewport);
            // "class" is in here because opening the dock is a class change on
            // body, and whether the dock overlays depends on it being open.
            new MutationObserver(syncCssZoomViewport).observe(document.body, {
                attributes: true,
                attributeFilter: ['style', 'class'],
            });
            new MutationObserver(syncCssZoomViewport).observe(document.documentElement, {
                attributes: true,
                attributeFilter: ['data-sidebar'],
            });
        })();

        function syncSidebarInteractiveState() {
            const root = document.documentElement;
            const sidebar = document.getElementById('repoSidebar');
            if (!sidebar) return;
            const utilityDockOpen = document.body.classList.contains('discuss-open');
            const compactDock = utilityDockOpen && window.matchMedia('(max-width: 1120px)').matches;
            const responsivelyRetracted = window.matchMedia('(max-width: 700px)').matches
                || root.dataset.cssZoom === 'true'
                || compactDock;
            const hidden = root.dataset.sidebar === 'closed' || responsivelyRetracted;
            sidebar.inert = hidden;
        }

        (function observeSidebarInteractiveState() {
            syncSidebarInteractiveState();
            window.addEventListener('resize', syncSidebarInteractiveState);
            window.addEventListener('proseview:workspace-metrics', syncSidebarInteractiveState);
            new MutationObserver(syncSidebarInteractiveState).observe(document.body, {
                attributes: true,
                attributeFilter: ['class'],
            });
            new MutationObserver(syncSidebarInteractiveState).observe(document.documentElement, {
                attributes: true,
                attributeFilter: ['data-sidebar', 'data-css-zoom'],
            });
        })();

        function workspaceZoomFactor() {
            return Math.max(1, parseFloat(getComputedStyle(document.body).zoom) || 1);
        }

        function workspaceLogicalViewportWidth() {
            return (document.documentElement.clientWidth || window.innerWidth) / workspaceZoomFactor();
        }

        function workspaceDockWidthBounds(minWidth) {
            const viewportWidth = workspaceLogicalViewportWidth();
            const zoomed = document.documentElement.dataset.cssZoom === 'true';
            const compact = !zoomed && viewportWidth <= 1120;
            const sidebar = document.getElementById('repoSidebar');
            const sidebarReserved = !compact && !zoomed && document.documentElement.dataset.sidebar !== 'closed' && sidebar
                ? sidebar.getBoundingClientRect().width / workspaceZoomFactor() + 10
                : 0;
            const minimumWritingWidth = viewportWidth < 700
                ? Math.max(180, viewportWidth * 0.375)
                : Math.min(480, Math.max(320, viewportWidth * 0.4));
            let maximum = Math.max(220, Math.floor(viewportWidth - sidebarReserved - minimumWritingWidth));
            if (compact) maximum = Math.min(maximum, Math.floor(viewportWidth / 2));
            return {
                min: Math.min(minWidth, maximum),
                max: Math.max(Math.min(minWidth, maximum), maximum)
            };
        }

        function workspaceSidebarWidthBounds() {
            const minimum = 160;
            const maximum = Math.max(minimum, Math.min(520, Math.floor(workspaceLogicalViewportWidth() - 370)));
            return {min: minimum, max: maximum};
        }

        function updateSeparatorValue(handle, value, minimum, maximum) {
            if (!handle) return;
            handle.setAttribute('aria-valuemin', String(Math.round(minimum)));
            handle.setAttribute('aria-valuemax', String(Math.round(maximum)));
            handle.setAttribute('aria-valuenow', String(Math.round(value)));
        }

        var _pmView = null;
        var _pmEditMode = false;
        var _pmOpenMtime = null;
        var _pmDirty = false;
        var _pmSavedFlashTimer = null;
        var _pmConflictDraft = null;
        var _pmSaveInFlight = false;
        // Counts SSE reload events we expect to be triggered by our own
        // /save-scene calls. Decremented (with a tail timeout) when the
        // event arrives, so reloadOrDefer can skip the page reload our
        // own save would otherwise cause.
        var _pendingSelfReloads = 0;
        var _pendingSelfReloadTimer = null;
