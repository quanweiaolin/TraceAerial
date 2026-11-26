import { useState } from "react";
import { apiFetch, ApiError } from "../api/client";
import { useApiQuery } from "../api/useApiQuery";
import type { GroupSummary, LocationSummary } from "../api/types";

const NEW_ALIAS_KEY_OPTION = "__new__";

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

  // 别名编辑草稿，按group.id分开存——每个分组有自己独立的下拉选择/
  // 新key输入/值输入状态，互不干扰
  const [aliasKeySelect, setAliasKeySelect] = useState<Record<number, string>>({});
  const [aliasCustomKey, setAliasCustomKey] = useState<Record<number, string>>({});
  const [aliasValue, setAliasValue] = useState<Record<number, string>>({});
  const [aliasSaving, setAliasSaving] = useState<Record<number, boolean>>({});
  const [aliasError, setAliasError] = useState<Record<number, string | null>>({});

  // 候选key从已经拉取的全部分组里去重算出来，不需要单独的接口/枚举表——
  // 新客户出现过一次之后，下拉框自然就多一个选项
  const knownAliasKeys = Array.from(new Set(groups.flatMap((g) => Object.keys(g.aliases))));

  async function handleSaveAlias(groupId: number) {
    const selected = aliasKeySelect[groupId] ?? "";
    const key =
      selected === NEW_ALIAS_KEY_OPTION
        ? (aliasCustomKey[groupId] ?? "").trim()
        : selected.trim();
    if (key === "") return;
    setAliasSaving((prev) => ({ ...prev, [groupId]: true }));
    setAliasError((prev) => ({ ...prev, [groupId]: null }));
    try {
      const rawValue = (aliasValue[groupId] ?? "").trim();
      await apiFetch(`/location-review/groups/${groupId}/aliases`, {
        method: "PATCH",
        body: JSON.stringify({ key, value: rawValue === "" ? null : rawValue }),
      });
      setAliasKeySelect((prev) => ({ ...prev, [groupId]: "" }));
      setAliasCustomKey((prev) => ({ ...prev, [groupId]: "" }));
      setAliasValue((prev) => ({ ...prev, [groupId]: "" }));
      refetchGroups();
    } catch (err) {
      setAliasError((prev) => ({
        ...prev,
        [groupId]: err instanceof ApiError ? err.message : err instanceof Error ? err.message : "保存失败",
      }));
    } finally {
      setAliasSaving((prev) => ({ ...prev, [groupId]: false }));
    }
  }

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
            {groups.map((group) => {
              const selected = aliasKeySelect[group.id] ?? "";
              const resolvedKey =
                selected === NEW_ALIAS_KEY_OPTION
                  ? (aliasCustomKey[group.id] ?? "").trim()
                  : selected.trim();
              const canSubmitAlias = resolvedKey !== "";
              return (
                <li key={group.id} data-testid={`group-${group.id}`}>
                  <strong>{group.name}</strong>
                  <ul>
                    {group.members.map((member) => (
                      <li key={member.id}>
                        {member.city} / <span>{member.site}</span>
                      </li>
                    ))}
                  </ul>

                  <div>
                    <h3>别名</h3>
                    {Object.keys(group.aliases).length === 0 ? (
                      <p>暂无别名</p>
                    ) : (
                      <ul>
                        {Object.entries(group.aliases).map(([key, value]) => (
                          <li key={key}>
                            {key}: {value}
                          </li>
                        ))}
                      </ul>
                    )}

                    <label htmlFor={`alias-key-select-${group.id}`}>别名key：</label>
                    <select
                      id={`alias-key-select-${group.id}`}
                      value={selected}
                      onChange={(event) =>
                        setAliasKeySelect((prev) => ({ ...prev, [group.id]: event.target.value }))
                      }
                    >
                      <option value="">请选择</option>
                      {knownAliasKeys.map((key) => (
                        <option key={key} value={key}>
                          {key}
                        </option>
                      ))}
                      <option value={NEW_ALIAS_KEY_OPTION}>新增...</option>
                    </select>

                    {selected === NEW_ALIAS_KEY_OPTION && (
                      <>
                        <label htmlFor={`alias-new-key-${group.id}`}>新key：</label>
                        <input
                          id={`alias-new-key-${group.id}`}
                          type="text"
                          value={aliasCustomKey[group.id] ?? ""}
                          onChange={(event) =>
                            setAliasCustomKey((prev) => ({ ...prev, [group.id]: event.target.value }))
                          }
                        />
                      </>
                    )}

                    <label htmlFor={`alias-value-${group.id}`}>别名值：</label>
                    <input
                      id={`alias-value-${group.id}`}
                      type="text"
                      value={aliasValue[group.id] ?? ""}
                      onChange={(event) =>
                        setAliasValue((prev) => ({ ...prev, [group.id]: event.target.value }))
                      }
                    />

                    <button
                      type="button"
                      onClick={() => handleSaveAlias(group.id)}
                      disabled={!canSubmitAlias || !!aliasSaving[group.id]}
                    >
                      保存别名
                    </button>

                    {aliasError[group.id] && <p role="alert">{aliasError[group.id]}</p>}
                  </div>
                </li>
              );
            })}
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
