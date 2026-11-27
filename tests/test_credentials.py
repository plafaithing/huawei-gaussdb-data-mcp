"""credentials.py 单测（das-only）：v1/v2 结构解析、必填校验、热轮换、写回。"""
import json
from pathlib import Path

import pytest
from huaweicloud_mcp_core.errors import McpToolError

from mcp_server_gaussdb_data.config import DataMcpSettings
from mcp_server_gaussdb_data.credentials import DbCredentials, load_tokens, save_das_connection


def _write_secret(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _settings(monkeypatch, path) -> DataMcpSettings:
    monkeypatch.setenv("GAUSSDB_DATA_MCP_CREDENTIALS_FILE", str(path))
    return DataMcpSettings.from_env()


def test_v1_flat_das_credentials(tmp_path, monkeypatch):
    f = tmp_path / "credentials.json"
    _write_secret(f, {"das_connection_id": "c-1", "dbname": "test", "user": "root",
                      "password": "p", "ak": "A", "sk": "S", "region": "cn-north-4"})
    creds = DbCredentials.load(_settings(monkeypatch, f))
    assert creds.das_connection_id == "c-1" and creds.dbname == "test"
    assert "password" not in repr(creds) and "sk" not in repr(creds)  # 防泄漏


def test_v2_multi_instance_and_alias(tmp_path, monkeypatch):
    f = tmp_path / "credentials.json"
    _write_secret(f, {"ak": "A", "sk": "S", "region": "北京四", "default_instance": "i1",
                      "instances": {
                          "i1": {"das_connection_id": "c-1", "dbname": "test",
                                 "user": "u", "password": "p"},
                          "i2": {"user": "u", "password": "p", "dbname": "db2"}}})
    s = _settings(monkeypatch, f)
    a = DbCredentials.load(s)
    assert (a.das_connection_id, a.das_region, a.instance_id) == ("c-1", "cn-north-4", "i1")
    b = DbCredentials.load(s, instance="i2")
    assert b.das_connection_id == "" and b.instance_id == "i2" and b.user == "u"
    with pytest.raises(McpToolError):
        DbCredentials.load(s, instance="nope")


def test_das_required_fields(tmp_path, monkeypatch):
    f = tmp_path / "credentials.json"
    _write_secret(f, {"dbname": "test", "user": "u", "password": "p"})  # 缺 ak/sk
    with pytest.raises(McpToolError, match="ak"):
        DbCredentials.load(_settings(monkeypatch, f))


def test_password_required_for_selfheal(tmp_path, monkeypatch):
    f = tmp_path / "credentials.json"
    _write_secret(f, {"ak": "A", "sk": "S", "region": "cn-north-4",
                      "das_connection_id": "c-1", "dbname": "test", "user": "u"})
    with pytest.raises(McpToolError, match="password"):
        DbCredentials.load(_settings(monkeypatch, f))


def test_hot_reload(tmp_path, monkeypatch):
    f = tmp_path / "credentials.json"
    _write_secret(f, {"ak": "A", "sk": "S", "region": "r", "das_connection_id": "c-1",
                      "dbname": "d1", "user": "u", "password": "p"})
    s = _settings(monkeypatch, f)
    first = DbCredentials.load(s)
    _write_secret(f, {"ak": "A", "sk": "S", "region": "r", "das_connection_id": "c-2",
                      "dbname": "d2", "user": "u", "password": "p"})
    second = DbCredentials.load(s)   # 每次调用重读（热轮换）
    assert first.das_connection_id == "c-1" and second.das_connection_id == "c-2"


def test_das_host_override(tmp_path, monkeypatch):
    f = tmp_path / "credentials.json"
    _write_secret(f, {"ak": "A", "sk": "S", "region": "cn-north-4", "das_host": "das.test.local",
                      "das_connection_id": "c-1", "dbname": "d", "user": "u", "password": "p"})
    creds = DbCredentials.load(_settings(monkeypatch, f))
    assert creds.das_host == "das.test.local"


def test_save_das_connection_roundtrip(tmp_path):
    f = tmp_path / "credentials.json"
    _write_secret(f, {"ak": "A", "instances": {}})
    save_das_connection(str(f), "i-1", "c-9", "mydb", activated=True)
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["instances"]["i-1"] == {"das_connection_id": "c-9", "dbname": "mydb",
                                        "activated": True}


def test_load_tokens(tmp_path):
    f = tmp_path / "tokens.json"
    f.write_text(json.dumps({"tokens": {"cli": "t1"}}), encoding="utf-8")
    assert load_tokens(str(f)) == {"cli": "t1"}
