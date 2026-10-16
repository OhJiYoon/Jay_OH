"""
SQL Server 일일점검 자동화 스크립트
=====================================
목적
----
- sqlcmd 를 통해 점검대상 SQL Server(MSFC 인스턴스)에 접속, sp_daily.sql 에서 쓰던 점검을 실행하고
  결과를 Oracle/Mysql 일일점검결과(엑셀)와 동일한 "DB(인스턴스) 단위 1행" 표준 포맷으로 평탄화한다.
- 서버는 2노드 MSFC 인스턴스 x 4대(1~4번 서버)를 가정하며, 인스턴스별 다중 DB(최대 20개)는
  sys.databases 조회로 동적으로 가져온다. 운영 환경 적용 시 SERVERS 리스트에 IP/PORT 만
  추가하면 된다 (DB 목록은 하드코딩하지 않음).
- 서버(인스턴스) 레벨 지표(클러스터 상태, 디스크, Job 실패, 에러로그)는 해당 서버의 모든 DB 행에
  동일하게 broadcast 되고, DB 레벨 지표(백업, DB 상태)는 DB별로 각각 채워진다.

사용 전 준비
------------
1) 이 스크립트를 실행하는 윈도우 서버에 sqlcmd(또는 Go-sqlcmd) 가 설치되어 있어야 한다.
     - `sqlcmd -?` 로 설치 확인. 없으면 "Microsoft Command Line Utilities for SQL Server"
       또는 `winget install sqlcmd` 로 설치.
2) 각 대상 인스턴스에 sp_daily.sql 의 프로시저들(USP_DAILY_JOB_FAIL, USP_DAILY_BACKUP_LIST,
   usp_daily_fixeddrives)이 master DB에 배포되어 있어야 한다 (이미 운영 중인 것으로 가정).
3) SERVERS 리스트에 접속정보(IP, PORT, 인증방식)를 채운다.
   비밀번호는 코드에 넣지 않고, 실행 계정의 환경변수(기본 DBCHECK_PASSWORD)에서 읽어
   sqlcmd 표준 환경변수 SQLCMDPASSWORD 로 전달한다 (명령줄 -P 미사용 → 프로세스 목록에 노출 안 됨).
     - PowerShell (값에 $ 가 있으면 반드시 작은따옴표):
         [Environment]::SetEnvironmentVariable('DBCHECK_PASSWORD', '<비밀번호>', 'User')
     - 작업 스케줄러로 돌릴 경우 "작업을 실행할 계정"으로 로그인해서 설정하거나 'Machine' 범위로 설정.
4) (선택) db_desc_map.json 으로 "업무설명" 매핑을 관리한다 (없으면 db_desc_map.sample.json 사용).
   매핑에 없는 DB 는 '입력 필요' 로 표시된다.
5) 결과 적재(Oracle) 준비
     - Oracle Instant Client: C:\\Oracle\\instantclient_19_30 (ORACLE_CLIENT_DIR)
     - pip install oracledb
     - 적재 계정 비밀번호를 환경변수 DCHECK_REPO_PASSWORD 에 설정 (3번과 같은 방법)
     - 대상 테이블 IRDMOW.TB_MON_DB_MSSQL_DCHECK 생성 (create_repository_table.sql)

출력
----
- CSV(daily_check_YYYYMMDD_HHMMSS.csv, UTF-8 BOM) 를 먼저 저장하고,
  IRDMOW.TB_MON_DB_MSSQL_DCHECK 에 적재한다. 같은 날 재실행 시 해당 서버 결과는 교체된다.
- 적재 실패 시 종료코드 1 (CSV 는 남아 있음).
- 엑셀 출력용 조회 쿼리는 select_mssql_dcheck_for_excel.sql 참고.
"""

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
# 1. 점검대상 서버 목록 (여기에 IP/PORT 만 추가하면 됨)
# ---------------------------------------------------------------------------
# auth: "windows" (통합인증, sqlcmd -E) 또는 "sql" (SQL 인증, -U + 환경변수 비밀번호)
# password_env: 비밀번호를 담은 환경변수 이름 (비밀번호 자체는 절대 코드/저장소에 넣지 않는다)
DB_LOGIN_USER = "LOGADMIN"
DB_PASSWORD_ENV = "DBCHECK_PASSWORD"

SERVERS: list[dict[str, Any]] = [
    {
        "alias": "SQLSVR1",          # 서버 식별자 (1~4번 서버)
        "host": "10.10.1.11",        # MSFC 리스너(가상 IP) 또는 AG 리스너 IP
        "port": 1433,
        "auth": "sql",
        "user": DB_LOGIN_USER,
        "password_env": DB_PASSWORD_ENV,
        "trust_cert": True,          # sqlcmd 18+ 에서 자체서명 인증서 신뢰(-C)
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

SQLCMD_PATH = "sqlcmd"                 # PATH 에 없으면 전체 경로로 변경
DB_DESC_MAP_PATH = Path(__file__).with_name("db_desc_map.json")                 # 실제 매핑 (없으면 아래 샘플 사용)
DB_DESC_MAP_SAMPLE_PATH = Path(__file__).with_name("db_desc_map.sample.json")
SQLCMD_LOGIN_TIMEOUT_SEC = 15         # sqlcmd -l : 접속 대기 한도
SQLCMD_QUERY_TIMEOUT_SEC = 30         # sqlcmd -t : 서버측 쿼리 실행 한도 (초과 시 서버에서 정상 취소)
SQLCMD_TIMEOUT_SEC = 60               # 파이썬 프로세스 강제종료 한도 (최후 안전장치)
LONG_RUNNING_THRESHOLD_SEC = 60
ERRORLOG_LOOKBACK_DAYS = 1
ERRORLOG_MAX_SIZE_MB = 100            # 현재 에러로그가 이보다 크면 읽지 않음 (대용량 파일 읽기 부하 방지)

# 모든 점검 쿼리 앞에 붙이는 세션 설정 (운영 업무에 영향을 주지 않기 위한 안전장치)
# - READ UNCOMMITTED : msdb 이력/시스템 뷰 조회 시 공유잠금을 잡지 않아 운영 트랜잭션을 블로킹하지 않음
# - LOCK_TIMEOUT 5초  : 혹시 잠금 대기가 생겨도 5초 후 포기 (무한 대기 방지)
# - DEADLOCK_PRIORITY LOW : 교착 발생 시 업무 세션 대신 점검 세션이 희생됨
SESSION_SAFETY_SQL = (
    "SET NOCOUNT ON; "
    "SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED; "
    "SET LOCK_TIMEOUT 5000; "
    "SET DEADLOCK_PRIORITY LOW;\n"
)


# ---------------------------------------------------------------------------
# 2. sqlcmd 실행 공통 헬퍼
# ---------------------------------------------------------------------------
def build_sqlcmd_args(server: dict[str, Any]) -> list[str]:
    args = [
        SQLCMD_PATH,
        "-S", f"{server['host']},{server['port']}",
        "-d", "master",
        "-h", "-1",   # 헤더 제거
        "-W",         # 공백 트림
        "-y", "0",    # 가변길이 컬럼 출력 길이 제한 해제 (FOR JSON 결과 잘림 방지)
        "-b",         # 에러 발생 시 0이 아닌 종료코드 반환
        "-f", "65001",  # 입출력 코드페이지를 UTF-8로 고정. 미지정 시 한글 Windows에서는 CP949(OEM)로
                        # 출력되어 utf-8 디코딩 시 한글(N'정상', 한글 Job명/DB명 등)이 깨진다.
        "-l", str(SQLCMD_LOGIN_TIMEOUT_SEC),
        "-t", str(SQLCMD_QUERY_TIMEOUT_SEC),
    ]
    if server.get("trust_cert", True):
        args.append("-C")

    if server.get("auth") == "windows":
        args.append("-E")
    else:
        # 비밀번호는 -P 로 넘기지 않는다 (명령줄 인자는 작업관리자/프로세스 목록에 그대로 노출됨).
        # -P 가 없으면 sqlcmd 는 환경변수 SQLCMDPASSWORD 를 사용한다 → build_sqlcmd_env() 참고.
        args += ["-U", server["user"]]
    return args


def build_sqlcmd_env(server: dict[str, Any]) -> dict[str, str]:
    """sqlcmd 자식 프로세스에만 SQLCMDPASSWORD 를 넣어준다 (현재 프로세스 환경은 건드리지 않음)."""
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
    """sqlcmd 를 실행하고 stdout 을 반환한다. 실패/타임아웃 시 RuntimeError."""
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
    """sqlcmd로 SQL을 실행하고 'FOR JSON PATH' 결과를 list[dict]로 반환한다.

    SQL Server는 FOR JSON 결과가 길면 여러 행(최대 2033자 단위)으로 쪼개서 반환하므로
    빈 줄/"(N rows affected)" 류의 꼬리 메시지를 제거한 뒤 이어붙여서 파싱해야 한다.
    """
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
# 3. 점검 항목별 쿼리
# ---------------------------------------------------------------------------
SQL_DATABASES = """
SET NOCOUNT ON;
SELECT name AS database_name, state_desc, recovery_model_desc
FROM sys.databases
WHERE database_id > 4            -- master/tempdb/model/msdb 제외
-- OFFLINE/SUSPECT/RECOVERY_PENDING 등 비정상 상태도 그대로 가져와야 DB_상태 점검이 의미가 있음
FOR JSON PATH;
"""

# 기존 USP_DAILY_JOB_FAIL 재사용 (서버 단위 결과)
SQL_JOB_FAIL = """
SET NOCOUNT ON;
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

# 기존 USP_DAILY_BACKUP_LIST 재사용 (DB 단위 결과 - 이미 DB별 1행 구조)
SQL_BACKUP_LIST = """
SET NOCOUNT ON;
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
-- FOR JSON 은 datetime 을 'YYYY-MM-DDTHH:MM:SS.fff' 로 내보내므로 엑셀 결과와 같은 'YYYY-MM-DD HH:MM:SS' 로 변환
SELECT database_name,
       CONVERT(varchar(19), backup_finished, 120) AS backup_finished,
       total_time,
       backup_size_mb
FROM #bkp FOR JSON PATH;
DROP TABLE #bkp;
"""

# DB 파일(데이터파일/트랜잭션로그파일)이 실제 위치한 볼륨의 사용률을 DB 단위로 조회.
# usp_daily_fixeddrives(xp_fixeddrives + sp_OACreate, OLE Automation 필요)를 대체하여
# OLE Automation Procedures 활성화 없이도 동작하고, DB 파일 종류별로 분리해서 볼 수 있다.
# (VIEW SERVER STATE 권한 필요)
SQL_DISK_BY_DB = """
SET NOCOUNT ON;
SELECT
    DB_NAME(mf.database_id) AS database_name,
    mf.type_desc AS file_type,   -- ROWS = 데이터파일, LOG = 트랜잭션 로그파일
    vs.volume_mount_point,
    CAST(100.0 * (vs.total_bytes - vs.available_bytes) / NULLIF(vs.total_bytes, 0) AS decimal(5,2)) AS used_pct
FROM sys.master_files mf
JOIN sys.databases d ON d.database_id = mf.database_id
CROSS APPLY sys.dm_os_volume_stats(mf.database_id, mf.file_id) vs
WHERE mf.type_desc IN ('ROWS', 'LOG')
  AND mf.database_id > 4          -- master/tempdb/model/msdb 제외
  AND d.state_desc = 'ONLINE'     -- 비ONLINE DB 파일은 볼륨 조회 대상에서 제외 (DB_상태 로 별도 표시)
FOR JSON PATH;
"""

# 엔진(설치) 영역: SQL Server는 설치 드라이브를 직접 알려주는 표준 DMV가 없어
# master 시스템DB의 데이터파일이 위치한 볼륨을 근사치로 사용한다
# (대부분의 표준 설치에서 바이너리와 master.mdf 는 동일한 시스템 드라이브에 위치).
# 정확한 설치 경로가 필요하면 레지스트리(xp_instance_regread 'SOFTWARE\\Microsoft\\MSSQLServer\\Setup','SQLBinRoot')
# 를 쓸 수 있으나 추가 권한이 필요해 기본값에서는 제외.
SQL_ENGINE_VOLUME = """
SET NOCOUNT ON;
SELECT TOP 1
    vs.volume_mount_point,
    CAST(100.0 * (vs.total_bytes - vs.available_bytes) / NULLIF(vs.total_bytes, 0) AS decimal(5,2)) AS used_pct
FROM sys.master_files mf
CROSS APPLY sys.dm_os_volume_stats(mf.database_id, mf.file_id) vs
WHERE mf.database_id = 1 AND mf.type_desc = 'ROWS'
FOR JSON PATH;
"""

# usp_Cluster_Status 는 미클러스터 서버에서 결과셋이 없거나(PRINT만 수행), 클러스터 서버에서는
# 3개의 서로 다른 결과셋(노드/공유디스크/MSDTC)을 반환해 INSERT...EXEC 로 한 번에 받기 어렵다.
# 동일한 점검 로직을 단일 JSON 행으로 재구성한 버전.
SQL_CLUSTER_STATUS = """
SET NOCOUNT ON;
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

-- 상태값 표기는 타 DBMS 점검결과(엑셀)와 동일하게: 정상='특이사항 없음', 미클러스터='해당없음'
SELECT
    @@SERVERNAME AS server_name,
    CAST(SERVERPROPERTY('MachineName') AS nvarchar(128)) AS machine_name,          -- MSFC 는 가상 네트워크명(페일오버해도 불변)
    ISNULL(CAST(SERVERPROPERTY('InstanceName') AS nvarchar(128)), N'MSSQLSERVER') AS instance_name,
    CAST(SERVERPROPERTY('ComputerNamePhysicalNetBIOS') AS nvarchar(128)) AS active_node,  -- 현재 서비스 중인 물리 노드
    @IsClustered AS is_clustered,
    CASE WHEN @IsClustered = 0 THEN N'해당없음'
         WHEN @NodeIssues = 0 THEN N'특이사항 없음' ELSE N'비정상' END AS cluster_status,
    CASE WHEN @IsClustered = 0 THEN N'해당없음'
         WHEN @SharedDiskIssues = 0 THEN N'특이사항 없음' ELSE N'비정상' END AS shared_disk_status,
    @MSDTCStatus AS msdtc_status
FOR JSON PATH;
"""

# xp_readerrorlog 직접 호출 (sp_readerrorlog 는 필터 없이 전체 덤프라 자동화에 부적합)
# - 부하 방지: xp_readerrorlog 는 날짜 필터를 줘도 파일 전체를 읽으므로, 에러로그가 재순환되지 않아
#   수백MB~GB 로 커진 경우 CPU/IO 를 크게 쓴다. 먼저 xp_enumerrorlogs 로 현재 로그(0번) 크기를 보고
#   ERRORLOG_MAX_SIZE_MB 를 넘으면 읽지 않고 "로그 과대" 로만 보고한다 (sp_cycle_errorlog 정기 실행 권장).
# - 오탐 방지: 로그인 성공, 백업 성공, DBCC 0 errors 같은 정보성 메시지는 제외하고
#   오류/실패/교착/IO 지연/스택덤프 등 문제성 메시지만 센다.
# 결과는 서버당 요약 1행: log_size_mb, skipped, issue_cnt, login_fail_cnt, latest_msg
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

-- 문제성 메시지만 남김
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

-- 'Error: 18456, Severity...' 행과 바로 뒤 'Login failed...' 행이 쌍으로 찍히므로 Error: 행은 건수에서 제외하고 본문 행만 센다
SELECT
    @size_mb AS log_size_mb,
    0 AS skipped,
    (SELECT COUNT(*) FROM #log WHERE log_text NOT LIKE 'Error:%') AS issue_cnt,
    (SELECT COUNT(*) FROM #log WHERE log_text LIKE 'Login failed%') AS login_fail_cnt,
    (SELECT TOP 1 LEFT(log_text, 80) FROM #log WHERE log_text NOT LIKE 'Error:%' ORDER BY log_date DESC) AS latest_msg
FOR JSON PATH, INCLUDE_NULL_VALUES;
DROP TABLE #log;
"""

# 대표 성능 지표 2종 (Oracle의 Buffer/Library Cache Hit Ratio 에 대응).
# SQL Server는 버퍼풀/플랜캐시가 인스턴스 전역에서 공유되므로 DB 단위로 쪼갤 수 없는 서버(인스턴스) 레벨 지표다.
# - Buffer Cache Hit Ratio: Oracle과 동일한 이름/의미의 카운터가 그대로 존재.
# - Plan Cache Hit Ratio: Oracle의 Library Cache Hit Ratio(파싱/실행계획 재사용률)에 대응하는 SQL Server 지표.
# 둘 다 "비율/베이스" 카운터 쌍으로 노출되므로 (value/base*100) 으로 계산해야 한다.
SQL_PERF_COUNTERS = """
SET NOCOUNT ON;
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

# 사용자 세션의 장시간 실행 요청만 집계.
# - session_id > 50 은 시스템 세션 구분 기준으로 부정확(2005 이후 시스템 세션도 50 초과 가능) → is_user_process 사용
# - 점검 세션 자신(@@SPID), 대기용 명령(WAITFOR, Service Broker 수신 대기 등)은 "쿼리가 느린 것"이 아니므로 제외
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
# 4. 서버 단위 수집 + DB 단위 표준화
# ---------------------------------------------------------------------------
FAIL = "점검실패"
NO_ISSUE = "특이사항 없음"          # 타 DBMS 점검결과(엑셀)와 동일한 정상 표기
DESC_REQUIRED = "입력 필요"          # 업무설명 매핑이 없는 DB

# 결과 1행의 컬럼 정의: (Oracle 적재 컬럼명, CSV 헤더). 순서 = CSV 컬럼 순서 = 엑셀 출력 순서
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
        # 메모장 등에서 저장하면 BOM이 붙을 수 있으므로 utf-8-sig 로 읽는다 (BOM 없어도 정상 동작)
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    return {}


def ratio_pct(value: float | None, base: float | None) -> float | None:
    if not value or not base:
        return None
    return round(value / base * 100, 2)


def build_disk_map(disk_rows: list[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    """DB별로 데이터파일(ROWS)/로그파일(LOG)이 여러 볼륨에 걸쳐 있으면 그 중 사용률이
    가장 높은(가장 위험한) 값을 그 DB의 대표값으로 사용한다."""
    disk_map: dict[str, dict[str, float | None]] = {}
    for r in disk_rows:
        entry = disk_map.setdefault(r["database_name"], {"ROWS": None, "LOG": None})
        cur = entry.get(r["file_type"])
        if cur is None or (r["used_pct"] is not None and r["used_pct"] > cur):
            entry[r["file_type"]] = r["used_pct"]
    return disk_map


def summarize_error_log(rows: list[dict[str, Any]] | None) -> str:
    """SQL_ERROR_LOG 요약 1행을 'ErrorLog' 칸 문자열로 변환 (최대 200자)."""
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

    # 서버(인스턴스) 레벨 지표는 한 번만 수집해서 모든 DB 행에 broadcast.
    # 항목별로 독립 실행: 한 항목(예: 프로시저 미배포, 타임아웃)이 실패해도 해당 칸만 '점검실패' 로
    # 표시하고 나머지 항목과 다음 서버 점검은 계속 진행한다.
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

    # DB 레벨 지표 (백업은 이미 DB 단위 결과)
    backup_rows = try_query("백업", SQL_BACKUP_LIST)
    backup_map = {r["database_name"]: r for r in backup_rows or []}

    host_name = (cluster.get("machine_name") or server["host"]).lower()   # 엑셀 양식과 동일하게 소문자
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
            "ACTIVE_NODE": (cluster.get("active_node") or "-").lower(),
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
# 5. 결과 저장: CSV 출력 (로컬 보관용)
# ---------------------------------------------------------------------------
def export_csv(rows: list[dict[str, Any]], out_path: Path) -> None:
    if not rows:
        print("저장할 결과가 없습니다.")
        return
    # Windows Excel은 BOM이 없는 UTF-8 CSV를 CP949로 해석해 한글이 깨지므로 반드시 utf-8-sig(BOM 포함)로 저장
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([header for _, header in RESULT_COLUMNS])
        for r in rows:
            writer.writerow([r[col] for col, _ in RESULT_COLUMNS])
    print(f"CSV 저장 완료: {out_path}")


# ---------------------------------------------------------------------------
# 6. 결과 저장: Oracle 중앙 저장소 적재 (IRDMOW.TB_MON_DB_MSSQL_DCHECK)
# ---------------------------------------------------------------------------
# - python-oracledb 를 Thick 모드(Oracle Instant Client 사용)로 접속한다.  설치: pip install oracledb
# - 테이블 DDL 은 create_repository_table.sql 참고.
# - 같은 날 다시 실행하면 그날 해당 서버의 기존 결과를 지우고 새 결과로 교체한다 (중복 적재 방지).
# - 비밀번호는 환경변수(REPO_PASSWORD_ENV)에서 읽는다. 코드/저장소에 넣지 않는다.
ORACLE_CLIENT_DIR = r"C:\Oracle\instantclient_19_30"
REPOSITORY = {
    "host": "10.10.20.134",
    "port": 1525,
    "service_name": "DIRST",
    "user": "IRDMOW",
}
REPO_PASSWORD_ENV = "DCHECK_REPO_PASSWORD"
REPOSITORY_TABLE = "IRDMOW.TB_MON_DB_MSSQL_DCHECK"

DATETIME_COLUMNS = {"LAST_FULL_BACKUP_DT", "CHECK_DT"}


def _to_oracle_bind(row: dict[str, Any]) -> dict[str, Any]:
    """결과 1행을 INSERT 바인드 값으로 변환 (일시 문자열 → datetime, CHECK_DATE 추가)."""
    bind = {}
    for col, _ in RESULT_COLUMNS:
        v = row[col]
        if col in DATETIME_COLUMNS and v:
            v = datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
        bind[col] = v
    bind["CHECK_DATE"] = bind["CHECK_DT"].strftime("%Y%m%d")
    return bind


def insert_results_to_repository(rows: list[dict[str, Any]]) -> None:
    """점검결과를 Oracle 테이블에 적재한다. 실패 시 예외를 그대로 올린다 (전체 롤백)."""
    if not rows:
        return

    password = os.environ.get(REPO_PASSWORD_ENV)
    if not password:
        raise RuntimeError(f"환경변수 {REPO_PASSWORD_ENV} 에 적재 DB 비밀번호가 설정되어 있지 않습니다.")

    import oracledb  # 적재 시에만 필요하므로 지연 import (CSV 만 뽑을 때는 미설치여도 동작)

    oracledb.init_oracle_client(lib_dir=ORACLE_CLIENT_DIR)   # Thick 모드
    dsn = f"{REPOSITORY['host']}:{REPOSITORY['port']}/{REPOSITORY['service_name']}"

    binds = [_to_oracle_bind(r) for r in rows]
    columns = ["CHECK_DATE"] + [col for col, _ in RESULT_COLUMNS]
    insert_sql = (
        f"INSERT INTO {REPOSITORY_TABLE} ({', '.join(columns)}) "
        f"VALUES ({', '.join(':' + c for c in columns)})"
    )
    delete_sql = f"DELETE FROM {REPOSITORY_TABLE} WHERE CHECK_DATE = :1 AND SERVER_ALIAS = :2"
    delete_keys = sorted({(b["CHECK_DATE"], b["SERVER_ALIAS"]) for b in binds})

    # with 블록을 commit 없이 빠져나가면(예외 포함) 연결 종료 시 자동 롤백된다
    with oracledb.connect(user=REPOSITORY["user"], password=password, dsn=dsn) as conn:
        with conn.cursor() as cur:
            cur.executemany(delete_sql, delete_keys)
            cur.executemany(insert_sql, binds)
        conn.commit()
    print(f"[REPO] {REPOSITORY_TABLE} {len(binds)}건 적재 완료")


# ---------------------------------------------------------------------------
# 7. 엔트리 포인트
# ---------------------------------------------------------------------------
def main() -> int:
    rows = run_daily_check()

    out_dir = Path(__file__).parent
    out_path = out_dir / f"daily_check_{datetime.now():%Y%m%d_%H%M%S}.csv"
    export_csv(rows, out_path)   # 적재 실패에 대비해 CSV 는 항상 먼저 남긴다

    try:
        insert_results_to_repository(rows)
    except Exception as e:
        print(f"[REPO] 적재 실패 (CSV 는 저장됨: {out_path}): {e}", file=sys.stderr)
        return 1   # 작업 스케줄러에서 실패로 감지되도록 0이 아닌 종료코드

    print(f"\n총 {len(rows)}개 DB 점검 완료.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
