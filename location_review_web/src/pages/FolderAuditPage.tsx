import { useState } from "react";
import { apiFetch, ApiError } from "../api/client";
import { useApiQuery } from "../api/useApiQuery";
import type { FolderAuditVideo } from "../api/types";

// 后端folder_audit接口加了.limit(500)防止空/过短前缀拖垮查询——命中这个
// 上限时前端提示"结果可能被截断"，不需要后端额外返回一个截断标记字段，
// 直接比较返回条数就行(Finding I5)。
const RESULT_LIMIT = 500;

export function FolderAuditPage() {
  const [prefix, setPrefix] = useState("");
  const [queryPath, setQueryPath] = useState<string | null>(null);
  const [generateError, setGenerateError] = useState<string | null>(null);
  const [generatedShas, setGeneratedShas] = useState<Set<string>>(new Set());

  const { data: videos, loading, error, refetch } = useApiQuery<FolderAuditVideo[]>(queryPath);

  const trimmedPrefix = prefix.trim();
  const canSubmit = trimmedPrefix !== "";

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!canSubmit) {
      return;
    }
    setGenerateError(null);
    const nextPath = `/location-review/folder-audit?storage_path_prefix=${encodeURIComponent(trimmedPrefix)}`;
    if (nextPath === queryPath) {
      // 前缀跟上次查询完全一样——path不变不会触发hook重新请求，得显式
      // 调refetch()才能真的重新发一次查询(比如想看看是不是有新视频入库)。
      refetch();
    } else {
      setQueryPath(nextPath);
    }
  }

  async function handleGenerate(video: FolderAuditVideo) {
    setGenerateError(null);
    try {
      await apiFetch("/location-review/queue/generate", {
        method: "POST",
        body: JSON.stringify({
          video_sha256: video.video_sha256,
          video_path: video.storage_path,
        }),
      });
      setGeneratedShas((prev) => new Set(prev).add(video.video_sha256));
    } catch (err) {
      if (err instanceof ApiError) {
        setGenerateError(err.message);
      } else {
        setGenerateError(err instanceof Error ? err.message : "生成失败");
      }
    }
  }

  return (
    <div>
      <h1>文件夹审查</h1>
      <form onSubmit={handleSubmit} style={{ marginBottom: 12 }}>
        <label htmlFor="storage-path-prefix">存储路径前缀：</label>
        <input
          id="storage-path-prefix"
          type="text"
          value={prefix}
          onChange={(event) => setPrefix(event.target.value)}
        />
        <button type="submit" disabled={!canSubmit || loading}>
          查询
        </button>
      </form>

      {loading && <p>加载中...</p>}
      {error && <p role="alert">{error}</p>}
      {generateError && <p role="alert">{generateError}</p>}

      {!loading && !error && videos !== null && videos !== undefined && videos.length === 0 && (
        <p>这个前缀下没有视频</p>
      )}

      {!loading && !error && videos && videos.length >= RESULT_LIMIT && (
        <p role="alert">结果可能被截断，请输入更精确的路径前缀</p>
      )}

      {!loading && !error && videos && videos.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>存储路径</th>
              <th>所属地点</th>
              <th>稳定</th>
              <th>经度</th>
              <th>纬度</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {videos.map((video) => {
              const unpicked = video.location_id === null;
              return (
                <tr
                  key={video.video_sha256}
                  data-testid={`folder-audit-row-${video.video_sha256}`}
                  data-unpicked={unpicked ? "true" : "false"}
                  style={{
                    backgroundColor: unpicked ? "#fff3cd" : "transparent",
                    border: unpicked ? "1px solid #e0a800" : undefined,
                  }}
                >
                  <td>{video.storage_path}</td>
                  <td>{video.location_id ?? "-"}</td>
                  <td>
                    {video.is_stable === null ? "-" : video.is_stable ? "是" : "否"}
                  </td>
                  <td>{video.collect_lon ?? "-"}</td>
                  <td>{video.collect_lat ?? "-"}</td>
                  <td>
                    {generatedShas.has(video.video_sha256) ? (
                      <span>已生成</span>
                    ) : (
                      <button type="button" onClick={() => handleGenerate(video)}>
                        生成候选
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

export default FolderAuditPage;
