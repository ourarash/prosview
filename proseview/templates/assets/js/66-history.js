
function updateHistoryDiffFontSize(size) {
    var contentDiv = document.getElementById('diffModalContent');
    if (contentDiv) contentDiv.style.fontSize = size + 'px';
    var slider = document.getElementById('historyDiffFontSize');
    if (slider) slider.value = size;
}
function loadSceneHistory(scenePath) {
    fetch(`/api/scene/history?path=${encodeURIComponent(scenePath)}`)
        .then(res => res.json())
        .then(data => {
            if (!data.ok) {
                console.error("Failed to load history:", data.error);
                return;
            }
            const container = document.getElementById("historyListContent");
            if (!container) return;
            
            
            
            let html = "";
            if (data.is_git_repo && !data.git_ignored) {
                html += `<div style="padding: 12px 16px; color: var(--text-muted); font-size: 13px; font-style: italic;">
                    Proseview creates local backups when you save. To keep your Git repository clean, we recommend adding <code>.proseview/backups/</code> to your <code>.gitignore</code>.
                </div>`;
            }
            
            if (data.history.length === 0) {
                const gitNote = data.is_git_repo ? `<br><br><span style="font-size: 11px;">Note: These are temporary, local snapshots and do not replace Git commits.</span>` : ``;
                container.innerHTML = html + `<div style="padding: 24px 16px; color: var(--text-muted); text-align: center; font-size: 13px; line-height: 1.5;">
                    <div style="margin-bottom: 8px;">No local backups found.</div>
                    File history is generated automatically when you save changes or apply AI edits.${gitNote}
                </div>`;
                return;
            }
            data.history.forEach(item => {
                const date = new Date(item.timestamp);
                const timeStr = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
                const dateStr = date.toLocaleDateString();
                
                html += `
                    <div class="history-item" onclick="openDiffModal('${scenePath}', '${item.file_ts}')">
                        <div class="history-item-header">
                            <span class="history-item-time">${timeStr} <span style="font-size: 11px; font-weight: normal; color: var(--text-muted);">${dateStr}</span></span>
                            <span class="history-item-source">${item.source}</span>
                        </div>
                        <div class="history-item-details">
                            <span>${item.diff_summary}</span>
                            <span>${item.word_count} words</span>
                        </div>
                    </div>
                `;
            });
            container.innerHTML = html;
        })
        .catch(err => console.error("History fetch error:", err));
}

let currentDiffScene = null;
let currentDiffTs = null;

let currentDiffMode = 'side-by-side';
let currentDiffContext = 'changes';
//: Re-opens whatever the modal is currently showing, so the inline/split and
//: "show entire file" toggles work the same for history and for a conflict.
let currentDiffReopen = null;

function toggleDiffContext() {
    const showFull = document.getElementById("diffShowFull").checked;
    currentDiffContext = showFull ? 'full' : 'changes';
    if (currentDiffReopen) currentDiffReopen();
}

function setDiffMode(mode) {
    if (currentDiffMode === mode) return;
    currentDiffMode = mode;
    
    document.getElementById("diffToggleInline").classList.toggle("active", mode === "inline");
    document.getElementById("diffToggleSideBySide").classList.toggle("active", mode === "side-by-side");
    
    if (currentDiffReopen) currentDiffReopen();
}

function _showDiffModal(options) {
    document.getElementById("diffModalTitle").textContent = options.title;
    document.getElementById("diffModalSubtitle").textContent = options.subtitle;
    document.getElementById("diffModalRestoreBtn").hidden = !options.restore;
    document.getElementById("diffModalOverwriteBtn").hidden = !options.overwrite;
    document.getElementById("diffModalContent").innerHTML = '<div style="padding: 24px; text-align: center; color: var(--text-muted);">Loading diff...</div>';
    document.getElementById("diffModalOverlay").hidden = false;
    
    var initialSize = 18;
    try {
        var stored = localStorage.getItem(MODAL_FONT_SIZE_STORAGE_KEY);
        if (stored) initialSize = parseInt(stored, 10);
    } catch(e) {}
    updateHistoryDiffFontSize(initialSize);

    
    options.load()
        .then(res => res.json())
        .then(data => {
            if (!data.ok) {
                alert("Could not load diff: " + data.error);
                return;
            }
            document.getElementById("diffModalContent").innerHTML = data.diff_html;
        })
        .catch(err => console.error("Diff fetch error:", err));
}

function openDiffModal(scenePath, timestamp) {
    currentDiffScene = scenePath;
    currentDiffTs = timestamp;
    currentDiffReopen = () => openDiffModal(scenePath, timestamp);
    
    _showDiffModal({
        title: "Review Changes",
        subtitle: "Restoring this version will apply the following changes to the current file.",
        restore: true,
        overwrite: false,
        load: () => fetch(`/api/scene/history/diff?path=${encodeURIComponent(scenePath)}&timestamp=${encodeURIComponent(timestamp)}&mode=${encodeURIComponent(currentDiffMode)}&context=${encodeURIComponent(currentDiffContext)}`)
    });
}

function openConflictDiffModal(absPath, draft) {
    currentDiffScene = null;
    currentDiffTs = null;
    currentDiffReopen = () => openConflictDiffModal(absPath, draft);
    
    _showDiffModal({
        title: "Scene changed on disk",
        subtitle: "Removed lines are the version on disk; added lines are your draft. Overwriting keeps the disk version in this scene's history.",
        restore: false,
        overwrite: true,
        load: () => fetch('/scene-diff', {
            method: 'POST',
            headers: pvHeaders(),
            body: JSON.stringify({
                abs_path: absPath,
                content: draft,
                mode: currentDiffMode,
                context: currentDiffContext
            })
        })
    });
}

function closeDiffModal() {
    const wasConflict = !document.getElementById("diffModalOverwriteBtn").hidden;
    document.getElementById("diffModalOverlay").hidden = true;
    currentDiffScene = null;
    currentDiffTs = null;
    currentDiffReopen = null;
    // Reading the diff is not a decision. Hand an unresolved conflict back to
    // the dialog rather than dropping the writer into an editor they still
    // cannot save from.
    if (wasConflict && typeof openSceneConflictDialog === 'function') openSceneConflictDialog();
}

document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
        const overlay = document.getElementById("diffModalOverlay");
        if (overlay && !overlay.hidden) {
            closeDiffModal();
            e.stopImmediatePropagation();
            e.preventDefault();
        }
    }
}, true);

document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("diffModalRestoreBtn")?.addEventListener("click", () => {
    if (!currentDiffScene || !currentDiffTs) return;
    
    // Check if dirty
    if (typeof _pmDirty !== 'undefined' && _pmDirty) {
        alert("Please save your current changes before restoring an older version.");
        return;
    }
    
    fetch(`/api/scene/history/restore`, {
        method: "POST",
        headers: pvHeaders(),
        body: JSON.stringify({ path: currentDiffScene, timestamp: currentDiffTs })
    })
    .then(res => res.json())
    .then(data => {
        if (!data.ok) {
            alert("Failed to restore: " + data.error);
            return;
        }
        closeDiffModal();
        refreshContent();
        
        // Show success highlight or toast
        setTimeout(() => {
            const editorView = document.querySelector(".ProseMirror");
            if (editorView) {
                editorView.classList.add("codex-highlight");
                setTimeout(() => editorView.classList.remove("codex-highlight"), 3000);
            }
        }, 500);
    })
    .catch(err => console.error("Restore error:", err));
});
});

function clearSceneHistory() {
    if (!paths[curIdx]) return;
    if (!confirm("Are you sure you want to clear all history for this file? This cannot be undone.")) return;
    
    fetch(`/api/scene/history?path=${encodeURIComponent(paths[curIdx])}`, {
        method: "DELETE",
        headers: pvHeaders()
    })
    .then(res => res.json())
    .then(data => {
        if (!data.ok) {
            alert("Failed to clear history: " + data.error);
            return;
        }
        loadSceneHistory(paths[curIdx]);
    })
    .catch(err => console.error("Clear history error:", err));
}

