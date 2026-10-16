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
4) (선택) DB_DESC_MAP_PATH 에 지정한 JSON 파일로 "업무설명" 매핑을 관리한다.
   예시는 db_desc_map.sample.json 참고. 없으면 전부 '-' 로 표시된다.

출력
----
- 표준화된 점검결과를 콘솔 요약 + CSV(결과일자_시각.csv, UTF-8 BOM) 로 남긴다.
- insert_results_to_repository() 는 중앙 결과 저장용 INSERT 예시이며, 실제 운영 DB/테이블명에
  맞게 수정해서 사용한다 (create_repository_table.sql 참고).
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 1. 점검대상 서버 목록 (여기에 IP/PORT 만 추가하면 됨)
# ---------------------------------------------------------------------------
# auth: "windows" (통합인증, sqlcmd -E) 또는 "sql" (SQL 인증, -U/-P)
SERVERS: list[dict[str, Any]] = [
    {
        "alias": "SQLSVR1",          # 서버 식별자 (1~4번 서버)
        "host": "10.10.1.11",        # MSFC 리스너(가상 IP) 또는 AG 리스너 IP
        "port": 1433,
        "auth": "windows",
        "user": None,
        "password": None,
        "trust_cert": True,          # sqlcmd 18+ 에서 자체서명 인증서 신뢰(-C)
    },
    {
        "alias": "SQLSVR2",
        "host": "10.10.1.12",
        "port": 1433,
        "auth": "windows",
        "user": None,
        "password": None,
        "trust_cert": True,
    },
    {
        "alias": "SQLSVR3",
        "host": "10.10.1.13",
        "port": 1433,
        "auth": "sql",
        "user": "svc_dbcheck",
        "password": "CHANGE_ME",     # 운영 적용 시 환경변수/Vault 등으로 분리 권장
        "trust_cert": True,
    },
    {
        "alias": "SQLSVR4",
        "host": "10.10.1.14",
        "port": 1433,
        "auth": "sql",
        "user": "svc_dbcheck",
        "password": "CHANGE_ME",
        "trust_cert": True,
    },
]

SQLCMD_PATH = "sqlcmd"                 # PATH 에 없으면 전체 경로로 변경
DB_DESC_MAP_PATH = Path(__file__).with_name("db_desc_map.sample.json")
SQLCMD_TIMEOUT_SEC = 60
LONG_RUNNING_THRESHOLD_SEC = 60
ERRORLOG_LOOKBACK_DAYS = 1


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
    ]
    if server.get("trust_cert", True):
        args.append("-C")

    if server.get("auth") == "windows":
        args.append("-E")
    else:
        args += ["-U", server["user"], "-P", server["password"]]
    return args


def run_sqlcmd(server: dict[str, Any], sql: str) -> list[dict[str, Any]]:
    """sqlcmd로 SQL을 실행하고 'FOR JSON PATH' 결과를 list[dict]로 반환한다.

    SQL Server는 FOR JSON 결과가 길면 여러 행(최대 2033자 단위)으로 쪼개서 반환하므로
    빈 줄/"(N rows affected)" 류의 꼬리 메시지를 제거한 뒤 이어붙여서 파싱해야 한다.
    """
    args = build_sqlcmd_args(server)
    proc = subprocess.run(
        args + ["-Q", sql],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SQLCMD_TIMEOUT_SEC,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"[{server['alias']}] sqlcmd 실행 실패 (code={proc.returncode})\n"
            f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
        )

    lines = []
    for line in proc.stdout.splitlines():
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
  AND state_desc <> 'OFFLINE'
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
SELECT * FROM #bkp FOR JSON PATH;
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
CROSS APPLY sys.dm_os_volume_stats(mf.database_id, mf.file_id) vs
WHERE mf.type_desc IN ('ROWS', 'LOG')
  AND mf.database_id > 4          -- master/tempdb/model/msdb 제외
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
        SET @MSDTCStatus = N'정상';
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;
        SET @MSDTCStatus = N'비정상';
    END CATCH
END

SELECT
    @@SERVERNAME AS server_name,
    CAST(SERVERPROPERTY('ComputerNamePhysicalNetBIOS') AS nvarchar(128)) AS active_node,
    @IsClustered AS is_clustered,
    CASE WHEN @IsClustered = 0 THEN N'단독서버'
         WHEN @NodeIssues = 0 THEN N'정상' ELSE N'비정상' END AS cluster_status,
    CASE WHEN @IsClustered = 0 THEN N'해당없음'
         WHEN @SharedDiskIssues = 0 THEN N'정상' ELSE N'비정상' END AS shared_disk_status,
    @MSDTCStatus AS msdtc_status
FOR JSON PATH;
"""

# xp_readerrorlog 직접 호출 (sp_readerrorlog 는 필터 없이 전체 덤프라 자동화에 부적합)
SQL_ERROR_LOG = f"""
SET NOCOUNT ON;
IF OBJECT_ID('tempdb..#log') IS NOT NULL DROP TABLE #log;
CREATE TABLE #log (log_date datetime, process_info nvarchar(50), log_text nvarchar(max));
DECLARE @start datetime = DATEADD(day, -{ERRORLOG_LOOKBACK_DAYS}, GETDATE());
INSERT INTO #log EXEC master.dbo.xp_readerrorlog 0, 1, NULL, NULL, @start, NULL, N'desc';
DELETE FROM #log WHERE log_text LIKE '%Logging SQL Server messages%';  -- 로그 시작 안내문 제외
SELECT TOP 50 * FROM #log FOR JSON PATH;
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

SQL_LONG_RUNNING = f"""
SET NOCOUNT ON;
SELECT
    DB_NAME(r.database_id) AS database_name,
    MAX(r.total_elapsed_time) / 1000 AS max_elapsed_sec,
    COUNT(*) AS session_cnt
FROM sys.dm_exec_requests r
WHERE r.total_elapsed_time > {LONG_RUNNING_THRESHOLD_SEC * 1000}
  AND r.session_id > 50
GROUP BY DB_NAME(r.database_id)
FOR JSON PATH;
"""


# ---------------------------------------------------------------------------
# 4. 서버 단위 수집 + DB 단위 표준화
# ---------------------------------------------------------------------------
def load_db_desc_map() -> dict[str, dict[str, str]]:
    if DB_DESC_MAP_PATH.exists():
        # 메모장 등에서 저장하면 BOM이 붙을 수 있으므로 utf-8-sig 로 읽는다 (BOM 없어도 정상 동작)
        with open(DB_DESC_MAP_PATH, encoding="utf-8-sig") as f:
            return json.load(f)
    return {}


def ratio_pct(value: float | None, base: float | None) -> float | None:
    if not value or not base:
        return None
    return round(value / base * 100, 1)


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


def check_one_server(server: dict[str, Any], db_desc_map: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    alias = server["alias"]
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows: list[dict[str, Any]] = []

    try:
        databases = run_sqlcmd(server, SQL_DATABASES)
    except Exception as e:
        print(f"[{alias}] 서버 접속/조회 실패, 건너뜀: {e}", file=sys.stderr)
        return [{
            "서버번호": alias, "호스트명": server["host"], "DB명": "-", "업무설명": "-",
            "DB_상태": "점검실패", "클러스터_상태": "-", "공유디스크_상태": "-", "MSDTC_상태": "-",
            "Job_실패_점검": "-",
            "Buffer_Cache_Hit_Ratio(%)": None, "Plan_Cache_Hit_Ratio(%)": None,
            "엔진영역_사용률(%)": None,
            "데이터파일영역_사용률(%)": None,
            "트랜잭션로그영역_사용률(%)": None,
            "최근Full백업_완료일시": None, "백업_소요시간": "-", "백업_사이즈(MB)": None,
            "장시간쿼리_점검": "-", "에러로그_점검": "-", "점검일시": checked_at,
        }]

    # 서버(인스턴스) 레벨 지표는 한 번만 수집해서 모든 DB 행에 broadcast
    cluster = (run_sqlcmd(server, SQL_CLUSTER_STATUS) or [{}])[0]
    job_fail_rows = run_sqlcmd(server, SQL_JOB_FAIL)
    job_fail_status = (
        "정상" if not job_fail_rows
        else "; ".join(f"{r['job_name']}({r['step_name']})" for r in job_fail_rows)
    )
    perf = (run_sqlcmd(server, SQL_PERF_COUNTERS) or [{}])[0]
    buffer_hit_ratio = ratio_pct(perf.get("buffer_hit_value"), perf.get("buffer_hit_base"))
    plan_hit_ratio = ratio_pct(perf.get("plan_hit_value"), perf.get("plan_hit_base"))
    disk_rows = run_sqlcmd(server, SQL_DISK_BY_DB)
    disk_map = build_disk_map(disk_rows)
    engine_row = (run_sqlcmd(server, SQL_ENGINE_VOLUME) or [{}])[0]
    engine_used_pct = engine_row.get("used_pct")
    error_log_rows = run_sqlcmd(server, SQL_ERROR_LOG)
    error_log_status = "특이사항 없음" if not error_log_rows else f"{len(error_log_rows)}건 발견(확인필요)"
    long_running_rows = run_sqlcmd(server, SQL_LONG_RUNNING)
    long_running_map = {r["database_name"]: r for r in long_running_rows}

    # DB 레벨 지표 (백업은 이미 DB 단위 결과)
    backup_rows = run_sqlcmd(server, SQL_BACKUP_LIST)
    backup_map = {r["database_name"]: r for r in backup_rows}

    host_display = cluster.get("server_name") or server["host"]
    desc_map_for_server = db_desc_map.get(alias, {})

    for db in databases:
        dbname = db["database_name"]
        backup = backup_map.get(dbname)
        longrun = long_running_map.get(dbname)
        area = disk_map.get(dbname, {"ROWS": None, "LOG": None})
        data_used_pct = area["ROWS"]
        log_used_pct = area["LOG"]

        rows.append({
            "서버번호": alias,
            "호스트명": host_display,
            "DB명": dbname,
            "업무설명": desc_map_for_server.get(dbname, "-"),
            "DB_상태": "정상" if db["state_desc"] == "ONLINE" else db["state_desc"],
            "클러스터_상태": cluster.get("cluster_status", "-"),
            "공유디스크_상태": cluster.get("shared_disk_status", "-"),
            "MSDTC_상태": cluster.get("msdtc_status", "-"),
            "Job_실패_점검": job_fail_status,
            "Buffer_Cache_Hit_Ratio(%)": buffer_hit_ratio,
            "Plan_Cache_Hit_Ratio(%)": plan_hit_ratio,
            "엔진영역_사용률(%)": engine_used_pct,
            "데이터파일영역_사용률(%)": data_used_pct,
            "트랜잭션로그영역_사용률(%)": log_used_pct,
            "최근Full백업_완료일시": backup["backup_finished"] if backup else None,
            "백업_소요시간": backup["total_time"] if backup else "확인필요",
            "백업_사이즈(MB)": backup["backup_size_mb"] if backup else None,
            "장시간쿼리_점검": (
                "정상" if not longrun
                else f"{longrun['session_cnt']}건, 최대 {longrun['max_elapsed_sec']}초"
            ),
            "에러로그_점검": error_log_status,
            "점검일시": checked_at,
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
# 5. 결과 저장: CSV 출력
# ---------------------------------------------------------------------------
def export_csv(rows: list[dict[str, Any]], out_path: Path) -> None:
    if not rows:
        print("저장할 결과가 없습니다.")
        return
    fieldnames = list(rows[0].keys())
    # Windows Excel은 BOM이 없는 UTF-8 CSV를 CP949로 해석해 한글이 깨지므로 반드시 utf-8-sig(BOM 포함)로 저장
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"CSV 저장 완료: {out_path}")


# ---------------------------------------------------------------------------
# 6. 결과 저장: 중앙 저장소 INSERT 예시 (실제 환경에 맞게 변경 예정)
# ---------------------------------------------------------------------------
# 대상 테이블 DDL 은 create_repository_table.sql 참고.
REPOSITORY_SERVER = {
    "alias": "REPO",
    "host": "10.10.1.100",     # 점검결과를 적재할 중앙 repository SQL Server
    "port": 1433,
    "auth": "windows",
    "user": None,
    "password": None,
    "trust_cert": True,
}
REPOSITORY_TABLE = "DBCHECK.dbo.DAILY_CHECK_RESULT"


def _sql_literal(value: Any) -> str:
    """Python 값을 T-SQL 리터럴 문자열로 변환 (예시 구현, 운영 시 파라미터 바인딩 권장)."""
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("'", "''")
    return f"N'{text}'"


def insert_results_to_repository(rows: list[dict[str, Any]], chunk_size: int = 200) -> None:
    """점검결과를 중앙 저장소 테이블에 적재하는 예시.

    - sqlcmd 로 일괄 INSERT 문을 실행한다 (대량일 때는 chunk_size 단위로 나눠 실행).
    - 운영 적용 시: 컬럼명/타입을 실제 REPOSITORY_TABLE 스키마에 맞추고,
      자격증명은 환경변수/비밀관리시스템에서 읽어오도록 교체할 것.
    """
    if not rows:
        return

    columns = [
        "server_alias", "host_name", "db_name", "biz_desc", "db_status",
        "cluster_status", "shared_disk_status", "msdtc_status", "job_fail_check",
        "buffer_cache_hit_ratio", "plan_cache_hit_ratio",
        "engine_used_pct", "data_used_pct", "log_used_pct",
        "last_full_backup_finished", "backup_duration", "backup_size_mb",
        "long_running_check", "error_log_check", "checked_at",
    ]
    key_order = [
        "서버번호", "호스트명", "DB명", "업무설명", "DB_상태", "클러스터_상태",
        "공유디스크_상태", "MSDTC_상태", "Job_실패_점검",
        "Buffer_Cache_Hit_Ratio(%)", "Plan_Cache_Hit_Ratio(%)",
        "엔진영역_사용률(%)", "데이터파일영역_사용률(%)", "트랜잭션로그영역_사용률(%)",
        "최근Full백업_완료일시", "백업_소요시간", "백업_사이즈(MB)",
        "장시간쿼리_점검", "에러로그_점검", "점검일시",
    ]

    for i in range(0, len(rows), chunk_size):
        chunk = rows[i:i + chunk_size]
        values_sql = ",\n".join(
            "(" + ", ".join(_sql_literal(r[k]) for k in key_order) + ")"
            for r in chunk
        )
        insert_sql = (
            f"SET NOCOUNT ON;\n"
            f"INSERT INTO {REPOSITORY_TABLE} ({', '.join(columns)})\n"
            f"VALUES\n{values_sql};"
        )
        args = build_sqlcmd_args(REPOSITORY_SERVER)
        proc = subprocess.run(
            args + ["-Q", insert_sql],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            print(f"[REPO] INSERT 실패: {proc.stderr}", file=sys.stderr)
        else:
            print(f"[REPO] {len(chunk)}건 적재 완료")


# ---------------------------------------------------------------------------
# 7. 엔트리 포인트
# ---------------------------------------------------------------------------
def main() -> None:
    rows = run_daily_check()

    out_dir = Path(__file__).parent
    out_path = out_dir / f"daily_check_{datetime.now():%Y%m%d_%H%M%S}.csv"
    export_csv(rows, out_path)

    # 중앙 저장소 적재는 기본적으로 비활성화 (예시 코드이므로 환경 맞춰 조정 후 주석 해제)
    # insert_results_to_repository(rows)

    print(f"\n총 {len(rows)}개 DB 점검 완료.")


if __name__ == "__main__":
    main()
