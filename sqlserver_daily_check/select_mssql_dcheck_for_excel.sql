-- =============================================================================
-- SQL Server 일일점검결과 엑셀 출력용 조회 쿼리 (Oracle: IRDMOW.TB_MON_DB_MSSQL_DCHECK)
--   타 DBMS 점검결과 시트(호스트명 | DB명 | 인스턴스명 | 업무설명 | 점검항목...)와 같은 양식.
--   - 정상 표기: '정상 서비스'(서비스 상태) / '특이사항 없음'(점검 항목)
--   - 비율/사용률: 소수 둘째 자리 숫자
--   - 백업 미확인: '확인필요'
--   점검일자는 기본값으로 오늘(SYSDATE). 다른 날짜는 :CHECK_DATE(YYYYMMDD) 바인드로 바꿔서 사용.
--   ※ 한글 별칭은 Oracle 12.2 이상(식별자 128바이트)에서 동작. 11g 등은 30바이트 제한으로
--     ORA-00972 가 날 수 있으니 그 경우 별칭을 빼고 출력 스크립트에서 헤더를 지정할 것.
-- =============================================================================
SELECT HOST_NAME                                                     AS "호스트명"
     , DB_NAME                                                       AS "DB명"
     , INSTANCE_NAME                                                 AS "인스턴스명"
     , BIZ_DESC                                                      AS "업무설명"
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
  FROM IRDMOW.TB_MON_DB_MSSQL_DCHECK
 WHERE CHECK_DATE = TO_CHAR(SYSDATE, 'YYYYMMDD')      -- 특정일: WHERE CHECK_DATE = :CHECK_DATE
 ORDER BY SERVER_ALIAS, DB_NAME
