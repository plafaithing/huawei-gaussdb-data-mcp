"""das.py 单测：mock _DasHttp 验证两步执行、驼峰解析、列名还原、5018 自愈与池接口兼容。"""

import pytest

from gaussdb_data_mcp import das as das_mod
from gaussdb_data_mcp.credentials import DbCredentials
from gaussdb_data_mcp.config import DataMcpSettings


def _settings(driver="das"):
    return DataMcpSettings.from_env.__wrapped__() if False else DataMcpSettings(
        driver=driver, max_rows=200,
    )


def _creds(**kw):
    base = dict(
        host="", port=0, dbname="postgres", user="root", password="pw",
        das_connection_id="conn-1", das_ak="ak", das_sk="sk",
        das_project_id="proj", das_region="cn-north-4", das_schema="public",
    )
    base.update(kw)
    return DbCredentials(**base)


class _FakeHttp:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, path, body):
        self.calls.append((path, body))
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


_RAW_RESULT = {
    "rows": 2,
    "metadata": [{"label": "datname", "name": "COLUMN_1"}, {"label": "datdba", "name": "COLUMN_2"}],
    "columnValues": [{"COLUMN_1": "postgres", "COLUMN_2": "10"}, {"COLUMN_1": "test", "COLUMN_2": "16750"}],
    "executeStatus": "finished", "timeDelay": 40, "sql": "SELECT 1",
}


def _backend(http):
    b = das_mod.DasBackend(_settings())
    b._http = lambda creds: http
    return b


def test_execute_two_steps_and_parsing():
    http = _FakeHttp([
        {"success": True, "executeId": "e1"},          # 受理
        dict(_RAW_RESULT),                              # 取结果
    ])
    b = _backend(http)
    cur = das_mod.DasCursor(b, _creds(), _settings())
    cur.execute("SELECT 1")
    assert http.calls[0][0].endswith("async-execute-sql-without-key")
    body = http.calls[0][1]
    assert body["database_name"] == "postgres" and body["schema_name"] == "public"
    assert body["ignore_limit"] is False
    assert http.calls[1][1] == {"execute_id": "e1"}
    assert [d[0] for d in cur.description] == ["datname", "datdba"]   # label 还原
    assert cur.fetchall() == [("postgres", "10"), ("test", "16750")]


def test_5018_self_heal_relogin_then_retry():
    http = _FakeHttp([
        {"errorCodeStr": "DAS.5018", "error_msg": "timed out"},   # 受理失败
        {},                                                        # relogin
        {"success": True, "executeId": "e2"},                      # 重试受理
        dict(_RAW_RESULT),                                         # 取结果
    ])
    b = _backend(http)
    cur = das_mod.DasCursor(b, _creds(), _settings())
    cur.execute("SELECT 1")
    assert http.calls[1][0].endswith("login")                     # 第二个调用是 login
    assert http.calls[1][1]["retry_login"]["is_save_password"] is True
    assert cur.fetchone() == ("postgres", "10")


def test_result_5018_self_heal():
    http = _FakeHttp([
        {"success": True, "executeId": "e3"},
        {"errorCodeStr": "DAS.5018"},                             # 取结果时失效
        {},                                                        # relogin
        {"success": True, "executeId": "e4"},                      # 重新受理
        dict(_RAW_RESULT),                                         # 重新取结果
    ])
    b = _backend(http)
    cur = das_mod.DasCursor(b, _creds(), _settings())
    cur.execute("SELECT 1")
    assert len(cur.fetchall()) == 2


def test_run_write_returns_affected_rows():
    http = _FakeHttp([{"success": True, "executeId": "e5"},
                      {**_RAW_RESULT, "rows": 3}])
    b = _backend(http)
    assert b.run_write(_creds(), settings=_settings(), sql="INSERT 1") == 3


def test_missing_connection_id_fails_fast():
    b = _backend(_FakeHttp([]))
    with pytest.raises(Exception, match="das_connection_id"):
        b.execute(_creds(das_connection_id=""), _settings(), "SELECT 1")


def test_das_driver_meta():
    d = das_mod.DasDriver()
    assert d.driver_name() == "das" and d.placeholder() == "%s"


def test_credentials_das_branch_validates(monkeypatch):
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DRIVER", "das")
    s = DataMcpSettings.from_env()
    try:
        DbCredentials.load(s)
        raise AssertionError("should raise")
    except Exception as e:
        assert "das_connection_id" in str(e)
