"""Offline tests for atomic state persistence + corruption self-heal.

Redirects the module-level paths to a temp dir so the live portfolio state is
never touched."""
import json
import tempfile
from pathlib import Path

import agents._state as st


def _with_temp_paths(fn):
    orig = (st.DATA_DIR, st.STATE_FILE, st.STATE_BAK)
    d = Path(tempfile.mkdtemp())
    st.DATA_DIR = d
    st.STATE_FILE = d / "portfolio_state.json"
    st.STATE_BAK = d / "portfolio_state.json.bak"
    try:
        fn()
    finally:
        st.DATA_DIR, st.STATE_FILE, st.STATE_BAK = orig


def test_save_then_load_roundtrip():
    def body():
        st.save_state({"a": 1, "order_log": []})
        assert st.load_state() == {"a": 1, "order_log": []}
    _with_temp_paths(body)


def test_backup_holds_previous_version():
    def body():
        st.save_state({"v": 1})
        st.save_state({"v": 2})
        assert json.loads(st.STATE_BAK.read_text()) == {"v": 1}  # prior snapshot
        assert st.load_state() == {"v": 2}                        # current
    _with_temp_paths(body)


def test_self_heal_from_corrupt_primary():
    def body():
        st.save_state({"v": 1})
        st.save_state({"v": 2})            # bak now = {v:1}
        st.STATE_FILE.write_text("{ this is corrupt json")
        recovered = st.load_state()        # must fall back to bak, not raise
        assert recovered == {"v": 1}
        assert st.load_state() == {"v": 1}  # primary rewritten from bak
    _with_temp_paths(body)


def test_unrecoverable_raises():
    def body():
        st.STATE_FILE.write_text("{ corrupt")   # no backup exists
        raised = False
        try:
            st.load_state()
        except Exception:
            raised = True
        assert raised, "unrecoverable corrupt state with no backup must raise (so health aborts)"
    _with_temp_paths(body)


def run():
    test_save_then_load_roundtrip()
    test_backup_holds_previous_version()
    test_self_heal_from_corrupt_primary()
    test_unrecoverable_raises()
