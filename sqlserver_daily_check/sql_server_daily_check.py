"""SQL Server 일일점검 자동화 - 사용법/설계 설명은 README.md 참고."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 1. 설정
# ---------------------------------------------------------------------------
DB_LOGIN_USER = "LOGADMIN"
DB_PASSWORD_ENV = "DBCHECK_PASSWORD"

SERVERS: list[dict[str, Any]] = [
    {
        "alias": "SQLSVR1",
        "host": "10.10.1.11",
        "port": 1433,
        "auth": "sql",              # "sql" 또는 "windows"
        "user": DB_LOGIN_USER,
        "password_env": DB_PASSWORD_ENV,
        "trust_cert": True,         # sqlcmd -C
    },
    {
        "alias": "SQLSVR2",
        "host": "10.10.1.12",
        "port": 1433,
        "auth": "sql",
        "user": DB_LOGIN_USER,
        "password_env": DB_PASSWORD_ENV,
        "trust_cert": True,
    },
    {
        "alias": "SQLSVR3",
        "host": "10.10.1.13",
        "port": 1433,
        "auth": "sql",
        "user": DB_LOGIN_USER,
        "password_env": DB_PASSWORD_ENV,
        "trust_cert": True,
    },
    {
        "alias": "SQLSVR4",
        "host": "10.10.1.14",
        "port": 1433,
        "auth": "sql",
        "user": DB_LOGIN_USER,
        "password_env": DB_PASSWORD_ENV,
        "trust_cert": True,
    },
]

SQLCMD_PATH = "sqlcmd"
DB_DESC_MAP_PATH = Path(__file__).with_name("db_desc_map.json")
DB_DESC_MAP_SAMPLE_PATH = Path(__file__).with_name("db_desc_map.sample.json")
SQLCMD_LOGIN_TIMEOUT_SEC = 15
SQLCMD_QUERY_TIMEOUT_SEC = 30
SQLCMD_TIMEOUT_SEC = 60
LONG_RUNNING_THRESHOLD_SEC = 60
ERRORLOG_LOOKBACK_DAYS = 1
ERRORLOG_MAX_SIZE_MB = 100

# 운영 영향 최소화용 세션 설정 (README '운영 영향' 참고)
SESSION_SAFETY_SQL = (
    "SET NOCOUNT ON; "
    "SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED; "
    "SET LOCK_TIMEOUT 5000; "
    "SET DEADLOCK_PRIORITY LOW;\n"
)

# Oracle 적재
ORACLE_CLIENT_DIR = r"C:\Oracle\instantclient_19_30"
REPOSITORY = {
    "host": "10.10.20.134",
    "port": 1525,
    "service_name": "DIRST",
    "user": "IRDMOW",
}
REPO_PASSWORD_ENV = "DCHECK_REPO_PASSWORD"
REPOSITORY_TABLE = "IRDMOW.TB_MON_DB_MSSQL_DCHECK"


# ---------------------------------------------------------------------------
# 2. sqlcmd 실행
# ---------------------------------------------------------------------------
def build_sqlcmd_args(server: dict[str, Any]) -> list[str]:
    args = [
        SQLCMD_PATH,
        "-S", f"{server['host']},{server['port']}",
        "-d", "master",
        "-h", "-1",
        "-W",
        "-y", "0",
        "-b",
        "-f", "65001",   # UTF-8 입출력 (미지정 시 한글 깨짐)
        "-l", str(SQLCMD_LOGIN_TIMEOUT_SEC),
        "-t", str(SQLCMD_QUERY_TIMEOUT_SEC),
    ]
    if server.get("trust_cert", True):
        args.append("-C")

    if server.get("auth") == "windows":
        args.append("-E")
    else:
        args += ["-U", server["user"]]   # 비밀번호는 SQLCMDPASSWORD 환경변수로 전달
    return args


def build_sqlcmd_env(server: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    if server.get("auth") != "windows":
        env_name = server.get("password_env", DB_PASSWORD_ENV)
        password = os.environ.get(env_name)
        if not password:
            raise RuntimeError(
                f"[{server['alias']}] 환경변수 {env_name} 에 비밀번호가 설정되어 있지 않습니다."
            )
        env["SQLCMDPASSWORD"] = password
    return env


def exec_sqlcmd(server: dict[str, Any], extra_args: list[str]) -> str:
    try:
        proc = subprocess.run(
            build_sqlcmd_args(server) + extra_args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SQLCMD_TIMEOUT_SEC,
            env=build_sqlcmd_env(server),
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"[{server['alias']}] sqlcmd 응답 없음 ({SQLCMD_TIMEOUT_SEC}초 초과)") from e
    if proc.returncode != 0:
        raise RuntimeError(
            f"[{server['alias']}] sqlcmd 실행 실패 (code={proc.returncode})\n"
            f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
        )
    return proc.stdout


def run_sqlcmd(server: dict[str, Any], sql: str) -> list[dict[str, Any]]:
    """SQL 을 실행하고 FOR JSON PATH 결과를 list[dict] 로 반환한다.
    긴 JSON 은 여러 줄로 쪼개져 나오므로 이어붙여서 파싱한다."""
    stdout = exec_sqlcmd(server, ["-Q", SESSION_SAFETY_SQL + sql])

    lines = []
    for line in stdout.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.lower().endswith("rows affected)") or s.startswith("Msg "):
            continue
        lines.append(s)

    if not lines:
        return []

    joined = "".join(lines)
    try:
        return json.loads(joined)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"[{server['alias']}] JSON 파싱 실패: {e}\n원본 출력: {joined[:500]}"
        ) from e


# ---------------------------------------------------------------------------
# 3. 점검 쿼리 (항목별 설명은 README '점검 항목' 참고)
# ---------------------------------------------------------------------------
SQL_DATABASES = """
SELECT name AS database_name, state_desc, recovery_model_desc
FROM sys.databases
WHERE database_id > 4
FOR JSON PATH;
"""

SQL_JOB_FAIL = """
IF OBJECT_ID('tempdb..#job_fail') IS NOT NULL DROP TABLE #job_fail;
CREATE TABLE #job_fail (
    server_name sysname,
    job_name nvarchar(128),
    step_name nvarchar(128),
    run_date int,
    err_msg nvarchar(max)
);
INSERT INTO #job_fail EXEC master.dbo.USP_DAILY_JOB_FAIL;
SELECT * FROM #job_fail FOR JSON PATH;
DROP TABLE #job_fail;
"""

SQL_BACKUP_LIST = """
IF OBJECT_ID('tempdb..#bkp') IS NOT NULL DROP TABLE #bkp;
CREATE TABLE #bkp (
    user_name nvarchar(128),
    database_name nvarchar(128),
    server_name nvarchar(128),
    backup_started datetime,
    backup_finished datetime,
    total_time varchar(100),
    backup_size_mb float
);
INSERT INTO #bkp EXEC master.dbo.USP_DAILY_BACKUP_LIST;
SELECT database_name,
       CONVERT(varchar(19), backup_finished, 120) AS backup_finished,
       total_time,
       backup_size_mb
FROM #bkp FOR JSON PATH;
DROP TABLE #bkp;
"""

SQL_DISK_BY_DB = """
SELECT
    DB_NAME(mf.database_id) AS database_name,
    mf.type_desc AS file_type,
    vs.volume_mount_point,
    CAST(100.0 * (vs.total_bytes - vs.available_bytes) / NULLIF(vs.total_bytes, 0) AS decimal(5,2)) AS used_pct
FROM sys.master_files mf
JOIN sys.databases d ON d.database_id = mf.database_id
CROSS APPLY sys.dm_os_volume_stats(mf.database_id, mf.file_id) vs
WHERE mf.type_desc IN ('ROWS', 'LOG')
  AND mf.database_id > 4
  AND d.state_desc = 'ONLINE'
FOR JSON PATH;
"""

SQL_ENGINE_VOLUME = """
SELECT TOP 1
    vs.volume_mount_point,
    CAST(100.0 * (vs.total_bytes - vs.available_bytes) / NULLIF(vs.total_bytes, 0) AS decimal(5,2)) AS used_pct
FROM sys.master_files mf
CROSS APPLY sys.dm_os_volume_stats(mf.database_id, mf.file_id) vs
WHERE mf.database_id = 1 AND mf.type_desc = 'ROWS'
FOR JSON PATH;
"""

SQL_CLUSTER_STATUS = """
DECLARE @IsClustered bit = CAST(ISNULL(SERVERPROPERTY('IsClustered'), 0) AS bit);
DECLARE @NodeIssues int = 0;
DECLARE @SharedDiskIssues int = 0;
DECLARE @MSDTCStatus nvarchar(20) = N'해당없음';

IF @IsClustered = 1
BEGIN
    SELECT @NodeIssues = COUNT(*) FROM sys.dm_os_cluster_nodes WHERE status_description <> 'Up';
    SELECT @SharedDiskIssues = COUNT(*) FROM sys.dm_io_cluster_shared_drives WHERE DriveName IS NULL;

    BEGIN TRY
        BEGIN DISTRIBUTED TRANSACTION;
        COMMIT TRANSACTION;
        SET @MSDTCStatus = N'특이사항 없음';
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;
        SET @MSDTCStatus = N'비정상';
    END CATCH
END

SELECT
    ISNULL(CAST(SERVERPROPERTY('InstanceName') AS nvarchar(128)), N'MSSQLSERVER') AS instance_name,
    CAST(SERVERPROPERTY('ComputerNamePhysicalNetBIOS') AS nvarchar(128)) AS active_node,
    CASE WHEN @IsClustered = 0 THEN N'해당없음'
         WHEN @NodeIssues = 0 THEN N'특이사항 없음' ELSE N'비정상' END AS cluster_status,
    CASE WHEN @IsClustered = 0 THEN N'해당없음'
         WHEN @SharedDiskIssues = 0 THEN N'특이사항 없음' ELSE N'비정상' END AS shared_disk_status,
    @MSDTCStatus AS msdtc_status
FOR JSON PATH;
"""

SQL_ERROR_LOG = f"""
IF OBJECT_ID('tempdb..#logs') IS NOT NULL DROP TABLE #logs;
CREATE TABLE #logs (archive_no int, log_date nvarchar(50), log_size bigint);
INSERT INTO #logs EXEC master.dbo.xp_enumerrorlogs 1;
DECLARE @size_mb decimal(12,1) = (SELECT CAST(log_size / 1048576.0 AS decimal(12,1)) FROM #logs WHERE archive_no = 0);

IF @size_mb > {ERRORLOG_MAX_SIZE_MB}
BEGIN
    SELECT @size_mb AS log_size_mb, 1 AS skipped, NULL AS issue_cnt, NULL AS login_fail_cnt, NULL AS latest_msg
    FOR JSON PATH, INCLUDE_NULL_VALUES;
    RETURN;
END

IF OBJECT_ID('tempdb..#log') IS NOT NULL DROP TABLE #log;
CREATE TABLE #log (log_date datetime, process_info nvarchar(50), log_text nvarchar(max));
DECLARE @start datetime = DATEADD(day, -{ERRORLOG_LOOKBACK_DAYS}, GETDATE());
INSERT INTO #log EXEC master.dbo.xp_readerrorlog 0, 1, NULL, NULL, @start, NULL, N'desc';

DELETE FROM #log
WHERE NOT (   log_text LIKE '%Error:%'
           OR log_text LIKE '%failed%'
           OR log_text LIKE '%deadlock%'
           OR log_text LIKE '%Stack Dump%'
           OR log_text LIKE '%I/O requests taking longer%'
           OR log_text LIKE '%corrupt%'
           OR log_text LIKE '%insufficient%'
           OR log_text LIKE '%not responding%'
           OR log_text LIKE '%Non-yielding%')
   OR log_text LIKE '%Login succeeded%'
   OR log_text LIKE '%found 0 errors%'
   OR log_text LIKE '%This is an informational message%';

SELECT
    @size_mb AS log_size_mb,
    0 AS skipped,
    (SELECT COUNT(*) FROM #log WHERE log_text NOT LIKE 'Error:%') AS issue_cnt,
    (SELECT COUNT(*) FROM #log WHERE log_text LIKE 'Login failed%') AS login_fail_cnt,
    (SELECT TOP 1 LEFT(log_text, 80) FROM #log WHERE log_text NOT LIKE 'Error:%' ORDER BY log_date DESC) AS latest_msg
FOR JSON PATH, INCLUDE_NULL_VALUES;
DROP TABLE #log;
"""

SQL_PERF_COUNTERS = """
SELECT
    MAX(CASE WHEN object_name LIKE '%Buffer Manager%' AND counter_name = 'Buffer cache hit ratio'
             THEN CAST(cntr_value AS float) END) AS buffer_hit_value,
    MAX(CASE WHEN object_name LIKE '%Buffer Manager%' AND counter_name = 'Buffer cache hit ratio base'
             THEN CAST(cntr_value AS float) END) AS buffer_hit_base,
    MAX(CASE WHEN object_name LIKE '%Plan Cache%' AND counter_name = 'Cache Hit Ratio' AND instance_name = '_Total'
             THEN CAST(cntr_value AS float) END) AS plan_hit_value,
    MAX(CASE WHEN object_name LIKE '%Plan Cache%' AND counter_name = 'Cache Hit Ratio Base' AND instance_name = '_Total'
             THEN CAST(cntr_value AS float) END) AS plan_hit_base
FROM sys.dm_os_performance_counters
FOR JSON PATH;
"""

SQL_LONG_RUNNING = f"""
SELECT
    DB_NAME(r.database_id) AS database_name,
    MAX(r.total_elapsed_time) / 1000 AS max_elapsed_sec,
    COUNT(*) AS session_cnt
FROM sys.dm_exec_requests r
JOIN sys.dm_exec_sessions s ON s.session_id = r.session_id
WHERE s.is_user_process = 1
  AND r.session_id <> @@SPID
  AND r.total_elapsed_time > {LONG_RUNNING_THRESHOLD_SEC * 1000}
  AND ISNULL(r.wait_type, '') NOT IN ('WAITFOR', 'BROKER_RECEIVE_WAITFOR', 'SP_SERVER_DIAGNOSTICS_SLEEP', 'XE_LIVE_TARGET_TVF')
  AND r.command NOT IN ('WAITFOR', 'BROKER TASK STOP')
GROUP BY DB_NAME(r.database_id)
FOR JSON PATH;
"""


# ---------------------------------------------------------------------------
# 4. 수집 + DB 단위 표준화
# ---------------------------------------------------------------------------
FAIL = "점검실패"
NO_ISSUE = "특이사항 없음"
DESC_REQUIRED = "입력 필요"

# (Oracle 컬럼명, CSV 헤더) - 순서대로 CSV 에 출력
RESULT_COLUMNS: list[tuple[str, str]] = [
    ("SERVER_ALIAS", "서버번호"),
    ("HOST_NAME", "호스트명"),
    ("DB_NAME", "DB명"),
    ("INSTANCE_NAME", "인스턴스명"),
    ("BIZ_DESC", "업무설명"),
    ("DB_STATUS", "DBMS 서비스"),
    ("CLUSTER_STATUS", "클러스터(MSFC) 상태"),
    ("ACTIVE_NODE", "Active 노드"),
    ("SHARED_DISK_STATUS", "공유디스크 상태"),
    ("MSDTC_STATUS", "MSDTC 상태"),
    ("ERROR_LOG_CHECK", "ErrorLog"),
    ("BUFFER_CACHE_HIT_RATIO", "Buffer Cache Hit Ratio"),
    ("PLAN_CACHE_HIT_RATIO", "Plan Cache Hit Ratio"),
    ("ENGINE_USED_PCT", "엔진 영역(80%이하)"),
    ("DATA_USED_PCT", "데이터 영역(95%이하)"),
    ("LOG_USED_PCT", "트랜잭션로그 영역(80%이하)"),
    ("LAST_FULL_BACKUP_DT", "데이터백업(Full)"),
    ("BACKUP_DURATION", "백업 소요시간"),
    ("BACKUP_SIZE_MB", "백업 크기(MB)"),
    ("JOB_FAIL_CHECK", "Agent Job 점검"),
    ("LONG_RUNNING_CHECK", "장시간 수행SQL 점검"),
    ("CHECK_DT", "점검일시"),
]


def load_db_desc_map() -> dict[str, dict[str, str]]:
    path = DB_DESC_MAP_PATH if DB_DESC_MAP_PATH.exists() else DB_DESC_MAP_SAMPLE_PATH
    if path.exists():
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    return {}


def ratio_pct(value: float | None, base: float | None) -> float | None:
    if not value or not base:
        return None
    return round(value / base * 100, 2)


def build_disk_map(disk_rows: list[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    """DB 파일이 여러 볼륨에 있으면 사용률이 가장 높은 값을 대표값으로 쓴다."""
    disk_map: dict[str, dict[str, float | None]] = {}
    for r in disk_rows:
        entry = disk_map.setdefault(r["database_name"], {"ROWS": None, "LOG": None})
        cur = entry.get(r["file_type"])
        if cur is None or (r["used_pct"] is not None and r["used_pct"] > cur):
            entry[r["file_type"]] = r["used_pct"]
    return disk_map


def summarize_error_log(rows: list[dict[str, Any]] | None) -> str:
    if rows is None:
        return FAIL
    r = (rows or [{}])[0]
    if r.get("skipped"):
        return f"로그 과대({r.get('log_size_mb')}MB) - 미조회, 에러로그 재순환 필요"
    cnt = r.get("issue_cnt") or 0
    if cnt == 0:
        return NO_ISSUE
    login_fail = r.get("login_fail_cnt") or 0
    detail = f", 로그인실패 {login_fail}건" if login_fail else ""
    return f"{cnt}건 발견(확인필요{detail}): {r.get('latest_msg') or ''}"[:200]


def check_one_server(server: dict[str, Any], db_desc_map: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    alias = server["alias"]
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows: list[dict[str, Any]] = []

    try:
        databases = run_sqlcmd(server, SQL_DATABASES)
    except Exception as e:
        print(f"[{alias}] 서버 접속/조회 실패, 건너뜀: {e}", file=sys.stderr)
        row = {col: None for col, _ in RESULT_COLUMNS}
        row.update({
            "SERVER_ALIAS": alias, "HOST_NAME": server["host"], "DB_NAME": "-", "INSTANCE_NAME": "-",
            "BIZ_DESC": "-", "DB_STATUS": FAIL, "CHECK_DT": checked_at,
        })
        return [row]

    # 항목별로 독립 실행: 실패한 항목만 '점검실패'
    def try_query(label: str, sql: str) -> list[dict[str, Any]] | None:
        try:
            return run_sqlcmd(server, sql)
        except Exception as e:
            print(f"[{alias}] {label} 점검 실패: {e}", file=sys.stderr)
            return None

    cluster_rows = try_query("클러스터", SQL_CLUSTER_STATUS)
    cluster = (cluster_rows or [{}])[0]
    cluster_default = FAIL if cluster_rows is None else "-"

    job_fail_rows = try_query("Job 실패", SQL_JOB_FAIL)
    if job_fail_rows is None:
        job_fail_status = FAIL
    elif not job_fail_rows:
        job_fail_status = NO_ISSUE
    else:
        job_fail_status = "; ".join(f"{r['job_name']}({r['step_name']})" for r in job_fail_rows)[:1000]

    perf = (try_query("성능지표", SQL_PERF_COUNTERS) or [{}])[0]
    buffer_hit_ratio = ratio_pct(perf.get("buffer_hit_value"), perf.get("buffer_hit_base"))
    plan_hit_ratio = ratio_pct(perf.get("plan_hit_value"), perf.get("plan_hit_base"))

    disk_map = build_disk_map(try_query("DB 디스크", SQL_DISK_BY_DB) or [])
    engine_used_pct = ((try_query("엔진 디스크", SQL_ENGINE_VOLUME) or [{}])[0]).get("used_pct")

    error_log_status = summarize_error_log(try_query("에러로그", SQL_ERROR_LOG))

    long_running_rows = try_query("장시간쿼리", SQL_LONG_RUNNING)
    long_running_map = {r["database_name"]: r for r in long_running_rows or []}

    backup_rows = try_query("백업", SQL_BACKUP_LIST)
    backup_map = {r["database_name"]: r for r in backup_rows or []}

    active_node = (cluster.get("active_node") or "").lower()
    host_name = active_node or server["host"]   # 호스트명 = 현재 서비스 중인 물리 노드명
    instance_name = cluster.get("instance_name", "-")
    desc_map_for_server = db_desc_map.get(alias, {})

    for db in databases:
        dbname = db["database_name"]
        backup = backup_map.get(dbname)
        longrun = long_running_map.get(dbname)
        area = disk_map.get(dbname, {"ROWS": None, "LOG": None})

        if long_running_rows is None:
            long_running_status = FAIL
        elif not longrun:
            long_running_status = NO_ISSUE
        else:
            long_running_status = f"{longrun['session_cnt']}건, 최대 {longrun['max_elapsed_sec']}초"

        rows.append({
            "SERVER_ALIAS": alias,
            "HOST_NAME": host_name,
            "DB_NAME": dbname,
            "INSTANCE_NAME": instance_name,
            "BIZ_DESC": desc_map_for_server.get(dbname) or DESC_REQUIRED,
            "DB_STATUS": "정상 서비스" if db["state_desc"] == "ONLINE" else db["state_desc"],
            "CLUSTER_STATUS": cluster.get("cluster_status", cluster_default),
            "ACTIVE_NODE": active_node or "-",
            "SHARED_DISK_STATUS": cluster.get("shared_disk_status", cluster_default),
            "MSDTC_STATUS": cluster.get("msdtc_status", cluster_default),
            "ERROR_LOG_CHECK": error_log_status,
            "BUFFER_CACHE_HIT_RATIO": buffer_hit_ratio,
            "PLAN_CACHE_HIT_RATIO": plan_hit_ratio,
            "ENGINE_USED_PCT": engine_used_pct,
            "DATA_USED_PCT": area["ROWS"],
            "LOG_USED_PCT": area["LOG"],
            "LAST_FULL_BACKUP_DT": backup["backup_finished"] if backup else None,
            "BACKUP_DURATION": backup["total_time"] if backup else (FAIL if backup_rows is None else "확인필요"),
            "BACKUP_SIZE_MB": backup["backup_size_mb"] if backup else None,
            "JOB_FAIL_CHECK": job_fail_status,
            "LONG_RUNNING_CHECK": long_running_status,
            "CHECK_DT": checked_at,
        })

    return rows


def run_daily_check() -> list[dict[str, Any]]:
    db_desc_map = load_db_desc_map()
    all_rows: list[dict[str, Any]] = []
    for server in SERVERS:
        print(f"[{server['alias']}] 점검 시작 ({server['host']}:{server['port']})")
        rows = check_one_server(server, db_desc_map)
        print(f"[{server['alias']}] 점검 완료: {len(rows)}개 DB")
        all_rows.extend(rows)
    return all_rows


# ---------------------------------------------------------------------------
# 5. CSV 저장
# ---------------------------------------------------------------------------
def export_csv(rows: list[dict[str, Any]], out_path: Path) -> None:
    if not rows:
        print("저장할 결과가 없습니다.")
        return
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:   # BOM 필수 (Excel 한글)
        writer = csv.writer(f)
        writer.writerow([header for _, header in RESULT_COLUMNS])
        for r in rows:
            writer.writerow([r[col] for col, _ in RESULT_COLUMNS])
    print(f"CSV 저장 완료: {out_path}")


# ---------------------------------------------------------------------------
# 6. Oracle 적재
# ---------------------------------------------------------------------------
DATETIME_COLUMNS = {"LAST_FULL_BACKUP_DT", "CHECK_DT"}


def _to_oracle_bind(row: dict[str, Any]) -> dict[str, Any]:
    bind = {}
    for col, _ in RESULT_COLUMNS:
        v = row[col]
        if col in DATETIME_COLUMNS and v:
            v = datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
        bind[col] = v
    bind["REG_DT"] = bind["CHECK_DT"].strftime("%Y%m%d")
    return bind


def insert_results_to_repository(rows: list[dict[str, Any]]) -> None:
    """같은 REG_DT+서버의 기존 결과를 지우고 적재한다. 실패 시 전체 롤백."""
    if not rows:
        return

    password = os.environ.get(REPO_PASSWORD_ENV)
    if not password:
        raise RuntimeError(f"환경변수 {REPO_PASSWORD_ENV} 에 적재 DB 비밀번호가 설정되어 있지 않습니다.")

    import oracledb

    oracledb.init_oracle_client(lib_dir=ORACLE_CLIENT_DIR)   # Thick 모드
    dsn = f"{REPOSITORY['host']}:{REPOSITORY['port']}/{REPOSITORY['service_name']}"

    binds = [_to_oracle_bind(r) for r in rows]
    columns = ["REG_DT"] + [col for col, _ in RESULT_COLUMNS]
    insert_sql = (
        f"INSERT INTO {REPOSITORY_TABLE} ({', '.join(columns)}) "
        f"VALUES ({', '.join(':' + c for c in columns)})"
    )
    delete_sql = f"DELETE FROM {REPOSITORY_TABLE} WHERE REG_DT = :1 AND SERVER_ALIAS = :2"
    delete_keys = sorted({(b["REG_DT"], b["SERVER_ALIAS"]) for b in binds})

    with oracledb.connect(user=REPOSITORY["user"], password=password, dsn=dsn) as conn:
        with conn.cursor() as cur:
            cur.executemany(delete_sql, delete_keys)
            cur.executemany(insert_sql, binds)
        conn.commit()
    print(f"[REPO] {REPOSITORY_TABLE} {len(binds)}건 적재 완료")


# ---------------------------------------------------------------------------
# 7. 실행
# ---------------------------------------------------------------------------
def main() -> int:
    rows = run_daily_check()

    out_path = Path(__file__).parent / f"daily_check_{datetime.now():%Y%m%d_%H%M%S}.csv"
    export_csv(rows, out_path)

    try:
        insert_results_to_repository(rows)
    except Exception as e:
        print(f"[REPO] 적재 실패 (CSV 는 저장됨: {out_path}): {e}", file=sys.stderr)
        return 1

    print(f"\n총 {len(rows)}개 DB 점검 완료.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
