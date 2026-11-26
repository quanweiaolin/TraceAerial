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
