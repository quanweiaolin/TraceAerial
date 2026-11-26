import { useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type { AgreementRate } from "../api/types";

export function AgreementRatePage() {
  const [rate, setRate] = useState<AgreementRate | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function loadRate() {
    setLoading(true);
    setError(null);
    try {
      const data = await apiFetch<AgreementRate>("/location-review/agreement-rate");
      setRate(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadRate();
  }, []);

  return (
    <div>
      <h1>一致率看板</h1>

      {loading && <p>加载中...</p>}
      {error && <p role="alert">{error}</p>}

      {!loading && !error && rate && (
        <section>
          <dl>
            <dt>总数</dt>
            <dd>{rate.total}</dd>
            <dt>一致</dt>
            <dd>{rate.agreed}</dd>
            <dt>不一致</dt>
            <dd>{rate.disagreed}</dd>
          </dl>

          {rate.ready_for_automation ? (
            <p>已达标，可以切换为LLM自动判断</p>
          ) : (
            <p role="alert">尚未达标，暂不能切换为LLM自动判断</p>
          )}
        </section>
      )}
    </div>
  );
}

export default AgreementRatePage;
