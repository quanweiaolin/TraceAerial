import { useState } from "react";
import { apiFetch, ApiError } from "../api/client";
import { useApiQuery } from "../api/useApiQuery";
import type { GroupSummary, LocationSummary } from "../api/types";

export function GroupsPage() {
  const {
    data: groupsData,
    loading: groupsLoading,
    error: groupsError,
    refetch: refetchGroups,
  } = useApiQuery<GroupSummary[]>("/location-review/groups");
  const groups = groupsData ?? [];

  const {
    data: locationsData,
    loading: locationsLoading,
    error: locationsError,
    refetch: refetchLocations,
  } = useApiQuery<LocationSummary[]>("/location-review/locations");
  const locations = locationsData ?? [];

  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [groupName, setGroupName] = useState("");
  const [mergeError, setMergeError] = useState<string | null>(null);
  const [merging, setMerging] = useState(false);

  function toggleSelected(id: number) {
    setSelectedIds((prev) =>
      prev.includes(id) ? prev.filter((existing) => existing !== id) : [...prev, id],
    );
  }

  async function handleMerge(event: React.FormEvent) {
    event.preventDefault();
    setMerging(true);
    setMergeError(null);
    try {
      await apiFetch<{ id: number; name: string; confirmed_by: string; confirmed_at: string }>(
        "/location-review/groups/merge",
        {
          method: "POST",
          body: JSON.stringify({ location_ids: selectedIds, group_name: groupName }),
        },
      );
      setSelectedIds([]);
      setGroupName("");
      refetchGroups();
      refetchLocations();
    } catch (err) {
      if (err instanceof ApiError) {
        setMergeError(err.message);
      } else {
        setMergeError(err instanceof Error ? err.message : "合并失败");
      }
    } finally {
      setMerging(false);
    }
  }

  const canSubmit = selectedIds.length > 0 && groupName.trim() !== "";

  return (
    <div>
      <h1>分组管理</h1>

      <section>
        <h2>已有分组</h2>
        {groupsLoading && <p>加载中...</p>}
        {groupsError && <p role="alert">{groupsError}</p>}
        {!groupsLoading && !groupsError && groups.length === 0 && <p>还没有任何分组</p>}
        {!groupsLoading && !groupsError && groups.length > 0 && (
          <ul>
            {groups.map((group) => (
              <li key={group.id} data-testid={`group-${group.id}`}>
                <strong>{group.name}</strong>
                <ul>
                  {group.members.map((member) => (
                    <li key={member.id}>
                      {member.city} / <span>{member.site}</span>
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section>
        <h2>合并为新分组</h2>
        {locationsLoading && <p>加载中...</p>}
        {locationsError && <p role="alert">{locationsError}</p>}

        {!locationsLoading && !locationsError && (
          <form onSubmit={handleMerge}>
            <ul>
              {locations.map((location) => (
                <li key={location.id}>
                  <label htmlFor={`location-checkbox-${location.id}`}>{location.site}</label>
                  <input
                    id={`location-checkbox-${location.id}`}
                    type="checkbox"
                    checked={selectedIds.includes(location.id)}
                    onChange={() => toggleSelected(location.id)}
                  />
                  <span>
                    （{location.city}，{location.site}）
                  </span>
                </li>
              ))}
            </ul>

            <label htmlFor="group-name">分组名称：</label>
            <input
              id="group-name"
              type="text"
              value={groupName}
              onChange={(event) => setGroupName(event.target.value)}
            />

            <button type="submit" disabled={!canSubmit || merging}>
              合并
            </button>
          </form>
        )}

        {mergeError && <p role="alert">{mergeError}</p>}
      </section>
    </div>
  );
}

export default GroupsPage;
