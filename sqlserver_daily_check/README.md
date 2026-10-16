# SQL Server 일일점검 자동화

sqlcmd 로 점검대상 SQL Server(2노드 MSFC 인스턴스 × 4대)에 접속해 점검 쿼리를 실행하고,
결과를 타 DBMS 일일점검결과와 같은 **DB 단위 1행** 형식으로 만들어 CSV 저장 + Oracle 적재한다.

| 파일 | 설명 |
|---|---|
| `sql_server_daily_check.py` | 점검 스크립트 |
| `dcheck_config.sample.json` | 설정 파일 예시 (서버/적재 DB 접속정보, sqlcmd 경로) |
| `create_repository_table.sql` | 적재 테이블 DDL (Oracle `IRDMOW.TB_MON_DB_MSSQL_DCHECK`) |
| `select_mssql_dcheck_for_excel.sql` | 엑셀 출력 스크립트에 넣을 조회 쿼리 |
| `db_desc_map.sample.json` | 서버/DB별 업무설명 매핑 예시 |
| `daily_check_sample_20261016_090000.csv` | 결과 CSV 예시 |

## 실행 준비

1. **sqlcmd 설치** (`sqlcmd -?` 로 확인). 없으면 "Microsoft Command Line Utilities for SQL Server" 또는 `winget install sqlcmd`.
2. **점검대상 프로시저**: 각 인스턴스 master DB 에 `USP_DAILY_JOB_FAIL`, `USP_DAILY_BACKUP_LIST` 가 배포되어 있어야 한다 (sp_daily.sql).
3. **설정 파일**: `dcheck_config.sample.json` 을 `dcheck_config.json` 으로 복사해서 실제 값으로 수정 (아래 '설정 파일').
   `dcheck_config.json` 은 저장소/배포 파일에 포함되지 않으므로 스크립트를 새 버전으로 덮어써도 유지된다.
4. **비밀번호**: `dcheck_config.json` 의 `sql_login.password`(LOGADMIN), `repository.password`(IRDMOW) 의 `INPUT_HERE` 를 실제 값으로 바꾼다.
   - JSON 이므로 비밀번호에 `\` 나 `"` 가 있으면 `\\`, `\"` 로 쓴다 (`$` 는 그대로).
   - `dcheck_config.json` 은 배포 파일에 포함되지 않으므로 새 버전으로 덮어써도 유지된다. git 에도 올라가지 않는다(.gitignore).
   - 서버마다 비밀번호가 다르면 `servers` 의 해당 서버에 `"password": "..."` 를 넣는다.
   - `INPUT_HERE` 그대로 두면 환경변수(`password_env`, 기본 `DBCHECK_PASSWORD` / `DCHECK_REPO_PASSWORD`)에서 읽는다.
     이미 열려 있던 터미널/IDE 에는 새 환경변수가 보이지 않으므로 스크립트가 레지스트리에서 직접 읽는다.
   - 파일에 비밀번호가 평문으로 있으므로 실행 계정만 읽을 수 있게 폴더 권한을 제한하는 것을 권장한다.
5. **Oracle 적재**: Instant Client, python `oracledb` (설치됨). `create_repository_table.sql` 로 테이블 생성 (최초 1회).
6. **업무설명 매핑**: `db_desc_map.sample.json` 을 `db_desc_map.json` 으로 복사해서 `{서버번호: {DB명: 업무설명}}` 형태로 채운다.
   `db_desc_map.json` 이 있으면 그 파일을, 없으면 샘플을 사용한다. 매핑에 없는 DB 는 `입력 필요` 로 표시된다.
   (SQL Server 는 Oracle/MySQL 처럼 조회 가능한 업무설명 메타데이터가 없어서 파일로 관리)

## 실행

```
python D:\scripts\sqlserver_daily_check\sql_server_daily_check.py
python sql_server_daily_check.py --config E:\conf\dcheck_config.json   # 설정 파일을 다른 위치에 둘 때
```

어느 폴더/드라이브에서 실행해도 된다. 설정 파일, 업무설명 매핑, 상대경로는 실행 위치가 아니라 **스크립트 폴더 기준**으로 찾는다.

1. 4대 서버를 순서대로 점검
2. `daily_check_YYYYMMDD_HHMMSS.csv` 저장 (UTF-8 BOM → Windows Excel 에서 한글 정상)
3. Oracle 적재: 같은 `REG_DT`(점검일자)+서버의 기존 결과를 지우고 새로 INSERT → 같은 날 재실행해도 중복 없음.

| 종료코드 | 의미 |
|---|---|
| 0 | 정상 |
| 1 | 점검은 했으나 Oracle 적재 실패 (CSV 는 남음, 전체 롤백) |
| 2 | 설정 오류 (설정 파일 없음/형식 오류, sqlcmd 를 찾을 수 없음) |

## 설정 파일 (`dcheck_config.json`)

| 항목 | 설명 |
|---|---|
| `sqlcmd_path` | SQLCMD.EXE 전체 경로. **비워두면** PATH → `%ProgramFiles%\Microsoft SQL Server\Client SDK\ODBC\*\Tools\Binn` → `%ProgramFiles%\Microsoft SQL Server\*\Tools\Binn` → `%ProgramFiles%\SqlCmd` 순서로 자동 탐색. 실행 시 첫 줄에 사용한 경로가 출력됨 |
| `output_dir` | CSV 저장 폴더. 비우면 스크립트 폴더. 없으면 자동 생성 |
| `sql_login` | 점검대상 공통 SQL 인증 계정(`user`), 비밀번호(`password`), 비밀번호 미입력 시 읽을 환경변수 이름(`password_env`) |
| `servers` | 점검대상 목록. `alias`, `host`, `port` 필수. 서버별로 `user`, `password`, `password_env`, `auth`(`sql`/`windows`), `trust_cert` 를 넣으면 공통값 대신 사용. DB 목록은 `sys.databases` 에서 자동 조회 |
| `repository` | 적재 Oracle 접속정보, 비밀번호(`password`), 테이블명, Instant Client 경로(`oracle_client_dir`) |

경로 작성 규칙
- JSON 에서는 `\` 를 `\\` 로 두 번 쓰거나 `/` 로 쓴다. 예) `"C:\\Oracle\\instantclient_19_30"` 또는 `"C:/Oracle/instantclient_19_30"`
- 스크립트와 다른 드라이브에 있으면 **드라이브 문자부터 전체 경로**로 쓴다 (`C:\...`). 드라이브 없이 쓴 상대경로는 스크립트 폴더 기준.
- `%ProgramFiles%` 같은 환경변수를 쓸 수 있다.

스크립트 상단 상수 (거의 바꿀 일 없음)

| 항목 | 값 | 설명 |
|---|---|---|
| `SQLCMD_LOGIN_TIMEOUT_SEC` / `SQLCMD_QUERY_TIMEOUT_SEC` | 15 / 30 | sqlcmd `-l` / `-t` |
| `SQLCMD_TIMEOUT_SEC` | 60 | sqlcmd 프로세스 강제종료 한도 (최후 안전장치) |
| `LONG_RUNNING_THRESHOLD_SEC` | 60 | 장시간 수행SQL 기준 |
| `ERRORLOG_LOOKBACK_DAYS` | 1 | 에러로그 조회 기간 |
| `ERRORLOG_MAX_SIZE_MB` | 100 | 현재 에러로그가 이보다 크면 읽지 않음 |

## sqlcmd 실행 실패 (code=N) 확인

오류 메시지의 `STDOUT` / `STDERR` 내용으로 원인을 본다.

| 메시지 | 원인 / 조치 |
|---|---|
| `Login failed for user 'LOGADMIN'` (Msg 18456) | 계정/비밀번호 확인 |
| `A network-related or instance-specific error` / `Login timeout expired` | IP/PORT, 방화벽, SQL Browser 확인 |
| `Could not find stored procedure 'master.dbo.USP_DAILY_...'` | 해당 서버에 sp_daily.sql 프로시저 배포 필요 (그 항목만 '점검실패') |
| `certificate chain was issued by an authority that is not trusted` | `trust_cert` 가 true 인지 확인 |
| `'-...' : Unknown Option` | 설치된 sqlcmd 버전이 옵션을 지원하지 않음 → 최신 sqlcmd 설치 |

## 점검 항목

서버(인스턴스) 단위 항목은 그 서버의 모든 DB 행에 같은 값으로 들어가고, DB 단위 항목은 DB별로 채워진다.
항목별로 독립 실행하므로 한 항목이 실패해도(프로시저 미배포, 타임아웃 등) 그 칸만 `점검실패` 로 표시하고 계속 진행한다.
서버 접속 자체가 실패하면 그 서버는 `DB명 = -`, `DBMS 서비스 = 점검실패` 1행으로 남긴다.

| 결과 컬럼 | 단위 | 방법 |
|---|---|---|
| 호스트명 / Active 노드 | 서버 | `SERVERPROPERTY('ComputerNamePhysicalNetBIOS')` - 현재 서비스 중인 물리 노드명(소문자). 페일오버 시 바뀐다 |
| 인스턴스명 | 서버 | `SERVERPROPERTY('InstanceName')`, 기본 인스턴스는 `MSSQLSERVER` |
| DBMS 서비스 | DB | `sys.databases.state_desc`. ONLINE=`정상 서비스`, 그 외(OFFLINE, SUSPECT, RECOVERY_PENDING 등)는 상태값 그대로 |
| 클러스터 / 공유디스크 / MSDTC | 서버 | `dm_os_cluster_nodes`, `dm_io_cluster_shared_drives`, 빈 분산 트랜잭션 시작/커밋. 미클러스터는 `해당없음`. 기존 `usp_Cluster_Status` 는 결과셋이 3개라 한 번에 받기 어려워 같은 로직을 1행으로 재구성 |
| ErrorLog | 서버 | `xp_readerrorlog` 최근 1일. 오류/실패/교착/IO 지연/스택덤프 등 문제성 메시지만 집계하고 로그인 성공, DBCC 0 errors 등 정보성 메시지는 제외. `Error: 18456` 행과 뒤따르는 `Login failed` 행이 쌍으로 찍히므로 `Error:` 행은 건수에서 제외 |
| Buffer / Plan Cache Hit Ratio | 서버 | `dm_os_performance_counters` 의 값/base 카운터 쌍으로 계산. Plan Cache 는 Oracle Library Cache Hit Ratio 대응 지표. 둘 다 인스턴스 전역 공유라 DB 단위로 나눌 수 없음 |
| 엔진 영역 | 서버 | master 데이터파일이 있는 볼륨의 사용률 (설치 드라이브를 직접 알려주는 DMV 가 없어 근사치) |
| 데이터 / 트랜잭션로그 영역 | DB | `dm_os_volume_stats` 로 DB 파일이 있는 볼륨의 사용률. 여러 볼륨이면 가장 높은 값. ONLINE DB 만 조회. 기존 `usp_daily_fixeddrives` 와 달리 OLE Automation 불필요 |
| 데이터백업(Full) | DB | `USP_DAILY_BACKUP_LIST`. 없으면 `확인필요` |
| Agent Job 점검 | 서버 | `USP_DAILY_JOB_FAIL`. 실패 Job 은 `Job명(Step명)` 목록 |
| 장시간 수행SQL 점검 | DB | `dm_exec_requests` 중 사용자 세션(`is_user_process`)만, 60초 초과. 점검 세션 자신과 WAITFOR / Service Broker 대기는 제외 |

정상 표기는 타 DBMS 시트와 같이 서비스 상태는 `정상 서비스`, 점검 항목은 `특이사항 없음`, 비율/사용률은 소수 둘째 자리.

## Oracle 적재 테이블

`IRDMOW.TB_MON_DB_MSSQL_DCHECK` - PK `(REG_DT, SERVER_ALIAS, DB_NAME)`

- `REG_DT` 는 점검일자 `YYYYMMDD` (VARCHAR2), `CHECK_DT` 는 점검일시 (DATE)
- 문자 컬럼은 `VARCHAR2(n CHAR)` → 한글도 n 글자까지 저장
- DDL 의 한글 COMMENT 가 깨지지 않도록 SQL Developer 로 실행하거나, sqlplus 는 `NLS_LANG=KOREAN_KOREA.AL32UTF8` 설정 후 실행
- 접속은 python-oracledb Thick 모드(Instant Client). Instant Client 19 는 Python 과 비트수(64/32)가 같아야 하고 VC++ 2017 재배포 패키지가 필요

## 엑셀 출력

기존 엑셀 출력 스크립트에 `select_mssql_dcheck_for_excel.sql` 의 SELECT 를 추가해서 사용한다.

- 기본은 오늘(`REG_DT = TO_CHAR(SYSDATE, 'YYYYMMDD')`). 다른 날짜는 `REG_DT = :REG_DT` 바인드로 변경
- **맥스게이지 프로세스** 는 별도 페이지에서 직접 확인하므로 `특이사항 없음` 고정값으로 출력
- 끝에 세미콜론 없음 (스크립트에서 실행하기 위함)

## 한글 인코딩 / sqlcmd 옵션

- `-W`(공백 제거)는 `-y`(긴 결과 잘림 방지)와 함께 쓸 수 없어 사용하지 않는다 (같이 주면 sqlcmd 가 code=1 로 종료). 공백은 스크립트에서 제거한다.

- sqlcmd 는 한글 Windows 에서 기본 CP949 로 출력하므로 `-f 65001` 로 UTF-8 고정 (미지정 시 결과 한글 깨짐)
- CSV 는 BOM 포함 UTF-8 (BOM 없으면 Excel 이 CP949 로 읽어 깨짐)
- 업무설명 JSON 은 BOM 유무 상관없이 읽음 (메모장 저장본 대응)

## 보안

- 비밀번호는 `dcheck_config.json`(배포/저장소 제외) 또는 환경변수에서 받는다. sqlcmd 에는 `-P` 대신 `SQLCMDPASSWORD` 환경변수로 넘겨 프로세스 목록에 노출되지 않는다.
- Oracle 은 바인드 변수로 INSERT 한다.

## 운영 영향(부하·지연) 검토

점검은 **조회 전용**이다. 사용자 DB 에 쓰기/DDL/DBCC/인덱스 작업은 없고, tempdb 에 임시테이블(#)만 만들었다 지운다.
서버당 sqlcmd 접속 9회를 순차 실행하며, 각 쿼리는 시스템 뷰·DMV 위주라 대부분 수 ms~수백 ms 수준이다.

모든 쿼리 앞에 다음 세션 설정과 타임아웃을 적용한다 (`SESSION_SAFETY_SQL`).

| 장치 | 효과 |
|---|---|
| `READ UNCOMMITTED` | msdb 백업/Job 이력 조회 시 공유잠금을 잡지 않아 업무·백업 트랜잭션을 블로킹하지 않음 |
| `LOCK_TIMEOUT 5000` | 잠금 대기가 생겨도 5초 후 포기 |
| `DEADLOCK_PRIORITY LOW` | 교착 시 업무 세션 대신 점검 세션이 희생 |
| `sqlcmd -t 30` / `-l 15` | 쿼리 30초, 접속 15초 초과 시 서버측에서 정상 취소 |
| 에러로그 크기 확인 | 현재 ERRORLOG 가 100MB 초과면 읽지 않음 (아래 참고) |

### 항목별 위험도

| 항목 | 위험 | 내용 / 조치 |
|---|---|---|
| DB 목록, 디스크(`dm_os_volume_stats`), 성능카운터, 장시간쿼리(`dm_exec_requests`) | 낮음 | 메모리상 DMV 조회 |
| 에러로그(`xp_readerrorlog`) | **중간** | 날짜 필터를 줘도 파일 전체를 읽음. 에러로그가 재순환되지 않아 수백MB~GB 면 CPU/IO 부하 → 먼저 `xp_enumerrorlogs` 로 크기를 보고 100MB 초과 시 미조회, "로그 과대" 로 보고. **`sp_cycle_errorlog` 를 주 1회 등 정기 실행 권장** |
| MSDTC(`BEGIN DISTRIBUTED TRANSACTION`) | 낮음~중간 | 클러스터 인스턴스에서만 실행. 빈 분산 트랜잭션을 열었다 커밋하며 데이터 잠금은 없음. MSDTC 서비스가 응답하지 않으면 대기가 생길 수 있으나 `-t 30` 으로 끊김 |
| `USP_DAILY_JOB_FAIL`, `USP_DAILY_BACKUP_LIST` | **확인 필요** | 프로시저 본문(sp_daily.sql)을 아직 검토하지 못함. msdb 이력(`backupset`, `sysjobhistory`)이 정리되지 않아 매우 크면 조회가 느릴 수 있음 → `sp_delete_backuphistory` 정기 실행 여부 확인 |

### 권한

현재 `LOGADMIN` 은 sysadmin 이다. 스크립트는 쓰기 작업을 하지 않으므로 서버 중단을 일으킬 경로는 없지만,
계정 유출 시 피해 범위를 줄이려면 아래 최소 권한 계정으로 분리하는 것을 권장한다.

- `VIEW SERVER STATE` (DMV), msdb `db_datareader` + `SQLAgentReaderRole` (Job/백업 이력)
- master 의 `USP_DAILY_*` 프로시저 `EXECUTE`
- `xp_readerrorlog` / `xp_enumerrorlogs` 는 `securityadmin` 이상 필요
- MSDTC 점검(`BEGIN DISTRIBUTED TRANSACTION`) 가능 여부는 최소 권한 계정으로 별도 테스트 필요
