import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest

spec=importlib.util.spec_from_file_location("verify_link",Path(__file__).resolve().parents[1]/"scripts/verify_link.py")
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

def test_foreign_host_is_rejected():
    with pytest.raises(ValueError):module.verify("https://example.com/other.git","0"*40)

def test_embedded_credentials_are_rejected():
    with pytest.raises(ValueError):module.verify(module.CLOUD_REPO.replace("https://","https://user:example@"),"0"*40)

def test_branch_name_is_not_a_release_sha():
    with pytest.raises(ValueError):module.verify(module.CLOUD_REPO,"main")

def test_mismatched_remote_commit_fails(monkeypatch):
    monkeypatch.setattr(module.subprocess,"run",lambda *a,**kw:SimpleNamespace(returncode=0,stdout="1"*40+"\trefs/heads/main\n"))
    with pytest.raises(RuntimeError):module.verify(module.CLOUD_REPO,"0"*40)
