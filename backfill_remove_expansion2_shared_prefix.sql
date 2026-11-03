-- ============================================================
-- 把videos.storage_path里历史遗留的/mnt/Expansion2-shared/前缀统一换成
-- 直连的/mnt/Expansion2/——videos表不该出现bindfs路径(2026-11-03确定)：
-- bindfs是单个用户态FUSE进程转发所有请求，机器繁忙时实测比直连慢约
-- 1000倍，而且这条前缀只是字符串层面的区别，底层是同一份物理文件，
-- 换前缀不会丢数据/不用重新读文件。
--
-- 代码那边已经改成"下载落地用aliyun_download_dir(bindfs,chmod需要)，
-- 扫描入库用aliyun_scan_dir(直连)"，这条SQL只处理改动之前已经入库的
-- 历史记录——不跑这条，老记录会一直带着shared前缀，新记录才是直连的，
-- 两种格式混在一张表里。
-- ============================================================

SET search_path TO aerial, public;

-- 1. dry run，先看一下范围有多少条
SELECT COUNT(*) FROM videos WHERE storage_path LIKE '/mnt/Expansion2-shared/%';

-- 2. 正式替换，只动这一个固定前缀，其余路径原样不变
UPDATE videos
SET storage_path = '/mnt/Expansion2/' || substring(storage_path FROM length('/mnt/Expansion2-shared/') + 1)
WHERE storage_path LIKE '/mnt/Expansion2-shared/%';

-- 3. 验证：改完之后这条应该是0
SELECT COUNT(*) FROM videos WHERE storage_path LIKE '/mnt/Expansion2-shared/%';
