-- ============================================================
-- scan_discoveries.resolved_video_sha256 / notifications.video_sha256
-- 这两个外键列一直没建索引，只有FK约束本身——导致任何对videos表的DELETE
-- 都要对这两张表做全表扫描来检查引用(2026-10-09发现，scan_discoveries
-- 当时有1321万行，删87条videos卡住不动就是因为这个)。
--
-- 用CONCURRENTLY建索引，不锁表，不影响生产正在跑的扫盘/通知写入，
-- 但不能在事务块里执行，这两条要分开跑(不要用BEGIN包起来)。
-- 大表上建索引本身会花一些时间，是一次性成本，换来以后所有涉及
-- 这两张表按video_sha256查询/删除的操作都变快。
-- ============================================================

SET search_path TO aerial, public;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_scan_discoveries_resolved_video_sha256
    ON scan_discoveries(resolved_video_sha256);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_notifications_video_sha256
    ON notifications(video_sha256);
