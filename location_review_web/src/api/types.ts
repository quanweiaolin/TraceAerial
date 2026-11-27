// 后端 app/services/location_visual_match.py 里 compare_frames() 的真实
// 返回形状——不是一个枚举字符串，是SIFT+RANSAC比对的具体数值结果。
export interface VisualMatchResult {
  matched: boolean;
  inliers: number;
  inlier_ratio: number;
  auto_accepted: boolean;
  auto_reason: string;
}

// 后端 app/services/location_llm_judge.py 里 judge_async() 的真实返回形状。
// same_location为null代表LLM调用失败(见后端"失败不计入一致率"的设计)，
// 不是"判断为不是同一地点"。
export interface LlmVerdict {
  same_location: boolean | null;
  confidence: number;
  reasoning: string;
}

// 跟SQL的CHECK约束(add_location_review_schema.sql的human_decision列)
// 完全对应的三个取值，见后端location_review.py的apply_human_decision。
export type HumanDecision = "same_location" | "different_location" | "new_location" | null;

export type ReviewStatus = "pending" | "decided" | "screenshot_failed" | string;

export interface ReviewCandidate {
  id: number;
  city: string;
  site: string;
  representative_frame_path: string | null;
  group_name: string | null;
}

export interface ReviewItem {
  id: number;
  status: ReviewStatus;
  video_sha256: string;
  candidate_location_id: number | null;
  screenshot_paths: string[];
  candidate: ReviewCandidate | null;
  gps_distance_meters: number | null;
  video_filename: string;
  video_city: string | null;
  video_collected_at: string | null;
  video_location_confirmed: boolean;
  visual_match_result: VisualMatchResult | null;
  llm_verdict: LlmVerdict | null;
  human_decision: HumanDecision;
}

// 后端 GET /location-review/folder-audit 的返回形状——按storage_path前缀列出
// 该目录下的视频，location_id为null代表这条视频还没被任何地点收录。
export interface FolderAuditVideo {
  video_sha256: string;
  storage_path: string;
  location_id: number | null;
  is_stable: boolean | null;
  collect_lon: number | null;
  collect_lat: number | null;
}

// 后端 GET /location-review/locations 的返回形状——locations表全量列表，
// 跟ReviewCandidate形状相似但不是同一个东西(多了location_group_id，少了
// group_name)，所以单独建一个类型而不是复用ReviewCandidate。
export interface LocationSummary {
  id: number;
  city: string;
  site: string;
  representative_frame_path: string | null;
  location_group_id: number | null;
}

// 后端 GET /location-review/groups 的返回形状——location_groups表全量列表，
// members是已归入该组的locations精简信息(不是LocationSummary全量字段，
// 后端只返回id/city/site)。
export interface GroupMember {
  id: number;
  city: string;
  site: string;
}

export interface GroupSummary {
  id: number;
  name: string;
  confirmed_at: string;
  members: GroupMember[];
  // 客户对这个地点各自的命名/编号，比如{zyt_name: "ADS3-1"}。没有别名的
  // 分组是空对象，展示时继续用name，不强制要求有别名。
  aliases: Record<string, string>;
}

// 后端 GET /location-review/agreement-rate 的返回形状——LLM判断与人工复核决策的
// 一致率统计，ready_for_automation为false代表一致率还未达到切换为LLM自动判断的阈值。
export interface AgreementRate {
  total: number;
  agreed: number;
  disagreed: number;
  ready_for_automation: boolean;
}

// 后端 GET /location-review/naming-alerts 的返回形状——命名审核提示。
// alert_type为"name_drift"时raw_site_votes是扁平的{候选名字: 票数}，
// 为"merge_candidate"时是嵌套的{候选名字: {location_id: 票数}}(两种类型
// 形状不同，这里用Record<string, unknown>兜底，具体渲染时各自按需要的
// 形状去读)。suggested_value为null代表后端还没能算出一个推荐值。
export interface NamingAlert {
  id: number;
  alert_type: "name_drift" | "merge_candidate";
  city: string;
  location_ids: number[];
  raw_site_votes: Record<string, unknown>;
  suggested_value: string | null;
  status: "pending" | "confirmed" | "dismissed";
  created_at: string;
}
