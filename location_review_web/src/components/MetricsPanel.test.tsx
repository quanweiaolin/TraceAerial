import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { MetricsPanel } from "./MetricsPanel";

test("三个指标都有值时正常显示文字", () => {
  render(
    <MetricsPanel
      gpsDistance={12.5}
      visualMatch={{
        matched: true,
        inliers: 42,
        inlier_ratio: 0.8,
        auto_accepted: true,
        auto_reason: "inlier_ratio_high",
      }}
      llmVerdict={{
        same_location: true,
        confidence: 0.9,
        reasoning: "画面特征一致",
      }}
    />,
  );

  expect(screen.getByText(/GPS距离: 12.5/)).toBeInTheDocument();
  expect(screen.getByText(/视觉比对/)).toBeInTheDocument();
  expect(screen.getByText(/LLM判断/)).toBeInTheDocument();
});

test("gpsDistance为null时显示无数据而不是null", () => {
  render(
    <MetricsPanel gpsDistance={null} visualMatch={null} llmVerdict={null} />,
  );

  expect(screen.getByText("GPS距离: 无数据")).toBeInTheDocument();
  expect(screen.queryByText(/null/)).not.toBeInTheDocument();
});

test("visualMatch和llmVerdict为null时也显示无数据", () => {
  render(
    <MetricsPanel gpsDistance={null} visualMatch={null} llmVerdict={null} />,
  );

  expect(screen.getByText(/视觉比对: 无数据/)).toBeInTheDocument();
  expect(screen.getByText(/LLM判断: 无数据/)).toBeInTheDocument();
});
