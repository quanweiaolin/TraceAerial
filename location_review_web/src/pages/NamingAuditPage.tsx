import { useState } from "react";
import { Link } from "react-router-dom";
import { apiFetch, ApiError } from "../api/client";
import { useApiQuery } from "../api/useApiQuery";
import type { NamingAlert } from "../api/types";

export function NamingAuditPage() {
  const {
    data: alertsData,
    loading,
    error,
    refetch,
  } = useApiQuery<NamingAlert[]>("/location-review/naming-alerts?status=pending");
  const alerts = alertsData ?? [];

  // 命名漂移提示的输入框草稿，按alert.id分开存，互不干扰(参照GroupsPage
  // 别名编辑的按id分开存模式)。
  const [chosenValue, setChosenValue] = useState<Record<number, string>>({});
  const [submitting, setSubmitting] = useState<Record<number, boolean>>({});
  const [actionError, setActionError] = useState<Record<number, string | null>>({});

  function defaultValueFor(alert: NamingAlert): string {
    // 预填这个地点**当前**的site值，不是裸的suggested_value(终审Important#5)：
    // 扫描器建地址时会加消歧后缀(`{raw_site}_{序号}`)，直接提交
    // suggested_value会把后缀悄悄抹掉，而且同一个raw_site家族有第二个
    // 地址时还会撞UNIQUE(city, site)报400。人工在这里做的是"改拼写、
    // 保持地点可识别"，不是从零重写整个site。
    const current = alert.locations.find((location) => location.id === alert.location_ids[0]);
    return current?.site ?? alert.suggested_value ?? "";
  }

  function valueFor(alert: NamingAlert): string {
    return chosenValue[alert.id] ?? defaultValueFor(alert);
  }

  async function handleConfirm(alert: NamingAlert) {
    setSubmitting((prev) => ({ ...prev, [alert.id]: true }));
    setActionError((prev) => ({ ...prev, [alert.id]: null }));
    try {
      await apiFetch(`/location-review/naming-alerts/${alert.id}/confirm`, {
        method: "POST",
        // merge_candidate的confirm只是把提示标成"已看过/已处理"，后端不碰
        // 任何Location行，也不需要chosen_value
        body: JSON.stringify(
          alert.alert_type === "name_drift" ? { chosen_value: valueFor(alert) } : {},
        ),
      });
      refetch();
    } catch (err) {
      setActionError((prev) => ({
        ...prev,
        [alert.id]: err instanceof ApiError ? err.message : err instanceof Error ? err.message : "确认失败",
      }));
    } finally {
      setSubmitting((prev) => ({ ...prev, [alert.id]: false }));
    }
  }

  async function handleDismiss(alert: NamingAlert) {
    setSubmitting((prev) => ({ ...prev, [alert.id]: true }));
    setActionError((prev) => ({ ...prev, [alert.id]: null }));
    try {
      await apiFetch(`/location-review/naming-alerts/${alert.id}/dismiss`, {
        method: "POST",
      });
      refetch();
    } catch (err) {
      setActionError((prev) => ({
        ...prev,
        [alert.id]: err instanceof ApiError ? err.message : err instanceof Error ? err.message : "忽略失败",
      }));
    } finally {
      setSubmitting((prev) => ({ ...prev, [alert.id]: false }));
    }
  }

  // 涉及地点展示：光给location_id人判断不了"这几个是不是同一个地方"，
  // 必须把每个地点当前的city/site也列出来(终审Important#4)
  function renderLocations(alert: NamingAlert) {
    return (
      <div>
        涉及地点：
        <ul>
          {alert.location_ids.map((locationId) => {
            const location = alert.locations.find((item) => item.id === locationId);
            return (
              <li key={locationId}>
                #{locationId}{" "}
                {location ? `${location.city} / ${location.site}` : "(地点已不存在)"}
              </li>
            );
          })}
        </ul>
      </div>
    );
  }

  return (
    <div>
      <h1>命名审核</h1>

      {loading && <p>加载中...</p>}
      {error && <p role="alert">{error}</p>}

      {!loading && !error && alerts.length === 0 && <p>暂无待处理的命名提示</p>}

      {!loading && !error && alerts.length > 0 && (
        <ul>
          {alerts.map((alert) =>
            alert.alert_type === "name_drift" ? (
              <li key={alert.id} data-testid={`alert-${alert.id}`}>
                <strong>命名漂移：{alert.city}</strong>

                {renderLocations(alert)}

                <ul>
                  {Object.entries(alert.raw_site_votes as Record<string, number>).map(
                    ([name, count]) => (
                      <li key={name}>
                        {name}：{count}票
                      </li>
                    ),
                  )}
                </ul>

                <label htmlFor={`chosen-value-${alert.id}`}>最终名称：</label>
                <input
                  id={`chosen-value-${alert.id}`}
                  type="text"
                  value={valueFor(alert)}
                  onChange={(event) =>
                    setChosenValue((prev) => ({ ...prev, [alert.id]: event.target.value }))
                  }
                />

                <button
                  type="button"
                  onClick={() => handleConfirm(alert)}
                  disabled={!!submitting[alert.id]}
                >
                  确认
                </button>

                {/* 误报的漂移提示也要有出口，而且这个出口不能改写Location.site */}
                <button
                  type="button"
                  onClick={() => handleDismiss(alert)}
                  disabled={!!submitting[alert.id]}
                >
                  忽略(误报)
                </button>

                {actionError[alert.id] && <p role="alert">{actionError[alert.id]}</p>}
              </li>
            ) : (
              <li key={alert.id} data-testid={`alert-${alert.id}`}>
                <strong>合并候选：{alert.city}</strong>

                {renderLocations(alert)}

                <p>推荐名称：{alert.suggested_value}</p>

                <Link to="/groups">去Groups页面合并</Link>

                {/* 真的在Groups页面合并完之后用这个收尾：语义是"确实是同一个
                    地方、已经处理了"，跟"忽略(误报)"不是一回事 */}
                <button
                  type="button"
                  onClick={() => handleConfirm(alert)}
                  disabled={!!submitting[alert.id]}
                >
                  已处理(已去Groups合并)
                </button>

                <button
                  type="button"
                  onClick={() => handleDismiss(alert)}
                  disabled={!!submitting[alert.id]}
                >
                  忽略(误报)
                </button>

                {actionError[alert.id] && <p role="alert">{actionError[alert.id]}</p>}
              </li>
            ),
          )}
        </ul>
      )}
    </div>
  );
}

export default NamingAuditPage;
