import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "./client";

export interface ApiQueryResult<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  // 同一个path重新发起一次请求——用在"提交表单后要刷新列表"的场景
  // (QueuePage/GroupsPage/FolderAuditPage)，path字符串本身没变，
  // 单靠useEffect的path依赖触发不了重新fetch。
  refetch: () => void;
}

// QueuePage/LocationsPage/GroupsPage/AgreementRatePage/FolderAuditPage这五
// 个页面原来各自手写了一遍"useState+useEffect+try/catch"的GET请求骨架，
// 这里抽成一个hook去重(见Finding I8)。`path`传null表示"现在还不该发
// 请求"(FolderAuditPage在用户填完前缀点查询之前不该发请求)。
export function useApiQuery<T>(path: string | null): ApiQueryResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshIndex, setRefreshIndex] = useState(0);

  useEffect(() => {
    if (path === null) {
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    apiFetch<T>(path)
      .then((result) => {
        if (!cancelled) {
          setData(result);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          // apiFetch已经把后端的detail字段提取成err.message了，这里
          // 不需要再单独判断ApiError。
          setError(err instanceof Error ? err.message : "加载失败");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
    // path不变但refreshIndex变了也要重新发请求，所以它要进依赖数组。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, refreshIndex]);

  const refetch = useCallback(() => {
    setRefreshIndex((index) => index + 1);
  }, []);

  return { data, loading, error, refetch };
}
