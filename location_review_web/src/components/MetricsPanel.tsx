import type { LlmVerdict, VisualMatchResult } from "../api/types";

interface MetricsPanelProps {
  gpsDistance: number | null;
  visualMatch: VisualMatchResult | null;
  llmVerdict: LlmVerdict | null;
}

function formatGpsDistance(gpsDistance: number | null): string {
  if (gpsDistance === null) {
    return "无数据";
  }
  return `${gpsDistance}米`;
}

function formatVisualMatch(visualMatch: VisualMatchResult | null): string {
  if (visualMatch === null) {
    return "无数据";
  }
  return `${visualMatch.matched ? "匹配" : "不匹配"}（内点数${visualMatch.inliers}，内点率${visualMatch.inlier_ratio}）`;
}

function formatLlmVerdict(llmVerdict: LlmVerdict | null): string {
  if (llmVerdict === null) {
    return "无数据";
  }
  if (llmVerdict.same_location === null) {
    return "无数据";
  }
  return `${llmVerdict.same_location ? "同一地点" : "不是同一地点"}（置信度${llmVerdict.confidence}）`;
}

export function MetricsPanel({ gpsDistance, visualMatch, llmVerdict }: MetricsPanelProps) {
  return (
    <div style={{ display: "flex", gap: 24, marginTop: 12 }}>
      <span>GPS距离: {formatGpsDistance(gpsDistance)}</span>
      <span>视觉比对: {formatVisualMatch(visualMatch)}</span>
      <span>LLM判断: {formatLlmVerdict(llmVerdict)}</span>
    </div>
  );
}

export default MetricsPanel;
