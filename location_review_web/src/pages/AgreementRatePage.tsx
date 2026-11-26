import { useApiQuery } from "../api/useApiQuery";
import type { AgreementRate } from "../api/types";

export function AgreementRatePage() {
  const { data: rate, loading, error } = useApiQuery<AgreementRate>(
    "/location-review/agreement-rate",
  );

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
