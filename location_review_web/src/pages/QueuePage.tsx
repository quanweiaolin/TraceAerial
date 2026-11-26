import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useApiQuery } from "../api/useApiQuery";
import type { ReviewItem, ReviewStatus } from "../api/types";

const PAGE_SIZE = 20;

const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "pending", label: "待处理" },
  { value: "decided", label: "已决定" },
  { value: "screenshot_failed", label: "截图失败" },
  { value: "", label: "全部" },
];

function isSuperseded(item: ReviewItem): boolean {
  return item.status === "pending" && item.video_location_confirmed === true;
}

export function QueuePage() {
  const navigate = useNavigate();
  const [status, setStatus] = useState<string>("pending");
  const [page, setPage] = useState(1);
  const query = status ? `?status=${status}` : "";
  const { data, loading, error } = useApiQuery<ReviewItem[]>(`/location-review/queue${query}`);
  const items = data ?? [];

  useEffect(() => {
    setPage(1);
  }, [status]);

  const totalPages = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
  const pageItems = items.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  return (
    <div>
      <h1>复核队列</h1>
      <div style={{ marginBottom: 12 }}>
        <label htmlFor="status-filter">状态筛选：</label>
        <select
          id="status-filter"
          value={status}
          onChange={(event) => setStatus(event.target.value as ReviewStatus | "")}
        >
          {STATUS_OPTIONS.map((option) => (
            <option key={option.value || "all"} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>

      {loading && <p>加载中...</p>}
      {error && <p role="alert">{error}</p>}

      {!loading && !error && (
        <>
          <table>
            <thead>
              <tr>
                <th>文件名</th>
                <th>城市</th>
                <th>采集时间</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {pageItems.map((item) => (
                <tr
                  key={item.id}
                  onClick={() => navigate(`/queue/${item.id}`)}
                  style={{ cursor: "pointer" }}
                >
                  <td>{item.video_filename}</td>
                  <td>{item.video_city ?? "-"}</td>
                  <td>{item.video_collected_at ?? "-"}</td>
                  <td>
                    {item.status}
                    {isSuperseded(item) && (
                      <span style={{ color: "#999", marginLeft: 8 }}>
                        （已通过其它条目处理）
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <div style={{ marginTop: 12, display: "flex", gap: 8, alignItems: "center" }}>
            <button
              type="button"
              disabled={page <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
            >
              上一页
            </button>
            <span>
              第 {page} / {totalPages} 页
            </span>
            <button
              type="button"
              disabled={page >= totalPages}
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            >
              下一页
            </button>
          </div>
        </>
      )}
    </div>
  );
}

export default QueuePage;
