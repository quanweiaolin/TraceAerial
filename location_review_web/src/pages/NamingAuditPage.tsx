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

  function valueFor(alert: NamingAlert): string {
    return chosenValue[alert.id] ?? alert.suggested_value ?? "";
  }

  async function handleConfirm(alert: NamingAlert) {
    setSubmitting((prev) => ({ ...prev, [alert.id]: true }));
    setActionError((prev) => ({ ...prev, [alert.id]: null }));
    try {
      await apiFetch(`/location-review/naming-alerts/${alert.id}/confirm`, {
        method: "POST",
        body: JSON.stringify({ chosen_value: valueFor(alert) }),
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

                {actionError[alert.id] && <p role="alert">{actionError[alert.id]}</p>}
              </li>
            ) : (
              <li key={alert.id} data-testid={`alert-${alert.id}`}>
                <strong>合并候选：{alert.city}</strong>
                <p>涉及地点：{alert.location_ids.join(", ")}</p>
                <p>推荐名称：{alert.suggested_value}</p>

                <Link to="/groups">去Groups页面合并</Link>

                <button
                  type="button"
                  onClick={() => handleDismiss(alert)}
                  disabled={!!submitting[alert.id]}
                >
                  忽略
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
