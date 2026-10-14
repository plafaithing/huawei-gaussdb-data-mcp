import json
from pathlib import Path

import pytest
from huaweicloud_mcp_core.errors import McpToolError

from gaussdb_data_mcp.config import DataMcpSettings
from gaussdb_data_mcp.credentials import DbCredentials, load_tokens


def _write_secret(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def test_load_from_file(tmp_path, monkeypatch):
    f = tmp_path / "credentials.json"
    _write_secret(f, {"host": "10.0.0.1", "port": 8000, "dbname": "postgres",
                      "user": "mcp_ro", "password": "p@ss"})
    monkeypatch.setenv("GAUSSDB_DATA_MCP_CREDENTIALS_FILE", str(f))
    creds = DbCredentials.load(DataMcpSettings.from_env())
    assert creds.host == "10.0.0.1" and creds.port == 8000
    assert "password" not in repr(creds)          # 防泄漏


def test_port_required(tmp_path, monkeypatch):
    f = tmp_path / "credentials.json"
    _write_secret(f, {"host": "10.0.0.1", "dbname": "postgres", "user": "u", "password": "p"})
    monkeypatch.setenv("GAUSSDB_DATA_MCP_CREDENTIALS_FILE", str(f))
    with pytest.raises(McpToolError, match="port"):
        DbCredentials.load(DataMcpSettings.from_env())   # 驱动默认 5432 是错的，必须显式


def test_hot_reload(tmp_path, monkeypatch):
    f = tmp_path / "credentials.json"
    _write_secret(f, {"host": "10.0.0.1", "port": 8000,
                      "dbname": "db", "user": "u", "password": "p1"})
    monkeypatch.setenv("GAUSSDB_DATA_MCP_CREDENTIALS_FILE", str(f))
    first = DbCredentials.load(DataMcpSettings.from_env())
    _write_secret(f, {"host": "10.0.0.2", "port": 8000,
                      "dbname": "db", "user": "u", "password": "p2"})
    second = DbCredentials.load(DataMcpSettings.from_env())
    assert second.host == "10.0.0.2" and first.fingerprint != second.fingerprint


def test_env_fallback(monkeypatch):
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DB_HOST", "10.0.0.3")
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DB_PORT", "8000")
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DB_DBNAME", "db")
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DB_USER", "u")
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DB_PASSWORD", "p")
    creds = DbCredentials.load(DataMcpSettings.from_env())
    assert creds.host == "10.0.0.3"


def test_bad_permissions_rejected(tmp_path, monkeypatch):
    f = tmp_path / "credentials.json"
    _write_secret(f, {"host": "h", "port": 1, "dbname": "d", "user": "u", "password": "p"})
    f.chmod(0o644)
    monkeypatch.setenv("GAUSSDB_DATA_MCP_CREDENTIALS_FILE", str(f))
    import os
    if os.name == "nt":
        pytest.skip("POSIX permission bits not enforced on Windows (与 core 行为一致)")
    with pytest.raises(McpToolError):
        DbCredentials.load(DataMcpSettings.from_env())


def test_tokens_load(tmp_path):
    f = tmp_path / "tokens.json"
    _write_secret(f, {"tokens": {"dify": "tok_1", "claude": "tok_2"}})
    assert load_tokens(str(f)) == {"dify": "tok_1", "claude": "tok_2"}


def test_public_host_requires_strong_ssl(tmp_path, monkeypatch):
    # 公网地址 + 弱 sslmode → 拒绝（fail-closed，模式 2 护栏）
    f = tmp_path / "credentials.json"
    _write_secret(f, {"host": "1.2.3.4", "port": 8000, "dbname": "db", "user": "u",
                      "password": "p", "sslmode": "prefer"})
    monkeypatch.setenv("GAUSSDB_DATA_MCP_CREDENTIALS_FILE", str(f))
    with pytest.raises(McpToolError, match="sslmode"):
        DbCredentials.load(DataMcpSettings.from_env())
    # verify-ca 放行（verify-ca 需 CA 证书：放一个到约定路径）
    (tmp_path / "cacert.pem").write_text("FAKE CA", encoding="utf-8")
    _write_secret(f, {"host": "1.2.3.4", "port": 8000, "dbname": "db", "user": "u",
                      "password": "p", "sslmode": "verify-ca"})
    creds = DbCredentials.load(DataMcpSettings.from_env())
    assert creds.sslmode == "verify-ca"


def test_private_host_allows_prefer(tmp_path, monkeypatch):
    # 内网地址不强制（VPC 内流量，prefer 足够）
    f = tmp_path / "credentials.json"
    _write_secret(f, {"host": "192.168.0.63", "port": 8000, "dbname": "db", "user": "u",
                      "password": "p", "sslmode": "prefer"})
    monkeypatch.setenv("GAUSSDB_DATA_MCP_CREDENTIALS_FILE", str(f))
    assert DbCredentials.load(DataMcpSettings.from_env()).sslmode == "prefer"


def test_domain_host_requires_strong_ssl(tmp_path, monkeypatch):
    # 域名一律按公网（保守）
    f = tmp_path / "credentials.json"
    _write_secret(f, {"host": "db.example.com", "port": 8000, "dbname": "db", "user": "u",
                      "password": "p", "sslmode": "prefer"})
    monkeypatch.setenv("GAUSSDB_DATA_MCP_CREDENTIALS_FILE", str(f))
    with pytest.raises(McpToolError, match="sslmode"):
        DbCredentials.load(DataMcpSettings.from_env())


def test_localhost_and_docker_internal_allowed(tmp_path, monkeypatch):
    # localhost / host.docker.internal 按内网处理（本地开发轨）
    for host in ("localhost", "host.docker.internal", "127.0.0.1"):
        f = tmp_path / "credentials.json"
        _write_secret(f, {"host": host, "port": 5432, "dbname": "db", "user": "u",
                          "password": "p", "sslmode": "prefer"})
        monkeypatch.setenv("GAUSSDB_DATA_MCP_CREDENTIALS_FILE", str(f))
        assert DbCredentials.load(DataMcpSettings.from_env()).host == host


def test_port_null_treated_as_missing(tmp_path, monkeypatch):
    # 文件 port 为 null → 按缺失处理，不得把 null 强转成字面 "None"
    f = tmp_path / "credentials.json"
    _write_secret(f, {"host": "10.0.0.1", "port": None, "dbname": "db", "user": "u",
                      "password": "p"})
    monkeypatch.setenv("GAUSSDB_DATA_MCP_CREDENTIALS_FILE", str(f))
    with pytest.raises(McpToolError, match="port") as excinfo:
        DbCredentials.load(DataMcpSettings.from_env())
    assert "None" not in str(excinfo.value)


def test_port_non_integer_rejected(tmp_path, monkeypatch):
    # port 非整数 → McpToolError，且不回显原始值
    f = tmp_path / "credentials.json"
    _write_secret(f, {"host": "10.0.0.1", "port": "abc", "dbname": "db", "user": "u",
                      "password": "p"})
    monkeypatch.setenv("GAUSSDB_DATA_MCP_CREDENTIALS_FILE", str(f))
    with pytest.raises(McpToolError, match="integer") as excinfo:
        DbCredentials.load(DataMcpSettings.from_env())
    assert "abc" not in str(excinfo.value)


def test_null_host_treated_as_missing(tmp_path, monkeypatch):
    # host 为 null → 按缺失处理，不得变成字面 "None"
    f = tmp_path / "credentials.json"
    _write_secret(f, {"host": None, "port": 8000, "dbname": "db", "user": "u", "password": "p"})
    monkeypatch.setenv("GAUSSDB_DATA_MCP_CREDENTIALS_FILE", str(f))
    with pytest.raises(McpToolError, match="host") as excinfo:
        DbCredentials.load(DataMcpSettings.from_env())
    assert "None" not in str(excinfo.value)


# --- CA 证书约定路径自动探测（verify-ca/verify-full 零配置）---

def test_verify_ca_autodetects_cacert_beside_credentials(tmp_path, monkeypatch):
    # verify-ca + credentials.json 同目录有 cacert.pem → 自动采用，sslrootcert 未填
    f = tmp_path / "credentials.json"
    (tmp_path / "cacert.pem").write_text("FAKE CA", encoding="utf-8")
    _write_secret(f, {"host": "10.0.0.1", "port": 8000, "dbname": "db", "user": "u",
                      "password": "p", "sslmode": "verify-ca"})
    monkeypatch.setenv("GAUSSDB_DATA_MCP_CREDENTIALS_FILE", str(f))
    creds = DbCredentials.load(DataMcpSettings.from_env())
    assert creds.connect_kwargs["sslrootcert"] == str(tmp_path / "cacert.pem")


def test_verify_ca_missing_ca_rejected(tmp_path, monkeypatch):
    # verify-ca 但约定路径无 cacert.pem → CA_MISSING，hint 指出期望路径
    f = tmp_path / "credentials.json"
    _write_secret(f, {"host": "10.0.0.1", "port": 8000, "dbname": "db", "user": "u",
                      "password": "p", "sslmode": "verify-ca"})
    monkeypatch.setenv("GAUSSDB_DATA_MCP_CREDENTIALS_FILE", str(f))
    with pytest.raises(McpToolError) as excinfo:
        DbCredentials.load(DataMcpSettings.from_env())
    assert excinfo.value.code == "GAUSSDB_DATA_MCP_CA_MISSING"
    assert "cacert.pem" in (excinfo.value.hint or "")


def test_verify_full_missing_ca_rejected(tmp_path, monkeypatch):
    # verify-full 同样受证书探测约束
    f = tmp_path / "credentials.json"
    _write_secret(f, {"host": "10.0.0.1", "port": 8000, "dbname": "db", "user": "u",
                      "password": "p", "sslmode": "verify-full"})
    monkeypatch.setenv("GAUSSDB_DATA_MCP_CREDENTIALS_FILE", str(f))
    with pytest.raises(McpToolError) as excinfo:
        DbCredentials.load(DataMcpSettings.from_env())
    assert excinfo.value.code == "GAUSSDB_DATA_MCP_CA_MISSING"


def test_verify_ca_explicit_sslrootcert_wins(tmp_path, monkeypatch):
    # 显式 sslrootcert（文件字段）→ 用显式值，不做约定路径探测
    f = tmp_path / "credentials.json"
    (tmp_path / "cacert.pem").write_text("FAKE CA", encoding="utf-8")
    explicit = tmp_path / "explicit-ca.pem"
    explicit.write_text("EXPLICIT CA", encoding="utf-8")
    _write_secret(f, {"host": "10.0.0.1", "port": 8000, "dbname": "db", "user": "u",
                      "password": "p", "sslmode": "verify-ca",
                      "sslrootcert": str(explicit)})
    monkeypatch.setenv("GAUSSDB_DATA_MCP_CREDENTIALS_FILE", str(f))
    creds = DbCredentials.load(DataMcpSettings.from_env())
    assert creds.connect_kwargs["sslrootcert"] == str(explicit)


def test_require_mode_skips_ca_probe(tmp_path, monkeypatch):
    # require 不校验服务端身份 → 不探测证书，无证书不报错
    f = tmp_path / "credentials.json"
    _write_secret(f, {"host": "1.2.3.4", "port": 8000, "dbname": "db", "user": "u",
                      "password": "p", "sslmode": "require"})
    monkeypatch.setenv("GAUSSDB_DATA_MCP_CREDENTIALS_FILE", str(f))
    creds = DbCredentials.load(DataMcpSettings.from_env())
    assert "sslrootcert" not in creds.connect_kwargs


def test_env_credentials_probe_cwd(tmp_path, monkeypatch):
    # 无凭证文件（纯 env 凭证）+ verify-ca → 探测当前工作目录 ./cacert.pem
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DB_HOST", "1.2.3.4")
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DB_PORT", "8000")
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DB_DBNAME", "db")
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DB_USER", "u")
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DB_PASSWORD", "p")
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DB_SSLMODE", "verify-ca")
    monkeypatch.setenv("GAUSSDB_DATA_MCP_CREDENTIALS_FILE", "")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cacert.pem").write_text("FAKE CA", encoding="utf-8")
    creds = DbCredentials.load(DataMcpSettings.from_env())
    assert creds.connect_kwargs["sslrootcert"] == str(tmp_path / "cacert.pem")
