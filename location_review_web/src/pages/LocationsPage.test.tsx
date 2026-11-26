import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { MemoryRouter } from "react-router-dom";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { LocationsPage } from "./LocationsPage";
import type { LocationSummary } from "../api/types";

const existingLocation: LocationSummary = {
  id: 1,
  city: "成都",
  site: "panshiroad_1",
  representative_frame_path: "/data/frames/1.jpg",
  location_group_id: null,
};

let currentLocations: LocationSummary[] = [];

const server = setupServer(
  http.get("*/location-review/locations", () => HttpResponse.json(currentLocations)),
);

beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  currentLocations = [];
});
afterAll(() => server.close());

test("加载后显示已有地点列表", async () => {
  currentLocations = [existingLocation];
  render(
    <MemoryRouter>
      <LocationsPage />
    </MemoryRouter>,
  );

  expect(await screen.findByText("成都")).toBeInTheDocument();
  expect(await screen.findByText("panshiroad_1")).toBeInTheDocument();
});

test("两个字段都填写前提交按钮禁用", async () => {
  currentLocations = [];
  render(
    <MemoryRouter>
      <LocationsPage />
    </MemoryRouter>,
  );

  const user = userEvent.setup();
  const submitButton = await screen.findByRole("button", { name: "新建" });
  expect(submitButton).toBeDisabled();

  await user.type(screen.getByLabelText("城市："), "成都");
  expect(submitButton).toBeDisabled();

  await user.type(screen.getByLabelText("地点前缀："), "panshiroad");
  expect(submitButton).not.toBeDisabled();
});

test("新建成功后列表刷新，新地点出现在列表中", async () => {
  currentLocations = [existingLocation];
  let capturedBody: unknown = null;
  server.use(
    http.post("*/location-review/locations", async ({ request }) => {
      capturedBody = await request.json();
      const created = { id: 2, city: "成都", site: "panshiroad_2" };
      currentLocations = [
        ...currentLocations,
        {
          id: created.id,
          city: created.city,
          site: created.site,
          representative_frame_path: null,
          location_group_id: null,
        },
      ];
      return HttpResponse.json(created);
    }),
  );

  render(
    <MemoryRouter>
      <LocationsPage />
    </MemoryRouter>,
  );

  expect(await screen.findByText("panshiroad_1")).toBeInTheDocument();

  const user = userEvent.setup();
  await user.type(screen.getByLabelText("城市："), "成都");
  await user.type(screen.getByLabelText("地点前缀："), "panshiroad");
  await user.click(screen.getByRole("button", { name: "新建" }));

  await waitFor(() => {
    expect(capturedBody).toEqual({ city: "成都", site_prefix: "panshiroad" });
  });

  expect(await screen.findByText("panshiroad_2")).toBeInTheDocument();
});
