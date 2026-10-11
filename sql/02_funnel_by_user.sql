-- ============================================================
-- 用户行为漏斗：按【用户去重】口径计算各环节转化率
--
-- 口径说明（写在最前面，避免被问到时说不清）：
--   1. 以"用户"为单位，同一用户在某环节出现过即计 1 人，不按行为次数算；
--   2. 采用严格顺序漏斗：进入下一环节的用户必须已经完成上一环节
--      （浏览 → 收藏 → 加购 → 购买），所以人数逐级递减；
--   3. 如果只做 COUNT(DISTINCT user_id) 分环节统计再相除，
--      会出现"收藏 → 加购 193%" 这种明显错误的数字 ——
--      因为用户可能直接加购而没收藏，环节之间不是严格包含关系。
--
-- 运行方式：
--   mysql -u root -p < sql/02_funnel_by_user.sql
-- ============================================================

USE taobao_analysis;

-- 各环节去重人数与逐级转化率（严格顺序漏斗）
WITH v AS (SELECT DISTINCT user_id FROM user_behavior WHERE behavior_type = 'pv'),
     f AS (SELECT DISTINCT user_id FROM user_behavior WHERE behavior_type = 'fav'),
     c AS (SELECT DISTINCT user_id FROM user_behavior WHERE behavior_type = 'cart'),
     b AS (SELECT DISTINCT user_id FROM user_behavior WHERE behavior_type = 'buy'),
     step1 AS (SELECT user_id FROM v),
     step2 AS (SELECT s.user_id FROM step1 s JOIN f USING (user_id)),
     step3 AS (SELECT s.user_id FROM step2 s JOIN c USING (user_id)),
     step4 AS (SELECT s.user_id FROM step3 s JOIN b USING (user_id))
SELECT '浏览'                     AS 环节, (SELECT COUNT(*) FROM step1) AS 人数,
       NULL                       AS 相对上一环节转化率
UNION ALL SELECT '浏览→收藏',              (SELECT COUNT(*) FROM step2),
       (SELECT ROUND(COUNT(*) / (SELECT COUNT(*) FROM step1) * 100, 1) FROM step2)
UNION ALL SELECT '收藏→加购',              (SELECT COUNT(*) FROM step3),
       (SELECT ROUND(COUNT(*) / (SELECT COUNT(*) FROM step2) * 100, 1) FROM step3)
UNION ALL SELECT '加购→购买',              (SELECT COUNT(*) FROM step4),
       (SELECT ROUND(COUNT(*) / (SELECT COUNT(*) FROM step3) * 100, 1) FROM step4);

-- 整体转化率：浏览用户里最终产生购买的比例
WITH v AS (SELECT DISTINCT user_id FROM user_behavior WHERE behavior_type = 'pv'),
     b AS (SELECT DISTINCT user_id FROM user_behavior WHERE behavior_type = 'buy')
SELECT (SELECT COUNT(*) FROM v)                      AS 浏览用户数,
       (SELECT COUNT(*) FROM v JOIN b USING (user_id)) AS 浏览且购买用户数,
       ROUND((SELECT COUNT(*) FROM v JOIN b USING (user_id))
             / (SELECT COUNT(*) FROM v) * 100, 1)     AS 浏览到购买转化率百分比;
