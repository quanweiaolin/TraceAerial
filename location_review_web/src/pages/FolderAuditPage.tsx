import { useState } from "react";
import { apiFetch, ApiError } from "../api/client";
import type { FolderAuditVideo } from "../api/types";

export function FolderAuditPage() {
  const [prefix, setPrefix] = useState("");
  const [videos, setVideos] = useState<FolderAuditVideo[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [generateError, setGenerateError] = useState<string | null>(null);
  const [generatedShas, setGeneratedShas] = useState<Set<string>>(new Set());

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setLoading(true);
    setError(null);
    setGenerateError(null);
    try {
      const data = await apiFetch<FolderAuditVideo[]>(
        `/location-review/folder-audit?storage_path_prefix=${encodeURIComponent(prefix)}`,
      );
      setVideos(data);
    } catch (err) {
      setVideos(null);
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
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
        <button type="submit" disabled={loading}>
          查询
        </button>
      </form>

      {loading && <p>加载中...</p>}
      {error && <p role="alert">{error}</p>}
      {generateError && <p role="alert">{generateError}</p>}

      {!loading && !error && videos !== null && videos.length === 0 && (
        <p>这个前缀下没有视频</p>
      )}

      {!loading && !error && videos !== null && videos.length > 0 && (
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
