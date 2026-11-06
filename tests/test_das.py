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
        das_project_id="proj", das_region="cn-north-4", das_schema="dbe_alert",
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
    assert body["database_name"] == "postgres" and body["schema_name"] == "dbe_alert"
    assert body["ignore_limit"] is False
    assert http.calls[1][1] == {"execute_id": "e1"}
    assert [d[0] for d in cur.description] == ["datname", "datdba"]   # label 还原
    assert cur.fetchall() == [("postgres", "10"), ("test", "16750")]


def test_execute_5018_rejected_no_heal():
    # 5018 只出现在取结果阶段（实测）；受理阶段 5018 直接失败不自愈
    http = _FakeHttp([{"errorCodeStr": "DAS.5018", "error_msg": "timed out"}])
    b = _backend(http)
    with pytest.raises(Exception, match="DAS execute rejected"):
        b.execute(_creds(), _settings(), "SELECT 1")


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
    # v2：connection_id 缺失合法（自动注册）；ak/sk/region 必填
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DRIVER", "das")
    monkeypatch.delenv("GAUSSDB_DATA_MCP_CREDENTIALS_FILE", raising=False)
    s = DataMcpSettings.from_env()
    try:
        DbCredentials.load(s)
        raise AssertionError("should raise")
    except Exception as e:
        assert "ak" in str(e)


def test_region_chinese_alias():
    assert DbCredentials._load_das.__doc__ is not None  # 冒烟：方法存在
    from gaussdb_data_mcp.credentials import normalize_region
    assert normalize_region("北京四") == "cn-north-4"
    assert normalize_region("cn-north-4") == "cn-north-4"
    assert normalize_region("") == ""


def test_blank_shell_self_heal():
    # 新连接未激活：200 空响应（无报错）——retry_login 后重试成功
    http = _FakeHttp([
        {"success": True, "executeId": "e6"},
        {"rows": 0, "metadata": None, "columnValues": None, "executeStatus": None},  # 空壳
        {},                                   # relogin
        {"success": True, "executeId": "e7"},
        dict(_RAW_RESULT),
    ])
    b = _backend(http)
    cur = das_mod.DasCursor(b, _creds(), _settings())
    cur.execute("SELECT 1")
    assert "login-actions" in http.calls[2][0]
    assert len(cur.fetchall()) == 2


def test_v2_multi_instance(tmp_path, monkeypatch):
    import json as _json
    cred = tmp_path / 'cred.json'
    cred.write_text(_json.dumps({
        'ak': 'AKX', 'sk': 'SKX', 'region': '北京四', 'default_instance': 'inst-a',
        'instances': {
            'inst-a': {'das_connection_id': 'conn-a', 'dbname': 'test'},
            'inst-b': {'user': 'root', 'password': 'pw', 'dbname': 'postgres'},
        }}), encoding='utf-8')
    monkeypatch.setenv('GAUSSDB_DATA_MCP_DRIVER', 'das')
    monkeypatch.setenv('GAUSSDB_DATA_MCP_CREDENTIALS_FILE', str(cred))
    s = DataMcpSettings.from_env()
    a = DbCredentials.load(s)
    assert (a.das_connection_id, a.das_region, a.dbname, a.instance_id) == ('conn-a', 'cn-north-4', 'test', 'inst-a')
    b = DbCredentials.load(s, instance='inst-b')
    assert b.das_connection_id == '' and b.instance_id == 'inst-b' and b.user == 'root'
    try:
        DbCredentials.load(s, instance='nope')
        raise AssertionError('should raise')
    except Exception as e:
        assert 'nope' in str(e)


def test_save_das_connection_roundtrip(tmp_path):
    import json as _json
    from gaussdb_data_mcp.credentials import save_das_connection
    p = tmp_path / 'cred.json'
    p.write_text('{"ak": "A", "instances": {}}', encoding='utf-8')
    save_das_connection(str(p), 'inst-1', 'conn-9', 'mydb')
    data = _json.loads(p.read_text(encoding='utf-8'))
    assert data['instances']['inst-1'] == {'das_connection_id': 'conn-9', 'dbname': 'mydb'}
    # 0600 chmod 在 Windows 上不可校验（POSIX-only），内容正确性已覆盖


def test_create_database_uses_dbe_alert_schema():
    http = _FakeHttp([{'success': True, 'executeId': 'e8'}, dict(_RAW_RESULT)])
    b = _backend(http)
    das_mod.DasCursor(b, _creds(), _settings()).execute('CREATE DATABASE x_db')
    assert http.calls[0][1]['schema_name'] == 'dbe_alert'


def test_normal_sql_uses_dbe_alert_default():
    http = _FakeHttp([{'success': True, 'executeId': 'e9'}, dict(_RAW_RESULT)])
    b = _backend(http)
    das_mod.DasCursor(b, _creds(), _settings()).execute('SELECT 1 FROM t')
    assert http.calls[0][1]['schema_name'] == 'dbe_alert'
