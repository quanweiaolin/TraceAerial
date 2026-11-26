import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { ApiError, apiFetch } from "../api/client";
import { ImageGallery } from "../components/ImageGallery";
import { MetricsPanel } from "../components/MetricsPanel";
import type { HumanDecision, LocationSummary, ReviewItem } from "../api/types";

const STATUS_LABELS: Record<string, string> = {
  pending: "待处理",
  decided: "已决策",
  screenshot_failed: "截图失败",
};

export function ReviewDetailPage() {
  const { itemId } = useParams<{ itemId: string }>();
  const [item, setItem] = useState<ReviewItem | null>(null);
  const [locations, setLocations] = useState<LocationSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedLocationId, setSelectedLocationId] = useState<string>("");
  const [decisionError, setDecisionError] = useState<string | null>(null);
  const [decisionSuccess, setDecisionSuccess] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // "新地点"决策需要先建一个全新的Location，不能跟"确认同一地点"共用
  // 已有地点下拉——否则这两种决策在UI上就是同一个操作，复核员没法真的
  // 新建一个地点(Finding I3)。
  const [creatingNewLocation, setCreatingNewLocation] = useState(false);
  const [newCity, setNewCity] = useState("");
  const [newSitePrefix, setNewSitePrefix] = useState("");

  useEffect(() => {
    if (!itemId) {
      return;
    }
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    Promise.all([
      apiFetch<ReviewItem>(`/location-review/queue/${itemId}`),
      apiFetch<LocationSummary[]>("/location-review/locations"),
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
    setDecisionSuccess(null);

    const needsTarget = decision === "same_location";
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
      setDecisionSuccess("已提交");
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

  async function submitNewLocationDecision(event: React.FormEvent) {
    event.preventDefault();
    if (!item) {
      return;
    }
    setDecisionError(null);
    setDecisionSuccess(null);
    setSubmitting(true);
    try {
      // 先建地点拿到id，再拿这个id去决策——两步都用apiFetch，任何一步
      // 失败都要让复核员看到具体错误，不能默默吞掉(Finding I3)。
      const created = await apiFetch<{ id: number; city: string; site: string }>(
        "/location-review/locations",
        {
          method: "POST",
          body: JSON.stringify({ city: newCity, site_prefix: newSitePrefix }),
        },
      );
      await apiFetch(`/location-review/queue/${item.id}/decide`, {
        method: "POST",
        body: JSON.stringify({
          decision: "new_location",
          target_location_id: created.id,
        }),
      });
      const refreshed = await apiFetch<ReviewItem>(`/location-review/queue/${item.id}`);
      setItem(refreshed);
      setDecisionSuccess("已提交");
      setCreatingNewLocation(false);
      setNewCity("");
      setNewSitePrefix("");
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
    setDecisionSuccess(null);
    setSubmitting(true);
    try {
      await apiFetch(`/location-review/videos/${item.video_sha256}/unlock`, {
        method: "POST",
        body: JSON.stringify({ reason: "人工解锁" }),
      });
      const refreshed = await apiFetch<ReviewItem>(`/location-review/queue/${item.id}`);
      setItem(refreshed);
      setDecisionSuccess("已解锁");
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

  const decided = item.status === "decided";
  // 已决策的item不该再从这个界面被重新决策一次——要改只能先解锁(如果
  // 视频被锁定了)或者离开再回来，不是在这个按钮组里直接改(Finding I2)。
  const decisionButtonsDisabled = submitting || decided;

  return (
    <div>
      <h1>复核详情：{item.video_filename}</h1>

      <p>
        状态：{STATUS_LABELS[item.status] ?? item.status}
        {decided && item.human_decision && `（${item.human_decision}）`}
      </p>
      {decisionSuccess && <p>{decisionSuccess}</p>}

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
          ) : (
            <>
              <p>
                {item.candidate.city} / {item.candidate.site}
                {item.candidate.group_name !== null && `（已合并至：${item.candidate.group_name}）`}
              </p>
              {item.candidate.representative_frame_path === null ? (
                <p>新地点候选，无参考图</p>
              ) : (
                <ImageGallery images={[item.candidate.representative_frame_path]} />
              )}
            </>
          )}
        </div>
      </div>

      <MetricsPanel
        gpsDistance={item.gps_distance_meters}
        visualMatch={item.visual_match_result}
        llmVerdict={item.llm_verdict}
      />

      {!creatingNewLocation && (
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
      )}

      {decisionError && <p role="alert">{decisionError}</p>}

      <div style={{ marginTop: 12, display: "flex", gap: 8 }}>
        <button
          type="button"
          disabled={decisionButtonsDisabled}
          onClick={() => submitDecision("same_location")}
        >
          确认同一地点
        </button>
        <button
          type="button"
          disabled={decisionButtonsDisabled}
          onClick={() => submitDecision("different_location")}
        >
          不是这个候选
        </button>
        <button
          type="button"
          disabled={decisionButtonsDisabled}
          onClick={() => setCreatingNewLocation(true)}
        >
          新地点
        </button>
        {item.video_location_confirmed === true && (
          <button type="button" disabled={submitting} onClick={unlockVideo}>
            解锁视频
          </button>
        )}
      </div>

      {creatingNewLocation && (
        <form onSubmit={submitNewLocationDecision} style={{ marginTop: 12 }}>
          <h3>新建地点</h3>
          <label htmlFor="new-location-city">城市：</label>
          <input
            id="new-location-city"
            type="text"
            value={newCity}
            onChange={(event) => setNewCity(event.target.value)}
          />
          <label htmlFor="new-location-site-prefix">地点前缀：</label>
          <input
            id="new-location-site-prefix"
            type="text"
            value={newSitePrefix}
            onChange={(event) => setNewSitePrefix(event.target.value)}
          />
          <button
            type="submit"
            disabled={submitting || newCity.trim() === "" || newSitePrefix.trim() === ""}
          >
            创建并确认
          </button>
          <button type="button" onClick={() => setCreatingNewLocation(false)} disabled={submitting}>
            取消
          </button>
        </form>
      )}
    </div>
  );
}

export default ReviewDetailPage;
