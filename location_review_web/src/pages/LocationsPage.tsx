import { useState } from "react";
import { apiFetch, ApiError } from "../api/client";
import { useApiQuery } from "../api/useApiQuery";
import type { LocationSummary } from "../api/types";

export function LocationsPage() {
  const { data, loading, error, refetch } = useApiQuery<LocationSummary[]>(
    "/location-review/locations",
  );
  const locations = data ?? [];

  const [city, setCity] = useState("");
  const [sitePrefix, setSitePrefix] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  async function handleCreate(event: React.FormEvent) {
    event.preventDefault();
    setCreating(true);
    setCreateError(null);
    try {
      await apiFetch<{ id: number; city: string; site: string }>("/location-review/locations", {
        method: "POST",
        body: JSON.stringify({ city, site_prefix: sitePrefix }),
      });
      refetch();
      setCity("");
      setSitePrefix("");
    } catch (err) {
      if (err instanceof ApiError) {
        setCreateError(err.message);
      } else {
        setCreateError(err instanceof Error ? err.message : "新建失败");
      }
    } finally {
      setCreating(false);
    }
  }

  const canSubmit = city.trim() !== "" && sitePrefix.trim() !== "";

  return (
    <div>
      <h1>地点管理</h1>

      <form onSubmit={handleCreate} style={{ marginBottom: 12 }}>
        <label htmlFor="location-city">城市：</label>
        <input
          id="location-city"
          type="text"
          value={city}
          onChange={(event) => setCity(event.target.value)}
        />
        <label htmlFor="location-site-prefix">地点前缀：</label>
        <input
          id="location-site-prefix"
          type="text"
          value={sitePrefix}
          onChange={(event) => setSitePrefix(event.target.value)}
        />
        <button type="submit" disabled={!canSubmit || creating}>
          新建
        </button>
      </form>

      {createError && <p role="alert">{createError}</p>}

      {loading && <p>加载中...</p>}
      {error && <p role="alert">{error}</p>}

      {!loading && !error && locations.length === 0 && <p>还没有任何地点</p>}

      {!loading && !error && locations.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>城市</th>
              <th>地点</th>
              <th>所属分组</th>
              <th>代表帧</th>
            </tr>
          </thead>
          <tbody>
            {locations.map((location) => (
              <tr key={location.id} data-testid={`location-row-${location.id}`}>
                <td>{location.city}</td>
                <td>{location.site}</td>
                <td>{location.location_group_id ?? "-"}</td>
                <td>{location.representative_frame_path ?? "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export default LocationsPage;
