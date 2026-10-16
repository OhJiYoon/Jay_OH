-- SQL Server 일일점검결과 엑셀 출력용 조회 (설명은 README.md 참고)
SELECT HOST_NAME                                                     AS "호스트명"
     , DB_NAME                                                       AS "DB명"
     , INSTANCE_NAME                                                 AS "인스턴스명"
--   , BIZ_DESC                                                      AS "업무설명"     -- 업무설명 정리 전까지 미표시
     , DB_STATUS                                                     AS "DBMS 서비스"
     , CLUSTER_STATUS                                                AS "클러스터(MSFC) 상태"
     , ACTIVE_NODE                                                   AS "Active 노드"
     , SHARED_DISK_STATUS                                            AS "공유디스크 상태"
     , MSDTC_STATUS                                                  AS "MSDTC 상태"
     , ERROR_LOG_CHECK                                               AS "ErrorLog"
     , BUFFER_CACHE_HIT_RATIO                                        AS "Buffer Cache Hit Ratio"
     , PLAN_CACHE_HIT_RATIO                                          AS "Plan Cache Hit Ratio"
     , ENGINE_USED_PCT                                               AS "엔진 영역(80%이하)"
     , DATA_USED_PCT                                                 AS "데이터 영역(95%이하)"
     , LOG_USED_PCT                                                  AS "트랜잭션로그 영역(80%이하)"
     , NVL(TO_CHAR(LAST_FULL_BACKUP_DT, 'YYYY-MM-DD HH24:MI:SS'),
           NVL(BACKUP_DURATION, '확인필요'))                          AS "데이터백업(Full)"
     , JOB_FAIL_CHECK                                                AS "Agent Job 점검"
     , LONG_RUNNING_CHECK                                            AS "장시간 수행SQL 점검"
     , '특이사항 없음'                                                AS "맥스게이지 프로세스"
  FROM IRDMOW.TB_MON_DB_MSSQL_DCHECK
 WHERE REG_DT = TO_CHAR(SYSDATE, 'YYYYMMDD')
 ORDER BY SERVER_ALIAS, DB_NAME
