# SQL Server 일일점검 자동화

| 파일 | 설명 |
|---|---|
| `sql_server_daily_check.py` | 점검 스크립트 (sqlcmd 로 각 인스턴스 조회 → DB 단위 1행 CSV) |
| `create_repository_table.sql` | 중앙 저장소 테이블 DDL |
| `db_desc_map.sample.json` | 서버/DB별 업무설명 매핑 예시 |
| `daily_check_sample_20261016_090000.csv` | 결과 CSV 예시 |

## 실행 준비

1. 실행 서버에 `sqlcmd` 설치 (`sqlcmd -?` 로 확인)
2. 접속 계정은 `LOGADMIN` (SQL 인증). **비밀번호는 코드/저장소에 넣지 않고** 실행 계정의 환경변수로 설정한다.
   ```powershell
   # 값에 $ 가 있으므로 반드시 작은따옴표 사용
   [Environment]::SetEnvironmentVariable('DBCHECK_PASSWORD', '<비밀번호>', 'User')
   ```
   스크립트는 이 값을 sqlcmd 의 `SQLCMDPASSWORD` 로만 넘기므로 명령줄(프로세스 목록)에 비밀번호가 보이지 않는다.
3. `python sql_server_daily_check.py` 실행 → 같은 폴더에 `daily_check_YYYYMMDD_HHMMSS.csv` (UTF-8 BOM, Excel 에서 바로 열림)

## 운영 영향(부하·지연) 검토

점검은 **조회 전용**이다. 사용자 DB 에 쓰기/DDL/DBCC/인덱스 작업은 없고, tempdb 에 임시테이블(#)만 만들었다 지운다.
서버당 sqlcmd 접속 9회를 순차 실행하며, 각 쿼리는 시스템 뷰·DMV 위주라 대부분 수 ms~수백 ms 수준이다.

모든 쿼리에 다음 안전장치를 적용했다.

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
| 에러로그(`xp_readerrorlog`) | **중간** | 날짜 필터를 줘도 파일 전체를 읽음. 에러로그가 재순환되지 않아 수백MB~GB 면 CPU/IO 부하 → 100MB 초과 시 미조회하고 "로그 과대" 로 보고. **`sp_cycle_errorlog` 를 주 1회 등 정기 실행 권장** |
| MSDTC(`BEGIN DISTRIBUTED TRANSACTION`) | 낮음~중간 | 클러스터 인스턴스에서만 실행. 빈 분산 트랜잭션을 열었다 커밋하며 데이터 잠금은 없음. MSDTC 서비스가 응답하지 않으면 대기가 생길 수 있으나 `-t 30` 으로 끊김 |
| `USP_DAILY_JOB_FAIL`, `USP_DAILY_BACKUP_LIST` | **확인 필요** | 프로시저 본문(sp_daily.sql)을 아직 검토하지 못함. msdb 이력(`backupset`, `sysjobhistory`)이 정리되지 않아 매우 크면 조회가 느릴 수 있음 → `sp_delete_backuphistory` 정기 실행 여부 확인 |

### 권한

현재 `LOGADMIN` 은 sysadmin 이다. 스크립트는 쓰기 작업을 하지 않으므로 서버 중단을 일으킬 경로는 없지만,
계정 유출 시 피해 범위를 줄이려면 아래 최소 권한 계정으로 분리하는 것을 권장한다.

- `VIEW SERVER STATE` (DMV), msdb `db_datareader` + `SQLAgentReaderRole` (Job/백업 이력)
- master 의 `USP_DAILY_*` 프로시저 `EXECUTE`
- `xp_readerrorlog` / `xp_enumerrorlogs` 는 `securityadmin` 이상 필요
- MSDTC 점검(`BEGIN DISTRIBUTED TRANSACTION`) 가능 여부는 최소 권한 계정으로 별도 테스트 필요
