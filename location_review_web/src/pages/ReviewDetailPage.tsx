import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { ApiError, apiFetch } from "../api/client";
import { ImageGallery } from "../components/ImageGallery";
import { MetricsPanel } from "../components/MetricsPanel";
import type { HumanDecision, ReviewItem } from "../api/types";

interface LocationOption {
  id: number;
  city: string;
  site: string;
  representative_frame_path: string | null;
  location_group_id: number;
}

export function ReviewDetailPage() {
  const { itemId } = useParams<{ itemId: string }>();
  const [item, setItem] = useState<ReviewItem | null>(null);
  const [locations, setLocations] = useState<LocationOption[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedLocationId, setSelectedLocationId] = useState<string>("");
  const [decisionError, setDecisionError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!itemId) {
      return;
    }
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    Promise.all([
      apiFetch<ReviewItem>(`/location-review/queue/${itemId}`),
      apiFetch<LocationOption[]>("/location-review/locations"),
    ])
      .then(([itemData, locationData]) => {
        if (!cancelled) {
          setItem(itemData);
          setLocations(locationData);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setLoadError(err instanceof Error ? err.message : "加载失败");
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
  }, [itemId]);

  async function submitDecision(decision: Exclude<HumanDecision, null>) {
    if (!item) {
      return;
    }
    setDecisionError(null);

    const needsTarget = decision === "same_location" || decision === "new_location";
    const targetLocationId = selectedLocationId ? Number(selectedLocationId) : null;

    if (needsTarget && targetLocationId === null) {
      setDecisionError("请先选择目标地点");
      return;
    }

    setSubmitting(true);
    try {
      await apiFetch(`/location-review/queue/${item.id}/decide`, {
        method: "POST",
        body: JSON.stringify({
          decision,
          target_location_id: decision === "different_location" ? null : targetLocationId,
        }),
      });
      const refreshed = await apiFetch<ReviewItem>(`/location-review/queue/${item.id}`);
      setItem(refreshed);
    } catch (err) {
      if (err instanceof ApiError) {
        setDecisionError(err.message);
      } else {
        setDecisionError(err instanceof Error ? err.message : "提交失败");
      }
    } finally {
      setSubmitting(false);
    }
  }

  async function unlockVideo() {
    if (!item) {
      return;
    }
    setDecisionError(null);
    setSubmitting(true);
    try {
      await apiFetch(`/location-review/videos/${item.video_sha256}/unlock`, {
        method: "POST",
        body: JSON.stringify({ reason: "人工解锁" }),
      });
      const refreshed = await apiFetch<ReviewItem>(`/location-review/queue/${item.id}`);
      setItem(refreshed);
    } catch (err) {
      if (err instanceof ApiError) {
        setDecisionError(err.message);
      } else {
        setDecisionError(err instanceof Error ? err.message : "解锁失败");
      }
    } finally {
      setSubmitting(false);
    }
  }

  if (loading) {
    return <p>加载中...</p>;
  }

  if (loadError) {
    return <p role="alert">{loadError}</p>;
  }

  if (!item) {
    return null;
  }

  return (
    <div>
      <h1>复核详情：{item.video_filename}</h1>

      <div style={{ display: "flex", gap: 24 }}>
        <div>
          <h2>当前视频</h2>
          {item.screenshot_paths.length === 0 ? (
            <p>截图提取失败</p>
          ) : (
            <ImageGallery images={[item.screenshot_paths[0]]} />
          )}
        </div>

        <div>
          <h2>候选地点</h2>
          {item.candidate === null ? (
            <p>新地点候选，无参考图</p>
          ) : item.candidate.representative_frame_path === null ? (
            <p>新地点候选，无参考图</p>
          ) : (
            <ImageGallery images={[item.candidate.representative_frame_path]} />
          )}
        </div>
      </div>

      <MetricsPanel
        gpsDistance={item.gps_distance_meters}
        visualMatch={item.visual_match_result}
        llmVerdict={item.llm_verdict}
      />

      <div style={{ marginTop: 16 }}>
        <label htmlFor="target-location">目标地点：</label>
        <select
          id="target-location"
          value={selectedLocationId}
          onChange={(event) => setSelectedLocationId(event.target.value)}
        >
          <option value="">请选择</option>
          {locations.map((location) => (
            <option key={location.id} value={location.id}>
              {location.city} - {location.site}
            </option>
          ))}
        </select>
      </div>

      {decisionError && <p role="alert">{decisionError}</p>}

      <div style={{ marginTop: 12, display: "flex", gap: 8 }}>
        <button
          type="button"
          disabled={submitting}
          onClick={() => submitDecision("same_location")}
        >
          确认同一地点
        </button>
        <button
          type="button"
          disabled={submitting}
          onClick={() => submitDecision("different_location")}
        >
          不是这个候选
        </button>
        <button
          type="button"
          disabled={submitting}
          onClick={() => submitDecision("new_location")}
        >
          新地点
        </button>
        {item.video_location_confirmed === true && (
          <button type="button" disabled={submitting} onClick={unlockVideo}>
            解锁视频
          </button>
        )}
      </div>
    </div>
  );
}

export default ReviewDetailPage;
