import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { MemoryRouter } from "react-router-dom";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { GroupsPage } from "./GroupsPage";
import type { GroupSummary, LocationSummary } from "../api/types";

const locationA: LocationSummary = {
  id: 1,
  city: "成都",
  site: "panshiroad_1",
  representative_frame_path: null,
  location_group_id: null,
};

const locationB: LocationSummary = {
  id: 2,
  city: "成都",
  site: "panshiroad_2",
  representative_frame_path: null,
  location_group_id: null,
};

const existingGroup: GroupSummary = {
  id: 1,
  name: "盘石路片区",
  confirmed_at: "2026-11-20T00:00:00Z",
  members: [{ id: 1, city: "成都", site: "panshiroad_1" }],
  aliases: {},
};

let currentLocations: LocationSummary[] = [];
let currentGroups: GroupSummary[] = [];

const server = setupServer(
  http.get("*/location-review/locations", () => HttpResponse.json(currentLocations)),
  http.get("*/location-review/groups", () => HttpResponse.json(currentGroups)),
);

beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  currentLocations = [];
  currentGroups = [];
});
afterAll(() => server.close());

test("加载后显示已有分组列表及其成员", async () => {
  currentGroups = [existingGroup];
  render(
    <MemoryRouter>
      <GroupsPage />
    </MemoryRouter>,
  );

  expect(await screen.findByText("盘石路片区")).toBeInTheDocument();
  expect(await screen.findByText("panshiroad_1")).toBeInTheDocument();
});

test("选中地点后点击合并按钮，提交成功后刷新分组列表", async () => {
  currentLocations = [locationA, locationB];
  currentGroups = [];
  let capturedBody: unknown = null;
  server.use(
    http.post("*/location-review/groups/merge", async ({ request }) => {
      capturedBody = await request.json();
      currentGroups = [
        {
          id: 2,
          name: "新片区",
          confirmed_at: "2026-11-26T00:00:00Z",
          members: [locationA, locationB],
          aliases: {},
        },
      ];
      return HttpResponse.json({
        id: 2,
        name: "新片区",
        confirmed_by: "tester",
        confirmed_at: "2026-11-26T00:00:00Z",
      });
    }),
  );

  render(
    <MemoryRouter>
      <GroupsPage />
    </MemoryRouter>,
  );

  const user = userEvent.setup();
  expect(await screen.findByText("panshiroad_1")).toBeInTheDocument();

  await user.click(screen.getByLabelText("panshiroad_1"));
  await user.click(screen.getByLabelText("panshiroad_2"));
  await user.type(screen.getByLabelText("分组名称："), "新片区");
  await user.click(screen.getByRole("button", { name: "合并" }));

  await waitFor(() => {
    expect(capturedBody).toEqual({ location_ids: [1, 2], group_name: "新片区" });
  });

  expect(await screen.findByText("新片区")).toBeInTheDocument();
});

test("合并已属于别的分组的location时，后端返回409，前端要显示这个错误信息", async () => {
  currentLocations = [locationA];
  currentGroups = [];
  server.use(
    http.post("*/location-review/groups/merge", () =>
      HttpResponse.json({ detail: "已属于另一个分组" }, { status: 409 }),
    ),
  );

  render(
    <MemoryRouter>
      <GroupsPage />
    </MemoryRouter>,
  );

  const user = userEvent.setup();
  expect(await screen.findByText("panshiroad_1")).toBeInTheDocument();

  await user.click(screen.getByLabelText("panshiroad_1"));
  await user.type(screen.getByLabelText("分组名称："), "新片区");
  await user.click(screen.getByRole("button", { name: "合并" }));

  expect(await screen.findByText("已属于另一个分组")).toBeInTheDocument();
});

test("展示已有别名，支持新增别名", async () => {
  currentGroups = [
    {
      id: 1,
      name: "盘石路片区",
      confirmed_at: "2026-11-20T00:00:00Z",
      members: [],
      aliases: { zyt_name: "ADS3-1" },
    },
  ];
  let capturedBody: unknown = null;
  server.use(
    http.patch("*/location-review/groups/1/aliases", async ({ request }) => {
      capturedBody = await request.json();
      // 2026-11-26最终review指出：原测试只断言请求体，从没验证保存成功后
      // 列表真的刷新、表单真的清空——这正是生产代码里唯一需要"人工走查
      // 才能确认正确"的部分。这里改成跟"选中地点后点击合并按钮"那个测试
      // 同样的模式：handler里真的更新currentGroups，再断言UI反映出新值。
      currentGroups = [
        {
          id: 1,
          name: "盘石路片区",
          confirmed_at: "2026-11-20T00:00:00Z",
          members: [],
          aliases: { zyt_name: "ADS3-1", new_customer: "X-1" },
        },
      ];
      return HttpResponse.json({
        id: 1,
        aliases: { zyt_name: "ADS3-1", new_customer: "X-1" },
      });
    }),
  );

  render(
    <MemoryRouter>
      <GroupsPage />
    </MemoryRouter>,
  );

  // 用完整文本匹配(不是部分正则)，避免跟下拉框里同名的<option>撞上——
  // "zyt_name"这个key既出现在别名展示列表里，也出现在候选key下拉框的
  // 选项里，用/zyt_name/这种部分匹配的正则会命中两处
  expect(await screen.findByText("zyt_name: ADS3-1")).toBeInTheDocument();

  const user = userEvent.setup();
  await user.selectOptions(screen.getByLabelText("别名key："), "新增...");
  await user.type(screen.getByLabelText("新key："), "new_customer");
  await user.type(screen.getByLabelText("别名值："), "X-1");
  await user.click(screen.getByRole("button", { name: "保存别名" }));

  await waitFor(() => {
    expect(capturedBody).toEqual({ key: "new_customer", value: "X-1" });
  });

  // 保存成功后：新别名真的出现在列表里，而不是只发了请求没刷新
  expect(await screen.findByText("new_customer: X-1")).toBeInTheDocument();
  // 表单重置了：下拉框回到未选状态，值输入框清空——否则在同一个分组上
  // 接着加第二条别名会沿用上一条的残留输入
  expect(screen.getByLabelText("别名值：")).toHaveValue("");
  expect(screen.queryByLabelText("新key：")).not.toBeInTheDocument();
});

test("新key为空时保存按钮保持禁用", async () => {
  currentGroups = [
    {
      id: 1,
      name: "盘石路片区",
      confirmed_at: "2026-11-20T00:00:00Z",
      members: [],
      aliases: {},
    },
  ];
  server.use(
    http.patch("*/location-review/groups/1/aliases", () => {
      throw new Error("不应该发起这个请求");
    }),
  );

  render(
    <MemoryRouter>
      <GroupsPage />
    </MemoryRouter>,
  );

  const user = userEvent.setup();
  await user.selectOptions(await screen.findByLabelText("别名key："), "新增...");
  await user.type(screen.getByLabelText("别名值："), "X-1");

  expect(screen.getByRole("button", { name: "保存别名" })).toBeDisabled();
});

test("别名值为空时保存按钮保持禁用——防止误触空值把已有别名删掉", async () => {
  // 2026-11-26最终review Important#1：原来只要求key非空，值留空直接点
  // 保存会发一个value:null的请求，等于悄悄把已有别名删了，且没有任何
  // 确认——这里要求key和值都非空才能点保存。
  currentGroups = [
    {
      id: 1,
      name: "盘石路片区",
      confirmed_at: "2026-11-20T00:00:00Z",
      members: [],
      aliases: { zyt_name: "ADS3-1" },
    },
  ];
  server.use(
    http.patch("*/location-review/groups/1/aliases", () => {
      throw new Error("不应该发起这个请求");
    }),
  );

  render(
    <MemoryRouter>
      <GroupsPage />
    </MemoryRouter>,
  );

  const user = userEvent.setup();
  await user.selectOptions(await screen.findByLabelText("别名key："), "zyt_name");
  // 值输入框留空，不输入任何内容

  expect(screen.getByRole("button", { name: "保存别名" })).toBeDisabled();
});

test("点击删除按钮可以删除已有别名", async () => {
  // 2026-11-26最终review Important#1：删除别名要有一个显式的、独立于
  // "保存"按钮的入口，不能靠"把值清空再保存"这种容易误触的隐式路径。
  currentGroups = [
    {
      id: 1,
      name: "盘石路片区",
      confirmed_at: "2026-11-20T00:00:00Z",
      members: [],
      aliases: { zyt_name: "ADS3-1" },
    },
  ];
  let capturedBody: unknown = null;
  server.use(
    http.patch("*/location-review/groups/1/aliases", async ({ request }) => {
      capturedBody = await request.json();
      currentGroups = [
        {
          id: 1,
          name: "盘石路片区",
          confirmed_at: "2026-11-20T00:00:00Z",
          members: [],
          aliases: {},
        },
      ];
      return HttpResponse.json({ id: 1, aliases: {} });
    }),
  );

  render(
    <MemoryRouter>
      <GroupsPage />
    </MemoryRouter>,
  );

  expect(await screen.findByText("zyt_name: ADS3-1")).toBeInTheDocument();

  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "删除 zyt_name" }));

  await waitFor(() => {
    expect(capturedBody).toEqual({ key: "zyt_name", value: null });
  });
  expect(await screen.findByText("暂无别名")).toBeInTheDocument();
});

test("没有别名时显示空态", async () => {
  currentGroups = [
    {
      id: 2,
      name: "无别名组",
      confirmed_at: "2026-11-26T00:00:00Z",
      members: [],
      aliases: {},
    },
  ];

  render(
    <MemoryRouter>
      <GroupsPage />
    </MemoryRouter>,
  );

  expect(await screen.findByText("暂无别名")).toBeInTheDocument();
});
