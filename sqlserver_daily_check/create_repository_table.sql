-- 일일점검결과 중앙 저장소 테이블 예시 DDL
-- sql_server_daily_check.py 의 insert_results_to_repository() 가 참조하는 스키마.
-- 실제 환경에 맞게 DB명/스키마/컬럼 타입을 조정해서 사용할 것.

USE DBCHECK;
GO

IF OBJECT_ID('dbo.DAILY_CHECK_RESULT') IS NOT NULL
    DROP TABLE dbo.DAILY_CHECK_RESULT;
GO

CREATE TABLE dbo.DAILY_CHECK_RESULT
(
    id                          bigint IDENTITY(1,1) PRIMARY KEY,
    server_alias                nvarchar(20)   NOT NULL,   -- SQLSVR1~4
    host_name                   nvarchar(128)  NOT NULL,   -- @@SERVERNAME
    db_name                     nvarchar(128)  NOT NULL,
    biz_desc                    nvarchar(200)  NULL,
    db_status                   nvarchar(50)   NULL,       -- 정상 / ONLINE 외 상태
    cluster_status              nvarchar(20)   NULL,       -- 정상/비정상/단독서버
    shared_disk_status          nvarchar(20)   NULL,
    msdtc_status                nvarchar(20)   NULL,
    job_fail_check               nvarchar(1000) NULL,       -- 정상 또는 실패잡 목록
    buffer_cache_hit_ratio        decimal(5,1)   NULL,       -- Buffer Cache Hit Ratio(%), 서버 단위 broadcast
    plan_cache_hit_ratio          decimal(5,1)   NULL,       -- Plan Cache Hit Ratio(%, Library Cache 대응), 서버 단위 broadcast
    engine_used_pct              decimal(5,2)   NULL,       -- 엔진(설치) 영역 사용률(%), 서버 단위 broadcast
    data_used_pct                decimal(5,2)   NULL,       -- 데이터파일 영역 사용률(%), DB 단위
    log_used_pct                 decimal(5,2)   NULL,       -- 트랜잭션로그 영역 사용률(%), DB 단위
    last_full_backup_finished   datetime       NULL,
    backup_duration             varchar(100)   NULL,
    backup_size_mb              float          NULL,
    long_running_check           nvarchar(200)  NULL,
    error_log_check              nvarchar(200)  NULL,
    checked_at                  datetime       NOT NULL,
    inserted_at                 datetime       NOT NULL DEFAULT GETDATE()
);
GO

CREATE INDEX IX_DAILY_CHECK_RESULT_checked_at
    ON dbo.DAILY_CHECK_RESULT (checked_at, server_alias, db_name);
GO
