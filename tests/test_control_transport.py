"""patches/control_transport.py on Linux: pure parts and the dispatch seams.

The Windows half (CreateNamedPipeW, the DACL, the owner check) cannot run here;
it is exercised by a real Windows run. What Linux can prove: the pipe name is a
pure function with fixed vectors, it is derived afresh on every call (never
cached), and importing the module on POSIX touches no Windows API.
"""
import importlib.util
import ntpath
import posixpath
import socket
import sys
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parent.parent / "patches" / "control_transport.py"


@pytest.fixture()
def ct():
    spec = importlib.util.spec_from_file_location("vml_control_transport", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- name vectors -----------------------------------------------------------
# Computed once with hashlib.sha256 over the normalised absolute path (UTF-8).

@pytest.mark.parametrize("path, pathmod, digest", [
    ("/home/u/.voicemode/control.sock", posixpath, "db50d67e0f6cb50e"),
    ("/tmp/x/control.sock", posixpath, "0f11ad0377093705"),
    (r"C:\Users\U\.voicemode\control.sock", ntpath, "27029940a442af58"),
])
def test_pipe_name_vectors(ct, path, pathmod, digest):
    assert ct.pipe_name(path, pathmod=pathmod) == r"\\.\pipe\voicemode-control-" + digest


def test_windows_name_ignores_case_and_slash_style(ct):
    a = ct.pipe_name(r"C:\Users\U\.voicemode\control.sock", pathmod=ntpath)
    b = ct.pipe_name("c:/users/u/.voicemode/control.sock", pathmod=ntpath)
    assert a == b


def test_distinct_paths_get_distinct_pipes(ct):
    a = ct.pipe_name("/a/control.sock", pathmod=posixpath)
    b = ct.pipe_name("/b/control.sock", pathmod=posixpath)
    assert a != b


def test_relative_path_is_made_absolute(ct, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert ct.pipe_name("control.sock") == ct.pipe_name(str(tmp_path / "control.sock"))


def test_name_accepts_pathlike(ct):
    assert ct.pipe_name(Path("/a/control.sock")) == ct.pipe_name("/a/control.sock")


# --- never cached -----------------------------------------------------------

def test_connect_derives_the_name_on_every_call(ct, monkeypatch, tmp_path):
    """FELIX rebuilds runtimes, which moves control.sock; a cached name would
    keep dialling the old pipe."""
    seen = []
    monkeypatch.setattr(ct, "_win_connect", lambda name, timeout: seen.append(name) or "conn")
    first, second = tmp_path / "one" / "control.sock", tmp_path / "two" / "control.sock"
    ct.connect(first, 0.1)
    ct.connect(second, 0.1)
    ct.connect(first, 0.1)
    assert seen == [ct.pipe_name(first), ct.pipe_name(second), ct.pipe_name(first)]


def test_create_server_derives_the_name_on_every_call(ct, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(ct, "_win_create_server", lambda name: seen.append(name) or "srv")
    ct.create_server(tmp_path / "one" / "control.sock")
    ct.create_server(tmp_path / "two" / "control.sock")
    assert seen == [ct.pipe_name(tmp_path / "one" / "control.sock"),
                    ct.pipe_name(tmp_path / "two" / "control.sock")]


def test_no_cache_decorators_on_name_functions(ct):
    for fn in (ct.pipe_name, ct.connect, ct.create_server):
        assert not hasattr(fn, "cache_info"), fn


# --- selection and POSIX safety ---------------------------------------------

def test_use_pipe_follows_af_unix_at_call_time(ct, monkeypatch):
    assert ct.use_pipe() is (not hasattr(socket, "AF_UNIX"))
    monkeypatch.delattr(socket, "AF_UNIX")
    assert ct.use_pipe() is True


def test_import_on_posix_loads_no_windows_api(ct):
    assert ct._API is None


def test_windows_entry_points_refuse_off_windows(ct, tmp_path):
    if sys.platform == "win32":
        pytest.skip("Windows runs the real thing")
    with pytest.raises(OSError):
        ct.connect(tmp_path / "control.sock", 0.1)
    with pytest.raises(OSError):
        ct.create_server(tmp_path / "control.sock")


def test_owner_only_sddl(ct):
    sid = "S-1-5-21-1-2-3-1001"
    assert ct.owner_only_sddl(sid) == f"O:{sid}D:P(A;;GA;;;{sid})"


def test_sddl_owner_only_check(ct):
    sid = "S-1-5-21-1-2-3-1001"
    assert ct.sddl_is_owner_only(f"O:{sid}D:P(A;;FA;;;{sid})", sid)
    assert not ct.sddl_is_owner_only(f"O:{sid}D:P(A;;FA;;;{sid})(A;;FA;;;WD)", sid)
    assert not ct.sddl_is_owner_only(f"O:{sid}D:(A;;FA;;;{sid})", sid)  # not protected
    assert not ct.sddl_is_owner_only(f"O:BAD:P(A;;FA;;;{sid})", sid)    # owner differs


def test_invalid_handle_value_is_recognised_as_c_void_p_returns_it(ct):
    """HANDLE is c_void_p: INVALID_HANDLE_VALUE arrives as 2**64-1, not -1.
    Missing this let failed CreateFileW / CreateNamedPipeW calls through on
    Windows, and the owner check then read the current process (handle -1)."""
    import ctypes
    assert ct._handle_ok(ctypes.c_void_p(-1).value) is False
    assert ct._handle_ok(-1) is False
    assert ct._handle_ok(None) is False
    assert ct._handle_ok(0) is False
    assert ct._handle_ok(0x1A4) is True


def test_owner_only_sddl_with_mandatory_label(ct):
    sid = "S-1-5-21-1-2-3-1001"
    assert ct.owner_only_sddl(sid, label_rid=0x2000) == \
        f"O:{sid}D:P(A;;GA;;;{sid})S:(ML;;NW;;;S-1-16-8192)"


def test_label_never_above_medium(ct):
    assert ct.server_label_rid(0x3000) == 0x2000   # elevated server: medium clients may write
    assert ct.server_label_rid(0x2000) == 0x2000
    assert ct.server_label_rid(0x1000) == 0x1000   # cannot label above itself


def test_sddl_owner_only_check_tolerates_a_label(ct):
    sid = "S-1-5-21-1-2-3-1001"
    assert ct.sddl_is_owner_only(f"O:{sid}D:P(A;;FA;;;{sid})S:(ML;;NW;;;ME)", sid)


@pytest.mark.parametrize("sddl, rid", [
    ("O:XD:P(A;;FA;;;X)S:(ML;;NW;;;ME)", 0x2000),
    ("O:XD:P(A;;FA;;;X)S:(ML;;NW;;;LW)", 0x1000),
    ("O:XD:P(A;;FA;;;X)S:(ML;;NW;;;HI)", 0x3000),
    ("O:XD:P(A;;FA;;;X)S:(ML;;NW;;;S-1-16-8192)", 0x2000),
    ("O:XD:P(A;;FA;;;X)", None),
])
def test_sddl_label_rid(ct, sddl, rid):
    assert ct.sddl_label_rid(sddl) == rid


@pytest.mark.parametrize("server, mine, ok", [
    (("U", 0x2000), ("U", 0x2000), True),
    (("U", 0x3000), ("U", 0x2000), True),
    (("U", 0x1000), ("U", 0x2000), False),
    (("V", 0x2000), ("U", 0x2000), False),
    (("U", None), ("U", 0x2000), False),     # unknown integrity fails closed
])
def test_server_is_trusted(ct, server, mine, ok):
    assert ct.server_is_trusted(server[0], server[1], mine[0], mine[1]) is ok


def test_docstring_records_the_accepted_denial_of_service(ct):
    doc = ct.__doc__.lower()
    assert "another user" in doc and "denies the channel" in doc
