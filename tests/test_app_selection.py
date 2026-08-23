"""Static regression tests for Proseview scene selection behavior."""

from __future__ import annotations

from pathlib import Path

JS_DIR = Path(__file__).resolve().parents[1] / "proseview" / "templates" / "assets" / "js"


def _read_app_js() -> str:
    """Concatenate the topical JS files in the same order generator.py does."""
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(JS_DIR.glob("*.js")))


class _AppJs:
    @staticmethod
    def read_text(encoding: str = "utf-8") -> str:
        return _read_app_js()


APP_JS = _AppJs()


def test_scene_selection_is_preserved_when_focus_leaves_the_prose():
    source = APP_JS.read_text(encoding="utf-8")

    # The live Selection is cached so the selection survives the user
    # clicking into the dock, which moves the OS-level selection.
    assert "let currentSelectionRange = null;" in source
    assert "function rememberSceneSelection(sel)" in source
    assert "currentSelectionRange = range.cloneRange();" in source
    assert "function restoreSceneSelection()" in source
    assert "sel.addRange(currentSelectionRange.cloneRange());" in source
    # A click inside the dock must not dismiss the pill or forget the range.
    assert "e.target.closest('#discussPanel')" in source
    # The visual marker is kept via pinSelectionHighlight rather than by
    # restoring the live Selection, which would jump focus back to the prose.
    assert "pinSelectionHighlight(currentSelectionRange)" in source


